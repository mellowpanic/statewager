"""Unit tests for statewager core and publish static exporter."""
from __future__ import annotations

import csv
import inspect
import pathlib

from publish import (
    CSV_HEADERS,
    merge_state_results,
    read_state_csv,
    render_index_html,
    write_combined_csv,
    write_index_html,
    write_state_csv,
)
from statewager import StateMonthResult
from statewager.discover import (
    classify_cumulative_filename,
    classify_sportsbook_filename,
    extract_month_label,
    normalize_filename,
)
from statewager.model import (
    CORE_RESULT_FIELDS,
    find_stale_months,
    format_month_label,
    hold_pct,
    label_to_iso_month,
)
from statewager.states import ALL_STATES, iowa, load_state_module, missouri, pennsylvania


def test_all_ten_state_modules_expose_fetch_signature() -> None:
    assert len(ALL_STATES) == 10
    for slug in ALL_STATES:
        mod = load_state_module(slug)
        assert hasattr(mod, "fetch")
        sig = inspect.signature(mod.fetch)
        params = list(sig.parameters)
        assert params[:2] == ["month_start", "month_end"]
        assert "cache_dir" in sig.parameters
        assert "log" in sig.parameters


def test_state_month_result_and_holds() -> None:
    iso = label_to_iso_month("Mar 2025")
    assert iso == "2025-03"
    assert format_month_label(iso) == "Mar 2025"
    item = StateMonthResult(
        state="Nevada",
        month=iso,
        handle=1000.0,
        revenue=75.0,
        hold=hold_pct(1000.0, 75.0),
        retail_handle=400.0,
        retail_revenue=20.0,
        retail_hold=hold_pct(400.0, 20.0),
        mobile_handle=600.0,
        mobile_revenue=55.0,
        mobile_hold=hold_pct(600.0, 55.0),
        source_url="https://gaming.nv.gov/report.pdf",
    )
    assert item.month == "2025-03"
    assert item.hold == 7.5
    assert item.retail_hold == 5.0
    assert item.mobile_hold == 9.17
    assert len(CORE_RESULT_FIELDS) == 12


def test_publish_writes_csvs_and_static_index(tmp_path: pathlib.Path) -> None:
    docs_dir = tmp_path / "docs"
    data_dir = docs_dir / "data"
    sample = StateMonthResult(
        state="Ohio",
        month="2025-03",
        handle=750000000.0,
        revenue=65000000.0,
        hold=8.67,
        retail_handle=20000000.0,
        retail_revenue=1500000.0,
        retail_hold=7.5,
        mobile_handle=730000000.0,
        mobile_revenue=63500000.0,
        mobile_hold=8.7,
        source_url="https://casinocontrol.ohio.gov/report.xlsx",
    )
    state_csv = write_state_csv(str(data_dir), "ohio", [sample])
    all_csv = write_combined_csv(str(data_dir), [sample])
    html_path = write_index_html(str(docs_dir))

    with open(state_csv, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == CSV_HEADERS
    assert rows[1][:5] == ["Ohio", "2025-03", "750000000", "65000000", "8.67"]
    assert pathlib.Path(all_csv).is_file()
    html_text = pathlib.Path(html_path).read_text(encoding="utf-8")
    assert "data/all.csv" in html_text
    assert "<svg" in html_text or "createElementNS" in html_text


class TestNormalizeFilename:
    def test_lowercases(self) -> None:
        assert normalize_filename("FOO.XLSX") == "foo.xlsx"

    def test_collapses_whitespace(self) -> None:
        assert normalize_filename("a  b\t c") == "a b c"

    def test_unescapes_html(self) -> None:
        assert normalize_filename("a&amp;b") == "a&b"

    def test_empty_string(self) -> None:
        assert normalize_filename("") == ""

    def test_none_string(self) -> None:
        assert normalize_filename(None) == ""  # type: ignore[arg-type]


class TestExtractMonthLabel:
    def test_trailing_yyyy_mm(self) -> None:
        assert extract_month_label("fy26_mobile_sb_2026_04_apr") == "Apr 2026"

    def test_la_underscore_form(self) -> None:
        assert extract_month_label("fy23-24_mobile_sb_2023_07_july.xlsx") == "Jul 2023"

    def test_month_year(self) -> None:
        assert extract_month_label("apr-2026.xlsx") == "Apr 2026"

    def test_full_month_year(self) -> None:
        assert extract_month_label("April 2026 sportsbook") == "Apr 2026"

    def test_no_month(self) -> None:
        assert extract_month_label("fy24-25_summary.pdf") is None

    def test_invalid_month_number(self) -> None:
        assert extract_month_label("2024_13_misc") is None

    def test_prefers_trailing(self) -> None:
        assert extract_month_label("fy25_2026_04_apr") == "Apr 2026"

    def test_lower_or_mixed_case(self) -> None:
        assert extract_month_label("April_2026") == "Apr 2026"
        assert extract_month_label("APRIL_2026") == "Apr 2026"


class TestClassifySportsbookFilename:
    def test_mobile_per_month(self) -> None:
        result = classify_sportsbook_filename("fy25_mobile_sportsbook_apr_2026.xlsx")
        assert result == ("Apr 2026", "mobile")

    def test_retail_per_month(self) -> None:
        result = classify_sportsbook_filename("FY25_Retail_SB_2025_02_Feb.xlsx")
        assert result == ("Feb 2025", "retail")

    def test_online_keyword_maps_to_mobile(self) -> None:
        result = classify_sportsbook_filename(
            "fy26_online_sportsbook_apr_2026.xlsx",
            channel_keywords={"retail": ["retail"], "mobile": ["mobile", "online"]},
        )
        assert result == ("Apr 2026", "mobile")

    def test_non_sportsbook_returns_none(self) -> None:
        assert classify_sportsbook_filename("casino-2025-04.pdf") is None

    def test_no_channel_returns_none(self) -> None:
        assert classify_sportsbook_filename("fy26_sportsbook_apr_2026.xlsx") is None

    def test_no_month_returns_none(self) -> None:
        assert classify_sportsbook_filename("retail_sportsbook_summary.xlsx") is None

    def test_title_provides_month(self) -> None:
        result = classify_sportsbook_filename(
            "/files/report.xlsx",
            title="April 2026 Mobile Sportsbook Report",
        )
        assert result == ("Apr 2026", "mobile")

    def test_text_provides_channel(self) -> None:
        result = classify_sportsbook_filename(
            "/files/sportsbook_apr_2026.xlsx",
            text="Retail",
        )
        assert result == ("Apr 2026", "retail")

    def test_snapshot_timestamp_excluded(self) -> None:
        assert classify_sportsbook_filename("fy26_mobile_sb_20260401_apr.xlsx") is None

    def test_url_path_basename(self) -> None:
        url = "https://example.com/foo/bar/FY25_mobile_sportsbook_apr_2026.xlsx"
        assert classify_sportsbook_filename(url) == ("Apr 2026", "mobile")


class TestClassifyCumulativeFilename:
    def test_fy_range_dashes(self) -> None:
        result = classify_cumulative_filename("fy23-24_mobile_sb.xlsx")
        assert result is not None
        months, channel = result
        assert channel == "mobile"
        assert "Jul 2023" in months
        assert "Jun 2024" in months
        assert "Jul 2024" not in months
        assert len(months) == 12

    def test_fy_solo(self) -> None:
        result = classify_cumulative_filename("fy25_mobile_sportsbook.xlsx")
        assert result is not None
        months, channel = result
        assert channel == "mobile"
        assert "Jul 2024" in months
        assert "Jun 2025" in months
        assert "Jul 2025" not in months

    def test_calendar_year_range(self) -> None:
        result = classify_cumulative_filename("2024_2025_retail_sportsbook.xlsx")
        assert result is not None
        months, channel = result
        assert channel == "retail"
        assert "Jul 2024" in months
        assert "Jun 2025" in months

    def test_per_month_file_returns_none(self) -> None:
        assert classify_cumulative_filename("fy25_mobile_sb_2025_02_feb.xlsx") is None

    def test_snapshot_treated_as_cumulative(self) -> None:
        result = classify_cumulative_filename("fy24_25_mobile_sb_20250203.xlsx")
        assert result is not None
        months, channel = result
        assert channel == "mobile"
        assert "Jan 2025" in months

    def test_no_channel_returns_none(self) -> None:
        assert classify_cumulative_filename("fy24-25_sb.xlsx") is None

    def test_no_sportsbook_returns_none(self) -> None:
        assert classify_cumulative_filename("fy24-25_mobile_casino.xlsx") is None


class TestFindStaleMonths:
    def test_no_stale_when_no_majority(self) -> None:
        data = {"PA": {"Apr 2026"}, "IL": {"Apr 2026"}, "IN": set()}
        assert find_stale_months(data, min_peers=5) == []

    def test_pa_missing_when_peers_have(self) -> None:
        data = {
            "PA": set(),
            "IL": {"Apr 2026"},
            "IN": {"Apr 2026"},
            "IA": {"Apr 2026"},
            "KS": {"Apr 2026"},
            "MO": {"Apr 2026"},
        }
        result = find_stale_months(data, min_peers=5)
        assert ("PA", "Apr 2026", 5) in result

    def test_sorted_by_month_then_state(self) -> None:
        data = {
            "PA": set(),
            "IL": {"Apr 2026", "May 2026"},
            "IN": {"Apr 2026", "May 2026"},
            "IA": {"Apr 2026", "May 2026"},
            "KS": {"Apr 2026", "May 2026"},
            "MO": {"Apr 2026"},
        }
        result = find_stale_months(data, min_peers=5)
        labels = [(state, label) for state, label, _ in result]
        assert ("PA", "Apr 2026") in labels
        assert ("PA", "May 2026") not in labels

    def test_empty_input(self) -> None:
        assert find_stale_months({}, min_peers=1) == []


def test_publish_incremental_merge_preserves_older_months(tmp_path: pathlib.Path) -> None:
    data_dir = tmp_path / "data"
    old_dec = StateMonthResult("Ohio", "2024-12", 500.0, 50.0, 10.0, None, None, None, None, None, None, "u0")
    old_mar = StateMonthResult("Ohio", "2025-03", 600.0, 60.0, 10.0, None, None, None, None, None, None, "u1")
    csv_path = write_state_csv(str(data_dir), "ohio", [old_dec, old_mar])

    existing = read_state_csv(csv_path)
    fresh_mar = StateMonthResult("Ohio", "2025-03", 750.0, 75.0, 10.0, None, None, None, None, None, None, "u2")
    fresh_apr = StateMonthResult("Ohio", "2025-04", 700.0, 70.0, 10.0, None, None, None, None, None, None, "u3")
    merged = merge_state_results(existing, [fresh_mar, fresh_apr])
    assert [r.month for r in merged] == ["2024-12", "2025-03", "2025-04"]
    assert merged[0].handle == 500.0
    assert merged[1].handle == 750.0
    assert merged[2].handle == 700.0


def test_iowa_fy26_archive_and_link_discovery() -> None:
    assert iowa.FY_MEDIA_IDS[(2025, 2026)] == 508
    parsed = iowa._parse_fy_media_links(
        [
            {"href": "/media/508/download?inline", "text": "Fiscal Year 2026 Sports Wagering Revenue"},
            {"href": "/media/555/download", "text": "2026-2027 Sports Wagering Revenue"},
            {"href": "/other/link", "text": "Fiscal Year 2028 Sports Wagering Revenue"},
        ]
    )
    assert parsed == {(2025, 2026): 508, (2026, 2027): 555}


def test_pennsylvania_fy27_filter_and_option_parsing() -> None:
    assert pennsylvania.FILTER_IDS[(2026, 2027)] == 292
    html = (
        '<select name="field_gaming_revenue_fiscal_year_target_id">'
        '<option value="292">FY 2026/2027</option>'
        '<option value="310">FY 2027/2028</option>'
        "</select>"
    )
    assert pennsylvania.parse_filter_options(html) == {(2026, 2027): 292, (2027, 2028): 310}


def test_render_index_html_has_partial_month_controls() -> None:
    html_text = render_index_html()
    assert "includePartial" in html_text
    assert "expectedStatesForMonth" in html_text
    assert html_text.index('<th data-col="month">Month</th>') < html_text.index('<th data-col="state">State</th>')


def test_missouri_pdf_name_re_matches_underscores_and_revised() -> None:
    m1 = missouri.PDF_NAME_RE.search("FY26_SWFinReport/03_Mar/SW_Monthly_Financials_0326_revised.pdf")
    m2 = missouri.PDF_NAME_RE.search("FY27_SWFinReport/09_Sep/SW Monthly Financials 0926.pdf")
    assert m1 is not None and m1.group(1) == "0326"
    assert m2 is not None and m2.group(1) == "0926"


def test_cache_path_traversal_zero_byte_and_stale_refresh(tmp_path) -> None:
    import os
    import pytest
    from statewager.cache import cache_path, load_cached_or_fetch, refresh_cached_if_stale

    base = str(tmp_path)
    for bad in ("", "../secret.pdf", "/etc/passwd", "sub/../../secret.pdf"):
        with pytest.raises(ValueError):
            cache_path(base, bad)
    assert cache_path(base, "sub/file.pdf") == os.path.join(os.path.abspath(base), "sub", "file.pdf")

    empty_file = tmp_path / "empty.bin"
    empty_file.write_bytes(b"")
    calls = 0

    def _fetch_ok() -> bytes:
        nonlocal calls
        calls += 1
        return b"PKvalid"

    assert (
        load_cached_or_fetch(cache_file=str(empty_file), fetcher=_fetch_ok, offline=True)
        is None
    )
    assert calls == 0
    assert (
        load_cached_or_fetch(cache_file=str(empty_file), fetcher=_fetch_ok, offline=False)
        == b"PKvalid"
    )
    assert calls == 1

    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"<html>error</html>")
    assert (
        load_cached_or_fetch(
            cache_file=str(corrupt),
            fetcher=lambda: None,
            validate=lambda p: p.startswith(b"PK"),
            offline=True,
        )
        is None
    )

    valid_cached = tmp_path / "ok.xlsx"
    valid_cached.write_bytes(b"PKold")
    assert (
        refresh_cached_if_stale(
            str(valid_cached),
            b"PKold",
            max_age_seconds=3600,
            fetcher=lambda: b"PKnew",
            validate=lambda p: p.startswith(b"PK"),
            offline=True,
        )
        is None
    )
    assert (
        refresh_cached_if_stale(
            str(valid_cached),
            b"PKold",
            max_age_seconds=3600,
            fetcher=lambda: b"PKnew",
            validate=lambda p: p.startswith(b"PK"),
        )
        is None
    )
    assert (
        refresh_cached_if_stale(
            str(valid_cached),
            b"PKold",
            max_age_seconds=0,
            fetcher=lambda: b"<html>bad</html>",
            validate=lambda p: p.startswith(b"PK"),
        )
        is None
    )
    assert (
        refresh_cached_if_stale(
            str(valid_cached),
            b"PKold",
            max_age_seconds=-1,
            fetcher=lambda: b"PKnew",
            validate=lambda p: p.startswith(b"PK"),
        )
        == b"PKnew"
    )
    assert valid_cached.read_bytes() == b"PKnew"


def test_http_signature_discover_model_and_quality_guards(monkeypatch) -> None:
    import datetime
    import math
    from statewager import quality
    from statewager.discover import extract_month_label
    from statewager.http import _matches_signature
    from statewager.model import (
        fiscal_year_july,
        hold_pct,
        parse_month_spec,
        safe_float,
    )

    assert _matches_signature(b"<!doctype html><html></html>", "https://example.com/a.pdf", "auto") is False
    assert _matches_signature(b"<!doctype html><html></html>", "https://example.com/download", "auto") is False
    assert _matches_signature(b"<html>ok</html>", "https://example.com/a.html", "auto") is True
    assert extract_month_label("report_20250315.pdf") is None

    assert parse_month_spec("Mar 2025") == datetime.date(2025, 3, 1)
    assert parse_month_spec("March 2025") == datetime.date(2025, 3, 1)

    assert safe_float(True) is None
    assert safe_float(float("nan")) is None
    neg_zero_norm = safe_float(-0.0)
    assert neg_zero_norm == 0.0 and math.copysign(1.0, neg_zero_norm) > 0
    zero_hold = hold_pct(100.0, -0.001)
    assert zero_hold == 0.0 and math.copysign(1.0, zero_hold) > 0
    assert fiscal_year_july(2025, 7) == (2025, 2026)
    assert fiscal_year_july(2026, 6) == (2025, 2026)

    monkeypatch.setattr(quality, "_aggregator_lookup", lambda _s, _l, _log: (100_000.0, 10_000.0))
    agg_logs: list[str] = []
    assert quality.validate_against_aggregators(
        "Indiana", "Mar 2025", 100_000.0, 0.0, log=agg_logs.append
    ) is False
    assert any("revenue $0" in m for m in agg_logs)

    anom_logs: list[str] = []
    assert quality.check_anomaly(
        "Indiana",
        "Mar 2025",
        0.0,
        0.0,
        [(100_000.0, 10_000.0), (102_000.0, 10_200.0), (98_000.0, 9_800.0)],
        log=anom_logs.append,
    ) is False
    assert any("handle $0" in m for m in anom_logs)


def test_state_edge_cases_and_csv_null_roundtrip(tmp_path) -> None:
    from publish.csv_writer import read_state_csv, write_state_csv
    from statewager.model import StateMonthResult
    from statewager.parse import parse_dollar
    from statewager.states import illinois, kansas, nevada

    assert parse_dollar("-3455.1100") == -3455.11
    assert parse_dollar(-3455.11) == -3455.11
    assert illinois._NUM_TOKEN_RE.search("-$1,234.56") is not None
    assert illinois._NUM_TOKEN_RE.search("\u2212$500") is not None
    accrual_csv = (
        "Location Type,Licensee,State AGR\n"
        "In-Person Wagering,Casino A,100.00\n"
        "Online Wagering,Book B,-100.00\n"
    ).encode("utf-8")
    assert illinois.parse_accrual_csv(accrual_csv) == (0.0, 100.0, -100.0)

    assert kansas.normalize_pdf_label("  Subtotal  -  Retail: ") == "subtotal - retail:"
    assert "-" in kansas.AMOUNT_TOKEN_RE.findall("DraftKings  $1,000  -  $100")

    details = [("Clark County", 100.0, 50.0), ("Washoe County", 20.0, 60.0)]
    reconciled = nevada.reconcile_county_details(details, 50.0, log=lambda _msg: None)
    assert reconciled == [("Clark County", 100.0, 50.0)]

    partial = StateMonthResult(
        state="Mississippi",
        month="2025-03",
        handle=1000.0,
        revenue=-0.001,
        hold=-0.0,
        retail_handle=1000.0,
        retail_revenue=0.0,
        retail_hold=0.0,
        mobile_handle=None,
        mobile_revenue=None,
        mobile_hold=None,
        source_url="https://example.com/ms.xlsx",
    )
    csv_path = write_state_csv(str(tmp_path), "mississippi", [partial])
    loaded = read_state_csv(csv_path)
    assert len(loaded) == 1
    assert loaded[0].mobile_handle is None
    assert loaded[0].hold == 0.0

    empty_holder = tmp_path / "empty_metrics.csv"
    empty_holder.write_text(
        "state,month,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url\n"
        "Test,2025-03,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    loaded_blank = read_state_csv(str(empty_holder))
    assert loaded_blank[0].handle is None
    assert loaded_blank[0].revenue is None
    assert loaded_blank[0].hold is None


def test_operator_normalization_and_brand_classification() -> None:
    from statewager.operators import BRAND_ORDER, classify_operator_brand, normalize_operator_name

    assert normalize_operator_name("JACK CLEVELAND (BETJACK)") == "Jack Cleveland - BetJack"
    assert normalize_operator_name("FANATICS-AMERISTAR KC") == "Fanatics - Ameristar KC"
    assert normalize_operator_name("KS CROSSING - CAESAR'S") == "Kansas Crossing - Caesars"
    assert normalize_operator_name("WALKER'S BLUFF CASINO RESORT, LLC") == "Walker's Bluff Casino Resort, LLC"
    assert normalize_operator_name("VALLEY FORGE CASINO¹") == "Valley Forge Casino"

    # No residual multi-letter ALL-CAPS tokens outside known acronyms.
    allowed_acronyms = {"LLC", "KC", "IA", "IL", "IN", "KS", "MO", "OH", "PA", "ESPN", "TSB", "betPARX"}
    for raw in [
        "HOLLYWOOD COLUMBUS - PENN INTERACTIVE (TSB)",
        "BLUE CHIP CASINO",
        "PAR-A-DICE GAMING CORPORATION",
        "CAESARS SPORTSBOOK",
    ]:
        norm = normalize_operator_name(raw)
        for tok in norm.replace(",", " ").replace("(", " ").replace(")", " ").split():
            letters = "".join(ch for ch in tok if ch.isalpha())
            if len(letters) >= 2 and letters.isupper():
                assert tok in allowed_acronyms, f"Unexpected ALL-CAPS token {tok!r} in {norm!r}"

    assert classify_operator_brand("Fairmount Park, Inc.", state="Illinois") == "FanDuel"
    assert classify_operator_brand("Northside Crown Gaming LLC", state="Illinois") == "DraftKings"
    assert classify_operator_brand("Alton Casino, LLC", state="Illinois") == "Fanatics"
    assert classify_operator_brand("Hillside (Iowa) LLC", state="Iowa") == "bet365"
    assert classify_operator_brand("Ameristar Casino (Retail)", state="Iowa") == "Other / Retail Venue"
    assert classify_operator_brand("Kansas Star - Bet365", state="Kansas") == "bet365"
    assert classify_operator_brand("HOLLYWOOD - THE SCORE", state="Missouri") in {"PENN / ESPN Bet", "Other / Retail Venue"}
    assert classify_operator_brand("HOLLYWOOD - THESCORE", state="Missouri") == "PENN / ESPN Bet"
    assert classify_operator_brand("Valley Forge Casino", state="Pennsylvania") == "FanDuel"
    assert BRAND_ORDER[0] == "DraftKings"
    assert BRAND_ORDER[-1] == "Other / Retail Venue"


def test_seven_states_expose_has_details() -> None:
    from statewager.states import ALL_STATES, load_state_module

    detail_slugs = {
        slug for slug in ALL_STATES if getattr(load_state_module(slug), "HAS_DETAILS", False)
    }
    assert detail_slugs == {
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "missouri",
        "ohio",
        "pennsylvania",
    }


def test_detail_csv_extraction_dedup_merge_and_write(tmp_path) -> None:
    from publish.csv_writer import (
        DETAIL_CSV_HEADERS,
        PublishedDetailRow,
        extract_published_details,
        merge_detail_results,
        read_detail_csv,
        write_combined_detail_csv,
        write_state_detail_csv,
    )
    from statewager.model import OperatorMonthResult, StateMonthResult, finalize_state_month

    # finalize_state_month prefers publish_details during publish while preserving details for downstream consumers.
    indiana_res = finalize_state_month(
        state="Indiana",
        iso_month="2025-03",
        handle=500.0,
        revenue=50.0,
        details=[
            OperatorMonthResult("2025-03", "French Lick Resort", 300.0, 30.0, 10.0),
        ],
        publish_details=[
            OperatorMonthResult("2025-03", "French Lick Resort - bet365", 200.0, 20.0, 10.0),
            OperatorMonthResult("2025-03", "French Lick Resort - BetRivers", 100.0, 10.0, 10.0),
        ],
    )
    assert indiana_res is not None
    assert len(indiana_res.details) == 1
    assert indiana_res.details[0].operator == "French Lick Resort"

    # extract_published_details normalizes and sums duplicate normalized operators (e.g., mobile + retail lines).
    ohio_res = finalize_state_month(
        state="Ohio",
        iso_month="2025-03",
        handle=1000.0,
        revenue=100.0,
        source_url="https://example.com/oh.xlsx",
        details=[
            OperatorMonthResult(
                "2025-03",
                "BELTERRA PARK - FANDUEL",
                600.0,
                60.0,
                10.0,
                mobile_handle=600.0,
                mobile_revenue=60.0,
            ),
            OperatorMonthResult(
                "2025-03",
                "Belterra Park - FanDuel",
                400.0,
                40.0,
                10.0,
                retail_handle=400.0,
                retail_revenue=40.0,
            ),
        ],
    )
    assert ohio_res is not None
    extracted = extract_published_details([indiana_res, ohio_res])
    assert len(extracted) == 3
    oh_row = next(r for r in extracted if r.state == "Ohio")
    assert oh_row.operator == "Belterra Park - FanDuel"
    assert oh_row.brand == "FanDuel"
    assert oh_row.handle == 1000.0
    assert oh_row.revenue == 100.0
    assert oh_row.mobile_handle == 600.0
    assert oh_row.retail_handle == 400.0

    # Month-scoped merge replaces stale operators within a refreshed (state, month) while preserving older months.
    existing = [
        PublishedDetailRow("Ohio", "2025-02", "Old Op", "Other / Retail Venue", 10.0, 1.0, 10.0),
        PublishedDetailRow("Ohio", "2025-03", "Stale Removed Op", "Other / Retail Venue", 50.0, 5.0, 10.0),
    ]
    merged = merge_detail_results(existing, [oh_row])
    assert [(r.month, r.operator) for r in merged] == [
        ("2025-02", "Old Op"),
        ("2025-03", "Belterra Park - FanDuel"),
    ]

    details_dir = tmp_path / "details"
    oh_csv = write_state_detail_csv(str(details_dir), "ohio", merged)
    all_csv = write_combined_detail_csv(str(details_dir), merged)
    assert oh_csv.endswith("ohio.csv") and all_csv.endswith("all.csv")
    reloaded = read_detail_csv(oh_csv)
    assert len(reloaded) == 2
    assert reloaded[1].operator == "Belterra Park - FanDuel"
    header_line = (details_dir / "ohio.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header_line.split(",") == list(DETAIL_CSV_HEADERS)


def test_index_html_has_entity_dropdown_and_detail_columns() -> None:
    import json
    import re
    from publish.site import render_index_html

    html = render_index_html()
    assert 'id="entitySelect"' in html
    assert 'id="detailCsvLink"' in html
    assert 'id="kpiRowsLabel"' in html
    assert 'data-col="operator"' in html
    assert 'data-col="brand"' in html
    assert "Not Disclosed by State Regulator" in html

    m = re.search(r"const DETAIL_STATES = new Set\((\[.*?\])\);", html)
    assert m is not None
    detail_states = set(json.loads(m.group(1)))
    assert detail_states == {
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "missouri",
        "ohio",
        "pennsylvania",
    }
    assert "nevada" not in detail_states


def test_indiana_spacer_columns_and_missouri_hyphen_spacing_regressions() -> None:
    import pytest
    openpyxl = pytest.importorskip("openpyxl")
    from statewager.states import indiana, missouri

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet7"
    ws.append(["INDIANA GAMING COMMISSION"])
    ws.append(["SPORTS WAGERING AGR", "Handle", None, "Taxable AGR*", None, "Sports Wagering Tax"])
    ws.append(["Ameristar Casino", 131476210.78, None, 16056131.55, None, 1525332.47])
    ws.append(["TOTAL", 131476210.78, None, 16056131.55, None, 1525332.47])
    locs = indiana.parse_location_totals(wb)
    assert locs == {"Ameristar Casino": (131476210.78, 16056131.55)}
    wb.close()

    mo_page = "\n".join([
        "SPORTS WAGERING MOBILE STATISTICAL REPORT",
        "PENN SPORTS INTERACTIVE- MOBILE Dec-25 264,479 $6,253,851.56 $6,026,261.76 $227,589.80 $22,758.98",
        "Jan-26 265,726 $6,722,817.56 $6,577,175.43 $145,642.13 $14,564.21",
        "Feb-26 164,600 $5,141,463.54 $4,875,544.90 $265,918.64 $26,591.86",
        "TOTALS: 694,805 $18,118,132.66 $17,478,982.09 $639,150.57 $63,915.06",
    ])
    mo_details = missouri.parse_report_details([mo_page], 2026, 2)
    assert len(mo_details) == 1
    assert mo_details[0][0] == "PENN SPORTS INTERACTIVE"
    assert round(mo_details[0][1], 2) == 5141463.54
    assert round(mo_details[0][2], 2) == 265918.64


def test_fiscal_csv_roundtrip_and_legacy_compatibility(tmp_path: pathlib.Path) -> None:
    from publish.csv_writer import (
        CSV_HEADERS,
        DETAIL_CSV_HEADERS,
        extract_published_details,
        read_detail_csv,
        read_state_csv,
        write_state_csv,
        write_state_detail_csv,
    )
    from statewager.model import CORE_RESULT_FIELDS, OperatorMonthResult, finalize_state_month

    assert len(CORE_RESULT_FIELDS) == 12
    assert len(CSV_HEADERS) == 15
    assert CSV_HEADERS[-3:] == ["gross_revenue", "promo_deductions", "tax"]
    assert len(DETAIL_CSV_HEADERS) == 17
    assert DETAIL_CSV_HEADERS[-3:] == ["gross_revenue", "promo_deductions", "tax"]

    state_res = finalize_state_month(
        state="Ohio",
        iso_month="2025-03",
        handle=1000.0,
        revenue=100.0,
        gross_revenue=100.0,
        promo_deductions=25.0,
        tax=20.0,
        source_url="https://example.com/oh.xlsx",
        details=[
            OperatorMonthResult(
                "2025-03",
                "BELTERRA PARK - FANDUEL",
                600.0,
                60.0,
                10.0,
                mobile_handle=600.0,
                mobile_revenue=60.0,
                gross_revenue=60.0,
                promo_deductions=15.0,
                tax=12.0,
            ),
            OperatorMonthResult(
                "2025-03",
                "Belterra Park - FanDuel",
                400.0,
                40.0,
                10.0,
                retail_handle=400.0,
                retail_revenue=40.0,
                gross_revenue=40.0,
                promo_deductions=10.0,
                tax=8.0,
            ),
        ],
    )
    assert state_res is not None
    state_csv = write_state_csv(str(tmp_path), "ohio", [state_res])
    loaded_state = read_state_csv(state_csv)
    assert loaded_state[0].gross_revenue == 100.0
    assert loaded_state[0].promo_deductions == 25.0
    assert loaded_state[0].tax == 20.0

    details = extract_published_details([state_res])
    assert len(details) == 1
    assert details[0].gross_revenue == 100.0
    assert details[0].promo_deductions == 25.0
    assert details[0].tax == 20.0
    detail_csv = write_state_detail_csv(str(tmp_path / "details"), "ohio", details)
    loaded_detail = read_detail_csv(detail_csv)
    assert loaded_detail[0].gross_revenue == 100.0
    assert loaded_detail[0].promo_deductions == 25.0
    assert loaded_detail[0].tax == 20.0

    # Legacy 12-col state & 14-col detail CSVs still load cleanly with None fiscal fields.
    legacy_state = tmp_path / "legacy_state.csv"
    legacy_state.write_text(
        "state,month,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url\n"
        "Nevada,2025-03,500,50,10,100,10,10,400,40,10,https://example.com\n",
        encoding="utf-8",
    )
    r_leg = read_state_csv(str(legacy_state))[0]
    assert r_leg.gross_revenue is None and r_leg.promo_deductions is None and r_leg.tax is None

    legacy_detail = tmp_path / "legacy_detail.csv"
    legacy_detail.write_text(
        "state,month,operator,brand,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url\n"
        "Ohio,2025-03,Jack Cleveland - BetJack,BetJack,500,50,10,,,,,,,https://example.com\n",
        encoding="utf-8",
    )
    d_leg = read_detail_csv(str(legacy_detail))[0]
    assert d_leg.gross_revenue is None and d_leg.promo_deductions is None and d_leg.tax is None


def test_state_fiscal_extractors(monkeypatch) -> None:
    import io
    import pytest
    openpyxl = pytest.importorskip("openpyxl")
    from statewager.states import illinois, indiana, iowa, kansas, louisiana, missouri, ohio, pennsylvania

    # 1. Pennsylvania summary + operator fiscal block
    pa_rows = [
        ("GRAND TOTAL", None),
        ("Total Sports Wagering", None),
        ("Handle*", 1000.0),
        ("Revenue", 120.0),
        ("Promotional Credits", 30.0),
        ("Gross Revenue (Taxable)", 90.0),
        ("State Tax Due (34%)", 30.6),
        ("Local Share Assessment (2%)", 1.8),
        ("Valley Forge Casino", None),
        ("Total Sports Wagering", None),
        ("Handle*", 700.0),
        ("Revenue", 80.0),
        ("Promotional Credits", 20.0),
        ("Gross Revenue (Taxable)", 60.0),
        ("State Tax Due (34%)", 20.4),
        ("Local Share Assessment (2%)", 1.2),
    ] + [("", None)] * 15
    col_map = {1: "Mar 2026"}
    assert pennsylvania.parse_sheet_fiscal(pa_rows, col_map)["Mar 2026"] == (120.0, 30.0, pytest.approx(32.4))
    op_pa = pennsylvania.parse_detail_fiscal(pa_rows, col_map)[("Mar 2026", "Valley Forge Casino")]
    assert op_pa == (80.0, 20.0, pytest.approx(21.6))

    # 2. Ohio monthly fiscal extraction
    wb_oh = openpyxl.Workbook()
    ws_oh = wb_oh.active
    ws_oh.append(["Type A Proprietors - Online Sports Gaming"])
    ws_oh.append(["Online Proprietor / Services Provider", "Handle", "Prizes", "Void", "Promotional", "Revenue"])
    ws_oh.append(["Belterra Park - FanDuel", 600.0, 520.0, 0.0, 15.0, 80.0])
    ws_oh.append(["Subtotal Type A Online", 600.0, 520.0, 0.0, 15.0, 80.0])
    ws_oh.append(["Type B Proprietors - Retail Sports Gaming"])
    ws_oh.append(["Belterra Park - FanDuel", 100.0, 90.0, 0.0, 2.0, 10.0])
    ws_oh.append(["Subtotal Type B Retail", 100.0, 90.0, 0.0, 2.0, 10.0])
    ws_oh.append(["Total Online and Retail", 700.0, 610.0, 0.0, 17.0, 90.0])
    parsed_oh = ohio._parse_month_sheet_with_fiscal(ws_oh)
    assert parsed_oh is not None
    _core, oh_gross, oh_promo, oh_det_f = parsed_oh
    assert oh_gross == 90.0 and oh_promo == 17.0
    assert oh_det_f[0][7:10] == (80.0, 15.0, None)
    wb_oh.close()

    # 3. Kansas summary (breaks at Fiscal Year table) + 7-token collapsed pdftotext detail
    monkeypatch.setattr(kansas, "pdfplumber", None)
    monkeypatch.setattr(
        kansas,
        "_extract_pdftotext_raw",
        lambda _d: "\n".join([
            "Kansas Crossing BetMGM $1,000,000 $90,000 $9,000",
            "TOTALS $1,000,000 $90,000 $9,000",
            "Fiscal Year Summary $99,999,999 $9,999,999 $999,999",
        ]),
    )
    ks_st_tax, ks_op_taxes = kansas.parse_summary_fiscal(b"%PDF")
    assert ks_st_tax == 9000.0
    assert ks_op_taxes["kansas crossing - betmgm"] == 9000.0

    monkeypatch.setattr(
        kansas,
        "_extract_pdftotext_raw",
        lambda _d: "\n".join([
            "Kansas Crossing BetMGM $1,000,000 $900,000 $20,000 $80,000 $0 $80,000 $8,000",
            "TOTALS $1,000,000 $900,000 $20,000 $80,000 $0 $80,000 $8,000",
        ]),
    )
    ks_det_st, ks_det_op = kansas.parse_detail_fiscal(b"%PDF", "Mar 2026")
    assert ks_det_st == (100000.0, 20000.0, 8000.0)
    assert ks_det_op["kansas crossing - betmgm"] == (100000.0, 20000.0, 8000.0)

    # 4. Louisiana workbook promo deduction & tax
    import datetime
    wb_la = openpyxl.Workbook()
    ws_la = wb_la.active
    ws_la.append(["Month", None, "Handle", "Promo Deduct.", "Net Proceeds", "Taxes Paid 1"])
    ws_la.append([datetime.date(2026, 3, 1), None, 1000.0, -30.0, 90.0, 13.5])
    buf_la = io.BytesIO()
    wb_la.save(buf_la)
    wb_la.close()
    assert louisiana.parse_workbook_fiscal_all_months(buf_la.getvalue())["Mar 2026"] == (30.0, 13.5)

    # 5. Indiana tax summary with spacer column + component tax allocation
    wb_in = openpyxl.Workbook()
    ws_in = wb_in.active
    ws_in.title = "7 SW Tax Summary"
    ws_in.append(["SPORTS WAGERING TAX", "Handle", None, "Taxable AGR", None, "Sports Wagering Tax"])
    ws_in.append(["Ameristar Casino", 1000.0, None, 100.0, None, 9.5])
    ws_in.append(["TOTAL", 1000.0, None, 100.0, None, 9.5])
    tot_in_tax, loc_in_taxes = indiana.parse_tax_summary(wb_in)
    assert tot_in_tax == 9.5 and loc_in_taxes["Ameristar Casino"] == 9.5
    alloc = indiana._allocate_component_tax([75.0, 25.0], 10.0)
    assert alloc == [pytest.approx(7.5), pytest.approx(2.5)]
    wb_in.close()

    # 6. Missouri detail gross revenue + statistical tax (ignoring summary section)
    mo_pages = [
        "SPORTS WAGERING STATISTICAL REPORT SUMMARY\nSTATE TOTALS: 10 $999.00 $888.00 $111.00 $99.00",
        "SPORTS WAGERING MOBILE STATISTICAL REPORT\n"
        "BETMGM - MOBILE Mar-26 100 $1,000.00 $900.00 $100.00 $10.00\n"
        "STATE TOTALS MTD: 100 $1,000.00 $900.00 $100.00 $10.00",
        "SPORTS WAGERING MOBILE DETAIL REPORT\n"
        "STATE TOTALS MTD: 100 $1,000.00 $880.00 $120.00 $20.00 $100.00",
    ]
    mo_gross, mo_tax, mo_ops = missouri.parse_page_texts_fiscal(mo_pages, 2026, 3)
    assert mo_gross == 120.0 and mo_tax == 10.0
    assert mo_ops[missouri.detail_operator_key("BETMGM")] == 10.0

    # 7. Iowa labeled totals STATE TAX extraction
    ia_block = (
        "TOTALS\n"
        "SPORTS WAGERING NET RECEIPTS $100,000\n"
        "SPORTS WAGERING HANDLE $1,000,000\n"
        "STATE TAX $6,750\n"
    )
    ia_parsed = iowa.parse_totals_block(ia_block)
    assert ia_parsed is not None and ia_parsed.tax == 6750.0

    # 8. Illinois accrual tax extraction
    il_csv = (
        "Location Type,Licensee,State AGR,State Tax,Cook County Tax,Sports Wager Tax\n"
        "Online Wagering,Book A,100.00,20.00,0.00,5.00\n"
        "In-Person Wagering,Casino B,50.00,10.00,1.00,\n"
    ).encode("utf-8")
    il_st, il_ops = illinois.parse_accrual_tax(il_csv)
    assert il_st == 36.0 and il_ops == {"Book A": 25.0, "Casino B": 11.0}
    assert illinois.parse_accrual_tax(b"Location Type,Licensee,State AGR\nOnline Wagering,A,10\n") == (None, {})


def test_publish_all_partial_outage_resilience(tmp_path: pathlib.Path, monkeypatch) -> None:
    import pytest
    from publish.__main__ import publish_all
    from publish.csv_writer import (
        PublishedDetailRow,
        read_detail_csv,
        read_state_csv,
        write_state_csv,
        write_state_detail_csv,
    )
    from statewager.model import StateMonthResult
    from statewager.states import nevada, ohio

    docs_dir = tmp_path / "docs"
    data_dir = docs_dir / "data"
    details_dir = data_dir / "details"
    existing_oh = StateMonthResult(
        "Ohio", "2026-02", 500.0, 50.0, 10.0, None, None, None, None, None, None, "u0", 50.0, 5.0, None,
    )
    write_state_csv(str(data_dir), "ohio", [existing_oh])
    write_state_detail_csv(
        str(details_dir),
        "ohio",
        [PublishedDetailRow("Ohio", "2026-02", "Belterra Park - FanDuel", "FanDuel", 500.0, 50.0, 10.0)],
    )

    monkeypatch.setattr(ohio, "fetch", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("OH outage")))
    monkeypatch.setattr(
        nevada,
        "fetch",
        lambda *_a, **_kw: [
            StateMonthResult(
                "Nevada", "2026-03", 800.0, 60.0, 7.5, None, None, None, None, None, None, "https://nv.gov",
            )
        ],
    )

    by_state = publish_all(
        month_start="2026-03",
        month_end="2026-03",
        states=["ohio", "nevada"],
        cache_dir=str(tmp_path / "cache"),
        docs_dir=str(docs_dir),
        merge_existing=True,
        quiet=True,
        jobs=2,
    )
    assert [r.month for r in by_state["ohio"]] == ["2026-02"]
    assert [r.month for r in by_state["nevada"]] == ["2026-03"]
    assert [r.month for r in read_state_csv(str(data_dir / "ohio.csv"))] == ["2026-02"]
    assert len(read_detail_csv(str(details_dir / "ohio.csv"))) == 1
    combined = read_state_csv(str(data_dir / "all.csv"))
    assert {(r.state, r.month) for r in combined} == {("Ohio", "2026-02"), ("Nevada", "2026-03")}

    with pytest.raises(RuntimeError):
        publish_all(
            month_start="2026-03",
            month_end="2026-03",
            states=["ohio"],
            cache_dir=str(tmp_path / "cache"),
            docs_dir=str(docs_dir),
            merge_existing=False,
            quiet=True,
        )

    monkeypatch.setattr(nevada, "fetch", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("NV outage")))
    with pytest.raises(RuntimeError, match="All 2 state refreshes failed"):
        publish_all(
            month_start="2026-03",
            month_end="2026-03",
            states=["ohio", "nevada"],
            cache_dir=str(tmp_path / "cache"),
            docs_dir=str(docs_dir),
            merge_existing=True,
            quiet=True,
        )


def test_viewer_html_features_and_workflow_file() -> None:
    from publish.site import render_index_html

    html = render_index_html()
    for marker in (
        'id="freshnessBar"',
        'id="windowSelect"',
        'id="compareBtn"',
        'value="handle_share"',
        'value="revenue_share"',
        'value="yoy_handle"',
        'value="yoy_revenue"',
        'value="yoy_hold_bps"',
        'data-col="gross_hold"',
        'data-col="promo_deductions"',
        'id="exportViewBtn"',
        'id="copyLinkBtn"',
        "URLSearchParams",
        "buildWindowedRows",
        "computeSameStoreYoY",
    ):
        assert marker in html

    workflow_path = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "refresh-data.yml"
    if workflow_path.exists():
        wf = workflow_path.read_text(encoding="utf-8")
        assert "17 13 */2 * *" in wf
        assert "workflow_dispatch" in wf
        assert "python -m publish" in wf



