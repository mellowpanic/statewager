"""Unit tests for statewager core and publish static exporter."""
from __future__ import annotations

import csv
import datetime
import functools
import inspect
import io
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import zipfile

import pytest
import requests

import publish.__main__ as pub_main
from publish import (
    CSV_HEADERS,
    merge_state_results,
    read_state_csv,
    render_index_html,
    write_combined_csv,
    write_index_html,
    write_state_csv,
)
from publish.__main__ import main as publish_main, publish_all
from publish.csv_writer import (
    DETAIL_CSV_HEADERS,
    PublishedDetailRow,
    _parse_opt_float,
    extract_published_details,
    merge_detail_results,
    read_detail_csv,
    write_combined_detail_csv,
    write_state_detail_csv,
    write_status_json,
)
from publish.site import _safe_inline_json
from statewager import StateMonthResult, http as sw_http, quality
from statewager.cache import (
    cache_path,
    find_local_month_file,
    load_cache,
    load_cached_or_fetch,
    recent_cache_ttl,
    recent_fy_cache_ttl,
    refresh_cached_if_stale,
    try_save_cache,
)
from statewager.discover import (
    AnchorParser,
    classify_cumulative_filename,
    classify_sportsbook_filename,
    extract_month_label,
    normalize_filename,
    parse_text_month,
)
from statewager.http import _matches_signature
from statewager.model import (
    CORE_RESULT_FIELDS,
    OperatorMonthResult,
    finalize_state_month,
    find_stale_months,
    fiscal_year_july,
    format_month_label,
    hold_pct,
    label_to_iso_month,
    month_yyyymm,
    parse_month_spec,
    reconcile_components,
    resolve_fetch_months,
    safe_float,
)
from statewager.operators import BRAND_ORDER, classify_operator_brand, normalize_operator_name
from statewager.parse import extract_pdf_text, open_xlsx_workbook, parse_dollar
from statewager.states import (
    ALL_STATES,
    STATE_ABBR_TO_SLUG,
    illinois,
    indiana,
    iowa,
    kansas,
    load_state_module,
    louisiana,
    mississippi,
    missouri,
    nevada,
    ohio,
    pennsylvania,
)

_EXPECTED_DETAIL_SLUGS = {
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "missouri",
    "ohio",
    "pennsylvania",
}


def _state_row(
    state: str,
    month: str,
    handle: float,
    revenue: float,
    hold: float | None = None,
    source_url: str | None = None,
    **extra: object,
) -> StateMonthResult:
    return StateMonthResult(
        state=state,
        month=month,
        handle=handle,
        revenue=revenue,
        hold=hold_pct(handle, revenue) if hold is None else hold,
        retail_handle=None,
        retail_revenue=None,
        retail_hold=None,
        mobile_handle=None,
        mobile_revenue=None,
        mobile_hold=None,
        source_url=source_url,
        **extra,
    )


@functools.lru_cache(maxsize=1)
def _default_html() -> str:
    return render_index_html()


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
    old_dec = _state_row("Ohio", "2024-12", 500.0, 50.0, source_url="u0")
    old_mar = _state_row("Ohio", "2025-03", 600.0, 60.0, source_url="u1")
    csv_path = write_state_csv(str(data_dir), "ohio", [old_dec, old_mar])

    existing = read_state_csv(csv_path)
    fresh_mar = _state_row("Ohio", "2025-03", 750.0, 75.0, source_url="u2")
    fresh_apr = _state_row("Ohio", "2025-04", 700.0, 70.0, source_url="u3")
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
    html_text = _default_html()
    assert "includePartial" in html_text
    assert "expectedStatesForMonth" in html_text
    month_m = re.search(r'<th[^>]*data-col="month"[^>]*>Month</th>', html_text)
    state_m = re.search(r'<th[^>]*data-col="state"[^>]*>State</th>', html_text)
    assert month_m is not None and state_m is not None
    assert month_m.start() < state_m.start()


def test_missouri_pdf_name_re_matches_underscores_and_revised() -> None:
    m1 = missouri.PDF_NAME_RE.search("FY26_SWFinReport/03_Mar/SW_Monthly_Financials_0326_revised.pdf")
    m2 = missouri.PDF_NAME_RE.search("FY27_SWFinReport/09_Sep/SW Monthly Financials 0926.pdf")
    assert m1 is not None and m1.group(1) == "0326"
    assert m2 is not None and m2.group(1) == "0926"


def test_cache_path_traversal_zero_byte_and_stale_refresh(tmp_path) -> None:
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
    assert classify_operator_brand("HOLLYWOOD - THE SCORE", state="Missouri") == "PENN / ESPN Bet"
    assert classify_operator_brand("HOLLYWOOD - THESCORE", state="Missouri") == "PENN / ESPN Bet"
    assert classify_operator_brand("Valley Forge Casino", state="Pennsylvania") == "FanDuel"
    assert BRAND_ORDER[0] == "DraftKings"
    assert BRAND_ORDER[-1] == "Other / Retail Venue"


def test_seven_states_expose_has_details() -> None:
    detail_slugs = {
        slug for slug in ALL_STATES if getattr(load_state_module(slug), "HAS_DETAILS", False)
    }
    assert detail_slugs == _EXPECTED_DETAIL_SLUGS


def test_detail_csv_extraction_dedup_merge_and_write(tmp_path) -> None:
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
    html = _default_html()
    assert 'id="entitySelect"' in html
    assert 'id="detailCsvLink"' in html
    assert 'id="kpiRowsLabel"' in html
    assert 'data-col="operator"' in html
    assert 'data-col="brand"' in html
    assert "Not Disclosed by State Regulator" in html

    m = re.search(r"const DETAIL_STATES = new Set\((\[.*?\])\);", html)
    assert m is not None
    detail_states = set(json.loads(m.group(1)))
    assert detail_states == _EXPECTED_DETAIL_SLUGS
    assert "nevada" not in detail_states


def test_indiana_spacer_columns_and_missouri_hyphen_spacing_regressions() -> None:
    openpyxl = pytest.importorskip("openpyxl")

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
    openpyxl = pytest.importorskip("openpyxl")

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
    docs_dir = tmp_path / "docs"
    data_dir = docs_dir / "data"
    details_dir = data_dir / "details"
    existing_oh = _state_row("Ohio", "2026-02", 500.0, 50.0, source_url="u0", gross_revenue=50.0, promo_deductions=5.0)
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
        lambda *_a, **_kw: [_state_row("Nevada", "2026-03", 800.0, 60.0, source_url="https://nv.gov")],
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
    html = _default_html()
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
        "showTooltip",
        "renderEmptyChart",
        "aria-sort",
        "windowedDetailRows",
    ):
        assert marker in html

    workflow_path = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "refresh-data.yml"
    if workflow_path.exists():
        wf = workflow_path.read_text(encoding="utf-8")
        assert "23 6,18 * * *" in wf
        assert "workflow_dispatch" in wf
        assert "actions/checkout@v7" in wf
        assert "actions/setup-python@v7" in wf
        assert "--jobs 10" in wf
        assert "--recheck-months 2" in wf
        assert "python -m publish" in wf
        ci_path = workflow_path.parent / "ci.yml"
        assert ci_path.exists()
        ci_wf = ci_path.read_text(encoding="utf-8")
        assert "pytest -v" in ci_wf
        assert "docs/data/**" in ci_wf
        assert "actions/checkout@v7" in ci_wf


def test_critique_core_fixes_and_single_pass_parsers(tmp_path: pathlib.Path, monkeypatch) -> None:
    # parse_month_spec validation & variant formats
    assert parse_month_spec("Mar-2026") == datetime.date(2026, 3, 1)
    assert parse_month_spec("2026-03-15") == datetime.date(2026, 3, 1)
    with pytest.raises(ValueError):
        parse_month_spec("2026-03-foo")

    # All 10 two-letter postal codes resolve cleanly
    assert len(STATE_ABBR_TO_SLUG) == 10
    for abbr, slug in STATE_ABBR_TO_SLUG.items():
        mod = load_state_module(abbr)
        assert mod.__name__.endswith(f".{slug}")

    # Operator normalization & Barstool/The Score brand classification
    assert normalize_operator_name("FRENCH LICK RESORT - GETSBK") == "French Lick Resort - getSBK"
    assert classify_operator_brand("Hollywood Gaming at Dayton Raceway - Barstool", state="Ohio") == "PENN / ESPN Bet"
    assert classify_operator_brand("HOLLYWOOD - THE SCORE", state="Missouri") == "PENN / ESPN Bet"

    # AnchorParser flushes unclosed trailing anchor on close()
    parser = AnchorParser()
    parser.feed('<a href="/report.pdf">March 2026')
    parser.close()
    assert len(parser.links) == 1 and parser.links[0]["href"] == "/report.pdf"

    # Louisiana _scan_cache_dir indexes remote-named cached files as cumulative fallbacks
    cache_dir = tmp_path / "la_cache"
    cache_dir.mkdir()
    cached_fy = cache_dir / "Sep_2026-mobile_fy27_mobile_sb_2026_09.xlsx"
    cached_fy.write_bytes(b"PK")
    _pm, cum = louisiana._scan_cache_dir(str(cache_dir), {"Jul 2026", "Sep 2026"})
    assert str(cached_fy) in cum.get("Jul 2026:mobile", [])

    # Missouri Detail Report per-operator gross revenue & alias key alignment
    mo_pages = [
        "SPORTS WAGERING RETAIL STATISTICAL REPORT\n"
        "CAESAR'S SPORTSBOOK-HARRAH'S - RETAIL Sep-26 100 $1,000.00 $900.00 $100.00 $10.00\n"
        "FANATICS-AMERISTAR KC - RETAIL Sep-26 100 $2,000.00 $1,800.00 $200.00 $20.00\n"
        "STATE TOTALS MTD: 200 $3,000.00 $2,700.00 $300.00 $30.00",
        "SPORTS WAGERING RETAIL DETAIL REPORT\n"
        "CAESARS SPORTSBOOK- HARRAH'S- RETAIL Sep-26\n"
        "Football 100 $1,000.00 $880.00 $120.00\n"
        "TOTALS: 100 $1,000.00 $880.00 $120.00\n"
        "FANATICS SPORTSBOOK- AMERISTAR KC - RETAIL Sep-26\n"
        "Football 100 $2,000.00 $1,750.00 $250.00\n"
        "TOTALS: 100 $2,000.00 $1,750.00 $250.00\n"
        "STATE TOTALS MTD: 200 $3,000.00 $2,630.00 $370.00",
    ]
    st_g, st_t, op_t, op_g = missouri._parse_page_texts_fiscal_full(mo_pages, 2026, 9)
    assert st_g == 370.0 and st_t == 30.0
    k_caesars = missouri._fiscal_operator_key("CAESAR'S SPORTSBOOK-HARRAH'S")
    k_fanatics = missouri._fiscal_operator_key("FANATICS-AMERISTAR KC")
    assert op_t[k_caesars] == 10.0 and op_g[k_caesars] == 120.0
    assert op_t[k_fanatics] == 20.0 and op_g[k_fanatics] == 250.0

    # Iowa fetch populates operator gross_revenue equal to net receipts
    fake_ia = iowa.ParsedIowaMetrics(
        metrics=(1000.0, 100.0, 200.0, 20.0, 800.0, 80.0),
        layout="test",
        details=(("Crown IA Gaming, LLC", 800.0, 80.0, None, None, 800.0, 80.0),),
        tax=6.75,
    )
    monkeypatch.setattr(iowa, "build_archive_lookup", lambda *_a, **_kw: {"Sep 2026": fake_ia})
    ia_res = iowa.fetch("2026-09", "2026-09", offline=True, log=lambda _m: None)
    assert len(ia_res) == 1
    assert ia_res[0].details[0].gross_revenue == 80.0

    # CSV malformed float defense & publish CLI validation
    assert _parse_opt_float("not-a-number") is None
    with pytest.raises(SystemExit):
        publish_main(["--months", "0"])
    with pytest.raises(SystemExit):
        publish_main(["--start-month", "2026-05", "--end-month", "2026-03"])


def test_adversarial_reviews_regressions(tmp_path: pathlib.Path, monkeypatch) -> None:
    # 1. cache_path rejects "." and symlink escapes while accepting clean subpaths
    cache_root = tmp_path / "cache_root"
    cache_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        cache_path(str(cache_root), ".")
    escape_link = cache_root / "escape"
    os.symlink(str(outside), str(escape_link))
    with pytest.raises(ValueError):
        cache_path(str(cache_root), "escape/secret.pdf")
    assert cache_path(str(cache_root), "sub/ok.pdf") == os.path.join(os.path.abspath(str(cache_root)), "sub", "ok.pdf")

    # recent_cache_ttl / recent_fy_cache_ttl handle year rollover cleanly
    today = datetime.date.today()
    prev_month = (today.replace(day=1) - datetime.timedelta(days=1))
    assert recent_cache_ttl(prev_month.year, prev_month.month) is not None
    assert recent_cache_ttl(2023, 1) is None
    assert recent_fy_cache_ttl(today.year + 1) is not None
    assert recent_fy_cache_ttl(2023) is None

    # 2. http: fetch_bytes closes response on 404; Wayback guards null JSON snapshots
    closed = {"count": 0}

    class FakeResp:
        def __init__(self, status_code: int, content: bytes = b"") -> None:
            self.status_code = status_code
            self.content = content

        def close(self) -> None:
            closed["count"] += 1

    monkeypatch.setattr(sw_http, "retry_get", lambda *_a, **_kw: FakeResp(404))
    assert sw_http.fetch_bytes("https://example.test/missing.pdf") is None
    assert closed["count"] == 1
    monkeypatch.setattr(sw_http, "fetch_text", lambda *_a, **_kw: json.dumps({"archived_snapshots": None}))
    assert sw_http._fetch_wayback_snapshot("https://example.test/old.pdf") is None

    # 3. model: hold_pct rejects <= 0 handle; finalize_state_month blanks non-reconciling splits on zero total
    assert hold_pct(-100.0, -50.0) is None
    assert hold_pct(0.0, 10.0) is None
    zero_split = finalize_state_month(
        state="Ohio",
        iso_month="2026-01",
        handle=100.0,
        revenue=0.0,
        retail_handle=50.0,
        retail_revenue=5.0,
        mobile_handle=50.0,
        mobile_revenue=5.0,
        strict_split_pct=0.05,
    )
    assert zero_split is not None
    assert zero_split.retail_handle is None and zero_split.mobile_handle is None

    # 4. operators & state parsers
    assert normalize_operator_name("KS Crossing-DraftKings", state="Kansas") == "Kansas Crossing - DraftKings"
    assert normalize_operator_name("HOLLYWOOD - THE SCORE", state="Missouri") == "Hollywood - theScore Bet"

    adj = indiana._allocate_component_adjustments([100.0, -100.0], 20.0)
    assert round(sum(adj), 6) == 20.0
    assert indiana._allocate_component_tax([100.0, 200.0], 0.0) == [0.0, 0.0]
    neg_tail_tax = indiana._allocate_component_tax([1000.0, 1000.0, 1.0], 0.03)
    assert neg_tail_tax is not None and all(v is not None and v >= 0 for v in neg_tail_tax)
    assert round(sum(v for v in neg_tail_tax if v is not None), 2) == 0.03

    assert iowa.MONEY_RE.findall("-$1,234.56 \u2212$9.50") == ["-$1,234.56", "\u2212$9.50"]
    assert kansas._extract_handle_and_revenue_from_line("Casino $1,234.00 -") == (1234.0, 0.0)
    assert nevada._metrics_from_win(100.0, 0.0) is None
    assert nevada._metrics_from_win(100.0, -5.0) is None
    assert nevada.reconcile_county_details([("Clark County", 10.0, -5.0)], statewide_revenue=-10.0) == [("Clark County", 10.0, -5.0)]
    assert pennsylvania.parse_month_columns([("Operator", "July", "July YTD", "January", "Total")], 2025, 2026) == {
        1: "Jul 2025",
        3: "Jan 2026",
    }

    # 5. publish CSV hardening, lossless detail hold, merge_detail_results, --no-merge --states, CLI validation
    bom_csv = tmp_path / "bom_detail.csv"
    bom_csv.write_bytes(
        "\ufeffstate,month,operator,brand,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,"
        "mobile_handle,mobile_revenue,mobile_hold,gross_revenue,promo_deductions,tax,source_url\n"
        "Ohio,2026-01,Test Op,Other / Retail Venue,0,0,0,0,0,0,0,0,0,,,,https://oh.gov/x\n".encode("utf-8")
    )
    loaded_det = read_detail_csv(str(bom_csv))
    assert len(loaded_det) == 1 and loaded_det[0].hold == 0.0 and loaded_det[0].retail_hold == 0.0

    # Preserves distinct published hold instead of recomputing if single row has rounded hold
    single_det = PublishedDetailRow(
        state="Ohio", month="2026-01", operator="Op A", brand="DraftKings",
        handle=1000.0, revenue=123.45, hold=12.40,
        retail_handle=200.0, retail_revenue=20.0, retail_hold=9.99,
        mobile_handle=800.0, mobile_revenue=103.45, mobile_hold=12.95,
    )
    det_dir = tmp_path / "det"
    det_csv = write_state_detail_csv(str(det_dir), "ohio", [single_det])
    roundtrip = read_detail_csv(det_csv)
    assert len(roundtrip) == 1 and roundtrip[0].hold == 12.40 and roundtrip[0].retail_hold == 9.99

    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid CSV header"):
        read_state_csv(str(bad_csv))

    merged_det = merge_detail_results([single_det], [], refreshed_months=[("Ohio", "2026-01")])
    assert merged_det == []

    # publish_all: --no-merge keeps existing ohio (when 0 rows for one state) + untouched nevada
    docs_dir = tmp_path / "docs"
    data_dir = docs_dir / "data"
    write_state_csv(str(data_dir), "nevada", [_state_row("Nevada", "2026-02", 500.0, 50.0)])
    write_state_csv(str(data_dir), "ohio", [_state_row("Ohio", "2026-02", 600.0, 60.0)])
    monkeypatch.setattr(ohio, "fetch", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        missouri,
        "fetch",
        lambda *_a, **_kw: [_state_row("Missouri", "2026-02", 400.0, 40.0)],
    )
    res_map = pub_main.publish_all(
        month_start="2026-02",
        month_end="2026-02",
        states=["ohio", "missouri"],
        cache_dir=str(tmp_path / "c"),
        docs_dir=str(docs_dir),
        merge_existing=False,
        quiet=True,
    )
    assert [r.month for r in res_map["ohio"]] == ["2026-02"]
    assert {(r.state, r.month) for r in read_state_csv(str(data_dir / "all.csv"))} == {
        ("Missouri", "2026-02"),
        ("Nevada", "2026-02"),
        ("Ohio", "2026-02"),
    }

    with pytest.raises(RuntimeError, match="All 1 state refreshes returned 0 rows"):
        pub_main.publish_all(
            month_start="2026-02",
            month_end="2026-02",
            states=["ohio"],
            cache_dir=str(tmp_path / "c2"),
            docs_dir=str(tmp_path / "empty_docs"),
            merge_existing=False,
            quiet=True,
        )

    # CLI validation: invalid --states & --months + --end-month
    with pytest.raises(SystemExit):
        pub_main.main(["--states", "atlantis"])
    captured = {}

    def _fake_publish_all(**kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(pub_main, "publish_all", _fake_publish_all)
    assert pub_main.main(["--months", "3", "--end-month", "2026-03", "--docs-dir", str(docs_dir), "--quiet"]) == 0
    assert captured["month_start"] == datetime.date(2026, 1, 1)
    assert captured["month_end"] == datetime.date(2026, 3, 1)

    # 6. viewer inline JSON escaping & ARIA/safety markers
    escaped = _safe_inline_json("</script>&\u2028\u2029")
    assert "</" not in escaped and "&" not in escaped and "\\u2028" in escaped and "\\u2029" in escaped
    html = _default_html()
    assert "TABLE_COLUMNS.some((c) => c.key === sc)" in html
    assert 'aria-controls="comparePanel"' in html
    assert 'aria-live="polite"' in html
    assert 'scope="col"' in html
    assert "ensureNonZeroRange" in html
    assert "csvInFlight" in html

    # Pooled HTTP session uses a standard browser User-Agent for WAF-protected portals (e.g. KS Lottery)
    sw_http._THREAD_LOCAL.session = None
    sess = sw_http._get_session()
    assert sess.headers.get("User-Agent") == sw_http.DEFAULT_USER_AGENT
    assert "Chrome/" in sw_http.DEFAULT_USER_AGENT
    sess.close()
    sw_http._THREAD_LOCAL.session = None


def test_maintenance_status_json_and_recheck_months(tmp_path: pathlib.Path, monkeypatch) -> None:
    # Iowa FY archive missing sections log INFO for current FY and WARN for closed FY
    today = datetime.date.today()
    cur_fy_end = today.year + (1 if today.month >= 7 else 0)
    monkeypatch.setattr(iowa, "download_pdf", lambda *_a, **_kw: b"%PDF-1.4\n")
    monkeypatch.setattr(iowa, "_extract_iowa_text_and_detail_map", lambda *_a, **_kw: ("SPORTS WAGERING REVENUE REPORT", {}))
    ia_cur_logs: list[str] = []
    iowa.build_archive_lookup(
        [(8, cur_fy_end - 1, f"Aug {cur_fy_end - 1}")],
        None,
        log=ia_cur_logs.append,
        fy_lookup={(cur_fy_end - 1, cur_fy_end): 999001},
    )
    assert ia_cur_logs and all(not msg.startswith("WARN") for msg in ia_cur_logs)
    ia_old_logs: list[str] = []
    iowa.build_archive_lookup(
        [(8, 2022, "Aug 2022")],
        None,
        log=ia_old_logs.append,
        fy_lookup={(2022, 2023): 999002},
    )
    assert any(msg.startswith("WARN Iowa archive section not found") for msg in ia_old_logs)

    # --recheck-months incremental narrowing & status.json idempotency
    recheck_docs = tmp_path / "recheck_docs"
    recheck_data = recheck_docs / "data"
    write_state_csv(
        str(recheck_data),
        "nevada",
        [
            _state_row("Nevada", "2026-07", 100.0, 10.0),
            _state_row("Nevada", "2026-08", 110.0, 11.0),
            _state_row("Nevada", "2026-09", 120.0, 12.0),
        ],
    )
    seen_spans: list[tuple[object, object]] = []

    def _fake_nv_fetch(ms, me, **_kw):
        seen_spans.append((ms, me))
        return [_state_row("Nevada", "2026-09", 125.0, 12.5)]

    monkeypatch.setattr(nevada, "fetch", _fake_nv_fetch)
    pub_main.publish_all(
        states=["nevada"],
        month_start=datetime.date(2026, 7, 1),
        month_end=datetime.date(2026, 10, 1),
        cache_dir=str(tmp_path / "c3"),
        docs_dir=str(recheck_docs),
        merge_existing=True,
        quiet=True,
        recheck_months=2,
    )
    assert seen_spans[-1] == (datetime.date(2026, 9, 1), datetime.date(2026, 10, 1))
    status_path = recheck_data / "status.json"
    assert status_path.is_file()
    status_1 = json.loads(status_path.read_text(encoding="utf-8"))
    assert status_1["state_count"] == 1
    assert status_1["states"]["nevada"]["latest_month"] == "2026-09"
    assert len(status_1["data_hash"]) == 16

    # Rewriting status.json with unchanged CSV bytes must keep data_updated_at identical
    later = datetime.datetime(2027, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    nv_rows = read_state_csv(str(recheck_data / "nevada.csv"))
    write_status_json(str(recheck_data), nv_rows, [], [("nevada", "Nevada")], now_utc=later)
    status_2 = json.loads(status_path.read_text(encoding="utf-8"))
    assert status_2["data_updated_at"] == status_1["data_updated_at"]

    # When CSV bytes change, hash and data_updated_at update
    updated_rows = [_state_row("Nevada", "2026-10", 130.0, 13.0)]
    write_combined_csv(str(recheck_data), nv_rows + updated_rows)
    write_status_json(str(recheck_data), nv_rows + updated_rows, [], [("nevada", "Nevada")], now_utc=later)
    status_3 = json.loads(status_path.read_text(encoding="utf-8"))
    assert status_3["data_hash"] != status_1["data_hash"]
    assert status_3["data_updated_at"] == "2027-01-02T03:04:05Z"

    # Invalid --recheck-months is rejected by CLI parser
    with pytest.raises(SystemExit):
        pub_main.main(["--recheck-months", "0"])

    # Viewer HTML includes status.json recency hook & freshness badge
    html = _default_html()
    assert "data/status.json" in html
    assert "freshness-updated" in html
    assert "fetchStatusMeta" in html


def test_critique_r4_pa_retry_and_try_save_cache(tmp_path: pathlib.Path, monkeypatch) -> None:
    calls: list[str] = []

    def _fake_fetch_text(url, **_kw):
        calls.append(url)
        if url == pennsylvania.REVENUE_PAGE:
            raise RuntimeError("primary index down")
        return ""

    monkeypatch.setattr(pennsylvania, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(
        pennsylvania,
        "crawl_anchors",
        lambda url, **_kw: calls.append(f"crawl:{url}") or [
            {"href": "/sites/default/files/2026-09.xlsx", "title": "", "text": "Sep 2026"},
            {"href": pennsylvania._absolute_url("/sites/default/files/2026-09.xlsx"), "title": "", "text": "Dup"},
        ],
    )
    pa_logs: list[str] = []
    anchors = pennsylvania.crawl_revenue_index(log=pa_logs.append, target_fys={(2026, 2027)})
    assert calls[0] == pennsylvania.REVENUE_PAGE
    assert f"crawl:{pennsylvania.REVENUE_PAGE}" not in calls
    assert any("WARN could not fetch anchor index" in m for m in pa_logs)
    assert len(anchors) == 1

    # try_save_cache logs WARN and returns False when OSError occurs
    save_logs: list[str] = []
    monkeypatch.setattr("statewager.cache.save_cache", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full")))
    assert try_save_cache(str(tmp_path / "x.bin"), b"abc", log=save_logs.append) is False
    assert any("WARN could not write cache" in m for m in save_logs)


def test_adversarial_r1_core_security_and_edge_cases(tmp_path: pathlib.Path, monkeypatch) -> None:
    # 1. HTTP: Retry-After bounds/Inf, SSRF host & Wayback guards, non-retryable 403, bounded response, _matches_signature
    assert sw_http._resolve_retry_after("inf") is None
    assert sw_http._resolve_retry_after("-inf") is None
    assert sw_http._resolve_retry_after("86400") == sw_http._MAX_RETRY_AFTER_SECONDS
    assert sw_http._should_try_wayback("2026-13") is False

    for bad_url in ("file:///etc/passwd", "http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(ValueError):
            sw_http.retry_request("GET", bad_url)

    with pytest.raises(ValueError, match="Unsupported signature_check"):
        _matches_signature(b"%PDF", "https://example.com/a.pdf", "pdf")

    class _StreamResp:
        def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
            self.status_code = status_code
            self.headers = headers
            self._chunks = chunks
            self.closed = False

        def iter_content(self, chunk_size: int = 65536):
            del chunk_size
            yield from self._chunks

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

        def close(self) -> None:
            self.closed = True

    calls_403: list[str] = []

    class _FakeSession:
        def request(self, _m, u, **_kw):
            calls_403.append(u)
            return _StreamResp(403, {}, [])

    with pytest.raises(requests.HTTPError):
        sw_http.retry_request("GET", "https://example.com/forbidden", retries=3, session=_FakeSession())  # type: ignore[arg-type]
    assert len(calls_403) == 1

    monkeypatch.setattr(sw_http, "retry_get", lambda *_a, **_kw: _StreamResp(200, {}, [b"abc", b"defg"]))
    with pytest.raises(ValueError, match="exceeds limit"):
        sw_http.fetch_bytes("https://example.com/big", max_bytes=5)

    wb_urls: list[str] = []

    def _fake_wb_text(_u, **_kw):
        return json.dumps({"archived_snapshots": {"closest": {"available": False, "url": "http://127.0.0.1/"}}})

    monkeypatch.setattr(sw_http, "fetch_text", _fake_wb_text)
    assert sw_http._fetch_wayback_snapshot("https://example.com/r.pdf") is None

    monkeypatch.setattr(
        sw_http,
        "fetch_text",
        lambda *_a, **_kw: json.dumps({"archived_snapshots": {"closest": {"available": True, "url": "http://169.254.169.254/x"}}}),
    )
    assert sw_http._fetch_wayback_snapshot("https://example.com/r.pdf") is None

    monkeypatch.setattr(
        sw_http,
        "fetch_text",
        lambda *_a, **_kw: json.dumps({"archived_snapshots": {"closest": {"available": True, "url": "http://web.archive.org/web/202501/https://example.com/r.pdf"}}}),
    )
    monkeypatch.setattr(sw_http, "fetch_bytes", lambda u, **_kw: wb_urls.append(u) or b"%PDF-1.4")
    assert sw_http._fetch_wayback_snapshot("https://example.com/r.pdf") == b"%PDF-1.4"
    assert wb_urls == ["https://web.archive.org/web/202501/https://example.com/r.pdf"]

    # 2. Cache: self-symlink rejection, find_local_month_file word boundary, refresh_cached_if_stale mtime & OSError
    croot = tmp_path / "cache"
    croot.mkdir()
    os.symlink(".", str(croot / "self_link"))
    with pytest.raises(ValueError):
        cache_path(str(croot), "self_link")

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "summary_2025_02.pdf").write_bytes(b"%PDF")
    assert find_local_month_file(str(local_dir), 2025, 3, ".pdf") is None
    (local_dir / "march_2025_report.pdf").write_bytes(b"%PDF-MAR")
    matched_local = find_local_month_file(str(local_dir), 2025, 3, ".PDF")
    assert matched_local is not None and matched_local[1] == b"%PDF-MAR"

    stale_file = tmp_path / "stale.bin"
    stale_file.write_bytes(b"same")
    old_ts = time.time() - 7200
    os.utime(stale_file, (old_ts, old_ts))
    assert refresh_cached_if_stale(str(stale_file), b"same", max_age_seconds=60, fetcher=lambda: b"same") is None
    assert stale_file.stat().st_mtime > old_ts
    os.utime(stale_file, (old_ts, old_ts))
    monkeypatch.setattr("statewager.cache.save_cache", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("ro fs")))
    ref_logs: list[str] = []
    assert refresh_cached_if_stale(
        str(stale_file), b"same", max_age_seconds=60, fetcher=lambda: b"updated", log=ref_logs.append,
    ) == b"updated"
    assert any("WARN could not write cache" in m for m in ref_logs)

    # 3. Parse: XLSX ZIP bomb / invalid archive guard + PDF page range validation
    bomb_buf = io.BytesIO()
    with zipfile.ZipFile(bomb_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/big.xml", b"0" * (12 * 1024 * 1024))
    bomb_logs: list[str] = []
    with open_xlsx_workbook(bomb_buf.getvalue(), openpyxl_mod=object(), log=bomb_logs.append) as wb_none:
        assert wb_none is None
    assert any("compression ratio" in m for m in bomb_logs)
    with pytest.raises(ValueError):
        extract_pdf_text(b"%PDF", first_page=0)
    with pytest.raises(ValueError):
        extract_pdf_text(b"%PDF", first_page=3, last_page=1)

    # 4. Discover: boundary fixes & per-month cumulative exclusion
    assert extract_month_label("2025_04_marketing.xlsx") == "Apr 2025"
    assert extract_month_label("report_904_2025.pdf") is None
    assert extract_month_label("report_03_202599.pdf") is None
    assert extract_month_label("03_12_2025.pdf") is None
    assert extract_month_label("dismay_2025.pdf") is None
    assert extract_month_label("2025_mayo.pdf") is None
    assert parse_text_month("September 202501") is None
    assert classify_cumulative_filename("fy24-25_mobile_sb_apr_2025.xlsx") is None

    # 5. Quality: Kansas vs Arkansas boundary, HTML entity unescape, NaN/Inf/zero-baseline, single-flight fetch
    sh_html = (
        "<h2>Arkansas Sports Betting</h2><table><tr><th>Month</th><th>Handle</th><th>Revenue</th></tr>"
        "<tr><td>Apr 2026</td><td>$1&nbsp;</td><td>$1</td></tr></table>"
        "<h2><span>Kansas</span> Sports Betting</h2><table><tr><th>Month</th><th>Handle</th><th>Revenue</th></tr>"
        "<tr><td>Apr 2026</td><td>&nbsp;$2,000,000</td><td>$200,000</td></tr></table>"
    )
    monkeypatch.setattr(quality, "fetch_text", lambda *_a, **_kw: sh_html)
    quality._SPORTSHANDLE_HTML_CACHE.clear()
    ks_sh = quality.query_aggregator_sportshandle("Kansas")
    assert ks_sh == {"Apr 2026": (2_000_000.0, 200_000.0)}
    fetch_calls = 0

    def _slow_loader() -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        time.sleep(0.02)
        return "ok"

    tmp_cache: dict[tuple[str, object], str] = {}
    barrier = threading.Barrier(4)
    out_vals: list[str | None] = []

    def _worker() -> None:
        barrier.wait()
        out_vals.append(quality._cached_source_fetch(tmp_cache, "https://example.test", _slow_loader, _slow_loader))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fetch_calls == 1 and out_vals == ["ok"] * 4

    monkeypatch.setattr(quality, "_aggregator_lookup", lambda *_a, **_kw: (100_000.0, 10_000.0))
    assert quality.validate_against_aggregators("Kansas", "Apr 2026", float("nan"), 10_000.0) is False
    assert quality.check_anomaly("Kansas", "Apr 2026", 10_000_000.0, 0.0, [(0.0, 0.0)] * 3) is False

    # 6. Operators & Model edge cases
    assert classify_operator_brand("Hard Rock Casino (Retail)", state="iowa") == "Other / Retail Venue"
    assert classify_operator_brand("Fairmount Park, Inc.", state="il") == "FanDuel"
    assert classify_operator_brand("HARRAH\u2019S HOOSIER PARK", state="Indiana") == "Caesars"
    assert normalize_operator_name("FAIRMOUNT PARK, INC.,") == "Fairmount Park, Inc.,"
    assert normalize_operator_name("CROWN IA GAMING LLC. - DRAFTKINGS") == "Crown IA Gaming LLC - DraftKings"
    assert normalize_operator_name("CASINO IN OHIO") == "Casino in Ohio"
    assert normalize_operator_name("BELTERRA PARK-FANDUEL") == "Belterra Park - FanDuel"

    assert month_yyyymm((None, 1)) is None
    with pytest.raises(ValueError):
        parse_month_spec((2025, True))
    with pytest.raises(ValueError):
        fiscal_year_july(2025, 13)
    assert resolve_fetch_months(
        "2025-01", "2025-02", launch_date=datetime.datetime(2024, 1, 15, 12, 0),
    ) == [datetime.date(2025, 1, 1), datetime.date(2025, 2, 1)]
    neg_logs: list[str] = []
    rh_rec, _rr_rec, _mh_rec, _mr_rec = reconcile_components(
        "OH", 100.0, 10.0, None, None, 150.0, 20.0, log=neg_logs.append,
    )
    assert rh_rec is None and any("negative retail handle" in m for m in neg_logs)
    strict_bad = finalize_state_month(
        state="Ohio",
        iso_month="2026-02",
        handle=100.0,
        revenue=10.0,
        mobile_handle=150.0,
        mobile_revenue=20.0,
        strict_split_pct=0.05,
    )
    assert strict_bad is not None and strict_bad.retail_handle is None and strict_bad.mobile_handle is None


def test_public_tree_excludes_private_tokens() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    forbidden = (
        "write_to_" + "sheet",
        "g" + "og",
        "run_" + "gog",
        "bo" + "yd",
        "1A2" + "a8zz",
        "kr" + "im",
        "jo" + "ey",
        "open" + "claw",
    )
    pattern = re.compile("|".join(re.escape(tok) for tok in forbidden), re.IGNORECASE)
    for folder in ("statewager", "publish", "docs"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                match = pattern.search(text)
                assert match is None, f"Forbidden token {match.group(0)!r} found in {path}"


def test_adversarial_r2_state_parsers_and_cache(tmp_path: pathlib.Path, monkeypatch) -> None:
    from contextlib import contextmanager

    class _MockSheet:
        def __init__(self, title: str, rows: list[tuple[object, ...]]) -> None:
            self.title = title
            self._rows = rows

        def iter_rows(self, values_only: bool = True):
            del values_only
            return iter(self._rows)

    class _MockWorkbook:
        def __init__(self, sheets: list[_MockSheet]) -> None:
            self.worksheets = sheets
            self.sheetnames = [s.title for s in sheets]

        def __getitem__(self, key: str) -> _MockSheet:
            return next(s for s in self.worksheets if s.title == key)

    # 1. Missouri: apostrophe stripping, subtitle immunity, STATE TOTALS reset
    assert missouri.detail_operator_key("Barstool's") == missouri.detail_operator_key("Barstools")
    mo_pages = [
        "\n".join([
            "SPORTS WAGERING RETAIL STATISTICAL REPORT",
            "ALPHA CASINO",
            "Feb-26  $100.00  $90.00  $10.00",
            "STATE TOTALS MTD:  $100.00  $90.00  $10.00",
            "Page 2",
            "SPORTS WAGERING MOBILE STATISTICAL REPORT",
            "BETA MOBILE - Mobile Jan-26  $50.00  $45.00  $5.00",
            "Interactive Division Feb-26",
            "Feb-26  $200.00  $180.00  $20.00  $2.00",
            "STATE TOTALS MTD:  $200.00  $180.00  $20.00  $2.00",
            "STATE TOTALS:  $999.00  $888.00  $99.00  $9.00",
            "SPORTS WAGERING GROSS RECEIPTS & TAX REPORT",
            "STATISTICAL REPORT SUMMARY",
        ])
    ]
    stat_totals, mo_details, _sg, state_tax, _og, _ot = missouri._scan_missouri_pages(mo_pages, 2026, 2)
    assert stat_totals["stat_retail"] == (100.0, 10.0)
    assert stat_totals["stat_mobile"] == (200.0, 20.0)
    assert state_tax == 2.0
    by_mo_op = {row[0]: row for row in mo_details}
    assert "BETA MOBILE" in by_mo_op and by_mo_op["BETA MOBILE"][5:7] == (200.0, 20.0)
    assert "Interactive Division" not in by_mo_op

    # 2. Kansas: narrow detail header table + clear partial op_buckets on fallback
    class _FakeKsCache:
        def __init__(self) -> None:
            self._rows = [
                ["Casino", "Platform", "Settled Wagers", "Prizes", "Promotional", "Net", "State Share"],
                ["Boot Hill", "DraftKings", "$1,000", "$900", "$10", "$90", "$9"],
                ["Totals", "", "$1,000", "$900", "$10", "$90", "$9"],
            ]

        def flattened_rows(self):
            return True, self._rows

        def raw_text(self):
            return None

    monkeypatch.setattr(kansas, "pdfplumber", object())
    ks_state_f, ks_ops_f = kansas.parse_detail_fiscal(b"%PDF", "Mar 2026", _page_cache=_FakeKsCache())
    assert ks_state_f == (100.0, 10.0, 9.0)
    assert "boot hill - draftkings" in ks_ops_f

    class _PartialFailKsCache:
        def flattened_rows(self):
            return True, [
                ["Casino", "Platform", "Settled Wagers", "Prizes", "Promotional", "Net", "State Share"],
                ["Boot Hill Stale", "FanDuel", "$500", "$400", "$5", "$95", "$9"],
            ]

        def raw_text(self):
            return (
                "Hollywood Fresh (BetMGM) $100 $1000 $900 $10 $90 $9\n"
                "Totals $1000 $900 $10 $90 $9\n"
            )

    ks_partial_state, ks_partial_ops = kansas.parse_detail_fiscal(
        b"%PDF", "Mar 2026", _page_cache=_PartialFailKsCache(),
    )
    assert "boot hill stale - fanduel" not in ks_partial_ops
    assert ks_partial_state is not None

    # 3. Illinois: stripped headers & unvalidated HTTP CSV must not clobber stale cache
    cash_bytes = (
        "Sport Level, Licensee , Location Type , Tier 1 Handle , Tier 2 Handle \n"
        "Pro, Alpha Book , Online Wagering , $100 , $50 \n"
    ).encode("utf-8")
    accr_bytes = (
        "Location Type , Licensee , State AGR , State Tax \n"
        "Online Wagering , Alpha Book , $15 , $3 \n"
    ).encode("utf-8")
    assert illinois._scan_cash_csv(cash_bytes)[0] == (150.0, 0.0, 150.0)
    assert illinois._scan_accrual_csv(accr_bytes)[0] == (15.0, 0.0, 15.0)

    il_cache = tmp_path / "il_cache"
    il_cache.mkdir()
    recent_date = datetime.date.today().replace(day=1)
    recent_ym = f"{recent_date.year}_{recent_date.month:02d}"
    detail_file = il_cache / f"illinois_{recent_ym}_detail.csv"
    tax_file = il_cache / f"illinois_{recent_ym}_tax.csv"
    detail_file.write_bytes(cash_bytes)
    tax_file.write_bytes(accr_bytes)
    stale_ts = time.time() - 30 * 86400
    os.utime(detail_file, (stale_ts, stale_ts))
    os.utime(tax_file, (stale_ts, stale_ts))
    monkeypatch.setattr(illinois, "_bootstrap_aem_tokens", lambda *_a, **_kw: ("p", "r", "v"))
    monkeypatch.setattr(illinois, "post_report_csv", lambda *_a, **_kw: b"<html>error page</html>")
    il_res = illinois.fetch(
        (recent_date.year, recent_date.month),
        cache_dir=str(il_cache),
        log=lambda _m: None,
    )
    assert len(il_res) == 1 and il_res[0].handle == 150.0 and il_res[0].revenue == 15.0
    assert detail_file.read_bytes() == cash_bytes

    # 4. Indiana & Ohio: consecutive header rows + subtotal exclusion & single fetch
    in_wb = _MockWorkbook([
        _MockSheet("SW Summary", [
            ("Operator", "Handle", None, None),
            (None, None, "Taxable AGR", None),
            ("TOTAL", 500.0, 50.0, None),
        ])
    ])
    assert indiana.parse_summary_totals(in_wb) == (500.0, 50.0)

    # Check subtotal exclusion when fallback branch reached before Online subtotal sets section=None
    oh_sheet2 = _MockSheet("Jan2026", [
        ("Type A Proprietors - Online Sports Gaming", None, None, None, None, None),
        ("Type B Proprietors - Retail Sports Gaming", None, None, None, None, None),
        ("Retail Prop", 40.0, 0.0, 0.0, 0.0, 4.0),
        ("Subtotal Type B Retail", 40.0, 0.0, 0.0, 0.0, 4.0),
        ("Type A Proprietors - Online Sports Gaming", None, None, None, None, None),
        ("Subtotal Extra", 60.0, 0.0, 0.0, 0.0, 6.0),
        ("Total Online and Retail", 100.0, 0.0, 0.0, 0.0, 10.0),
    ])
    oh_parsed = ohio._parse_month_sheet_with_fiscal(oh_sheet2)
    assert oh_parsed is None  # Online subtotal wasn't seen so returns None, and Subtotal Extra wasn't added
    oh_sheet3 = _MockSheet("Jan2026", [
        ("Type A Proprietors - Online Sports Gaming", None, None, None, None, None),
        ("Online Prop", 60.0, 0.0, 0.0, 0.0, 6.0),
        ("Subtotal Type A Online", 60.0, 0.0, 0.0, 0.0, 6.0),
        ("Type B Proprietors - Retail Sports Gaming", None, None, None, None, None),
        ("Retail Prop", 40.0, 0.0, 0.0, 0.0, 4.0),
        ("Subtotal Type B Retail", 40.0, 0.0, 0.0, 0.0, 4.0),
        ("Total Online and Retail", 100.0, 0.0, 0.0, 0.0, 10.0),
    ])
    oh_core, _, _, _ = ohio._parse_month_sheet_with_fiscal(oh_sheet3)
    assert [d[0] for d in oh_core[6]] == ["Online Prop", "Retail Prop"]

    # Ohio single download guard on expired cache missing month
    oh_cache = tmp_path / "oh_cache"
    oh_cache.mkdir()
    oh_cfile = oh_cache / f"ohio_{recent_date.year}_cumulative.xlsx"
    oh_cfile.write_bytes(b"PK\x03\x04old")
    os.utime(oh_cfile, (stale_ts, stale_ts))
    oh_dl_calls: list[int] = []
    monkeypatch.setattr(ohio, "openpyxl", object())
    monkeypatch.setattr(
        ohio,
        "download_cumulative_workbook",
        lambda *_a, **_kw: oh_dl_calls.append(1) or b"PK\x03\x04new",
    )
    monkeypatch.setattr(ohio, "_parse_cumulative_xlsx_with_fiscal", lambda *_a, **_kw: ({}, {}))
    ohio.fetch((recent_date.year, recent_date.month), cache_dir=str(oh_cache), log=lambda _m: None)
    assert len(oh_dl_calls) == 1

    # 5. Pennsylvania: .xlsx-only discovery, relative URL, month patterns, dynamic short operator blocks
    assert pennsylvania._is_fy_summary_anchor(
        "/sites/default/files/fy25-26-monthly-sports-wagering-report-summary.pdf",
        "",
        "FY 25-26 Monthly Sports Wagering Summary",
    ) is None
    assert pennsylvania._is_fy_summary_anchor(
        "/sites/default/files/FY2025-2026_Sports_Wagering_Monthly_Summary.xlsx?v=1",
        "",
        "FY 2025-2026 Monthly Sports Wagering Summary",
    ) == (2025, 2026)
    assert pennsylvania._absolute_url("reports/fy26.xlsx") == (
        "https://gamingcontrolboard.pa.gov/news-and-transparency/reports/fy26.xlsx"
    )
    pa_cols = pennsylvania.parse_month_columns(
        [("Op", "July 2025", "Sept 2025", "Augusta", "January 2026")],
        2025,
        2026,
    )
    assert pa_cols == {1: "Jul 2025", 2: "Sep 2025", 4: "Jan 2026"}
    pa_rows = [
        ("OpShort", None),
        ("Total Sports Wagering", None),
        ("Handle*", 100.0),
        ("Revenue", 10.0),
        ("Promotional Credits", 2.0),
        ("Retail Sports Wagering", None),
        ("Handle*", 100.0),
        ("Revenue", 10.0),
        ("OpSecond", None),
        ("Total Sports Wagering", None),
        ("Handle*", 200.0),
        ("Revenue", 20.0),
        ("Online Sports Wagering", None),
        ("Handle*", 200.0),
        ("Revenue", 20.0),
    ]
    blocks = list(pennsylvania._iter_operator_blocks(pa_rows))
    assert blocks == [(0, "OpShort"), (8, "OpSecond")]
    pa_det = pennsylvania.parse_detail_metrics(pa_rows, 2025, 2026, col_to_label={1: "Jul 2025"})
    assert len(pa_det["Jul 2025"]) == 2
    assert pa_det["Jul 2025"][0][3:7] == (100.0, 10.0, None, None)
    assert pa_det["Jul 2025"][1][3:7] == (None, None, 200.0, 20.0)
    assert pennsylvania._extract_fiscal_block(pa_rows, 8, {1: "Jul 2025"}) == {"Jul 2025": (20.0, None, None)}

    # 6. Nevada: negative Win, placeholder %-change slot, None mobile split, per-report TTL
    assert nevada.parse_sports_line("Sports Pool (2) 10 10 -125 -50.0 -2.5", "Sports Pool (2)") == (5_000_000.0, -125_000.0)
    assert nevada.parse_sports_line("Sports Pool (2) 10 10 \u2212125 -50.0 \u22122.5", "Sports Pool (2)") == (
        5_000_000.0,
        -125_000.0,
    )
    assert nevada.parse_sports_row_values(
        ["Sports Pool (2)", 180, 180, 45_000, "-", 5.0, 120_000, 10.0, 5.2],
        "Sports Pool (2)",
    ) == pytest.approx((900_000_000.0, 45_000_000.0))
    assert nevada._with_mobile_splits(100.0, 10.0, None, []) == (100.0, 10.0, None, None, None, None, [])

    nv_cache = tmp_path / "nv_cache"
    nv_cache.mkdir()
    nv_xlsx = nv_cache / f"nevada_{recent_ym}.xlsx"
    nv_pdf = nv_cache / f"nevada_{recent_ym}.pdf"
    nv_xlsx.write_bytes(b"PK\x03\x04stale")
    nv_pdf.write_bytes(b"%PDF-1.4 fresh")
    os.utime(nv_xlsx, (stale_ts, stale_ts))
    monkeypatch.setattr(nevada, "discover_report_urls", lambda **_kw: {})
    monkeypatch.setattr(
        nevada,
        "parse_report_bytes",
        lambda kind, data, **_kw: (200.0, 20.0, 50.0, 5.0, 150.0, 15.0, []) if kind == "pdf" else None,
    )
    nv_res = nevada.fetch((recent_date.year, recent_date.month), cache_dir=str(nv_cache), log=lambda _m: None)
    assert len(nv_res) == 1 and nv_res[0].source_url is None and nv_res[0].handle == 200.0

    # 7. Iowa: FY link filtering, repeated same-month headings + ß/ﬁ ligatures, x-tolerance
    ia_fy_links = iowa._parse_fy_media_links([
        {"href": "/media/1/download", "text": "FY2026 Fantasy Sports Archive", "title": ""},
        {"href": "/media/2/download", "text": "FY2026 Advanced Deposit Wagering", "title": ""},
        {"href": "/media/3/download", "text": "FY2026 Sports Wagering Amended", "title": ""},
        {"href": "/media/4/download", "text": "FY2026 Casino Gaming Archive", "title": ""},
        {"href": "/media/5/download", "text": "FY2026 Sports Wagering Revenue Archive", "title": ""},
    ])
    assert ia_fy_links == {(2025, 2026): 5}
    ia_text = (
        "\ufb01 ß Preamble\n"
        "SPORTS WAGERING REVENUE REPORT -- OCTOBER 2025\n"
        "Page 1 intro\n"
        "SPORTS WAGERING REVENUE REPORT -- OCTOBER 2025\n"
        "TOTALS\n"
        "SPORTS WAGERING NET RECEIPTS $1,000.00\n"
        "SPORTS WAGERING HANDLE $10,000.00\n"
        "RETAIL HANDLE $2,000.00\n"
        "RETAIL NET RECEIPTS $200.00\n"
        "INTERNET HANDLE $8,000.00\n"
        "INTERNET NET RECEIPTS $800.00\n"
        "SPORTS WAGERING REVENUE REPORT -- NOVEMBER 2025\n"
    )
    ia_parsed = iowa.parse_month_section(ia_text, "Oct 2025")
    assert ia_parsed is not None and ia_parsed.metrics[:2] == (10_000.0, 1_000.0)
    ia_details: dict[str, list[tuple[str, float, float, float | None, float | None, float | None, float | None]]] = {}

    class _MockIaPage:
        def extract_words(self, **_kw) -> list[dict[str, object]]:
            return [
                {"text": "Alpha", "top": 10.0, "x0": 110.0, "x1": 160.0},
                {"text": "Beta", "top": 10.0, "x0": 240.0, "x1": 290.0},
                {"text": "RETAIL", "top": 30.0, "x0": 10.0, "x1": 45.0},
                {"text": "HANDLE", "top": 30.0, "x0": 50.0, "x1": 90.0},
                {"text": "$100.00", "top": 30.0, "x0": 120.0, "x1": 160.0},
                {"text": "$200.00", "top": 30.0, "x0": 250.0, "x1": 290.0},
                {"text": "RETAIL", "top": 50.0, "x0": 10.0, "x1": 40.0},
                {"text": "NET", "top": 50.0, "x0": 42.0, "x1": 60.0},
                {"text": "RECEIPTS", "top": 50.0, "x0": 62.0, "x1": 95.0},
                {"text": "$20.00", "top": 50.0, "x0": 250.0, "x1": 290.0},
            ]

    iowa._extract_details_from_page(
        _MockIaPage(),
        "SPORTS WAGERING REVENUE REPORT -- OCTOBER 2025",
        None,
        None,
        ia_details,
    )
    assert ia_details["Oct 2025"] == [("Beta (Retail)", 200.0, 20.0, 200.0, 20.0, None, None)]

    # 8. Discover & Louisiana: dir-date immunity, fy2026, FY rolling ranking, remote_cumulative before cache
    assert classify_sportsbook_filename(
        "https://lsp.org/wp-content/uploads/2025/09/fy26_mobile_sb.xlsx",
        text="FY26 Mobile Sportsbook",
    ) is None
    cum_2026 = classify_cumulative_filename(
        "https://lsp.org/wp-content/uploads/2025/09/fy2026_mobile_sb.xlsx",
        text="FY2026 Mobile Sportsbook",
    )
    assert cum_2026 is not None and cum_2026[1] == "mobile" and "Sep 2025" in cum_2026[0]
    monkeypatch.setattr(
        louisiana,
        "crawl_anchors",
        lambda *_a, **_kw: [
            {"href": "/wp-content/uploads/2026/10/fy27_mobile_sb_2026_10.xlsx", "title": "", "text": "FY27 Oct 2026"},
            {"href": "/wp-content/uploads/2026/12/fy27_mobile_sb_2026_12.xlsx", "title": "", "text": "FY27 Dec 2026"},
        ],
    )
    la_pm, la_cum = louisiana.discover_remote_links(log=lambda _m: None)
    assert "Oct 2026" in la_pm and la_cum["Jul 2026:mobile"][0].endswith("2026_12.xlsx")

    # 9. Mississippi multi-region on one sheet & cache.load_cache OSError
    ms_wb = _MockWorkbook([
        _MockSheet("Combined", [
            ("Coastal", None, None, None, None),
            ("Overall for Region", None, 100.0, None, 10.0),
            ("Central", None, None, None, None),
            ("Overall for Region", None, 200.0, None, 20.0),
            ("Overall for State", None, 300.0, None, 30.0),
        ])
    ])

    @contextmanager
    def _ms_ctx(*_a, **_kw):
        yield ms_wb

    monkeypatch.setattr(mississippi, "openpyxl", object())
    monkeypatch.setattr(mississippi, "open_xlsx_workbook", _ms_ctx)
    ms_total_h, ms_total_r, ms_details = mississippi.parse_state_totals(b"PK\x03\x04dummy")
    assert (ms_total_h, ms_total_r) == (300.0, 30.0)
    assert [(r[0], r[1], r[2]) for r in ms_details] == [("Coastal", 100.0, 10.0), ("Central", 200.0, 20.0)]

    monkeypatch.setattr("statewager.cache._read_cache_with_age", lambda _p: (_ for _ in ()).throw(PermissionError("denied")))
    assert load_cache(str(tmp_path / "forbidden.bin")) is None


def test_adversarial_r3_publish_workflows_and_viewer(tmp_path: pathlib.Path, monkeypatch) -> None:
    root = pathlib.Path(__file__).resolve().parent.parent

    # 1. Workflows: CI least-privilege permissions + no rebase over generated docs
    wf_dir = root / ".github" / "workflows"
    if wf_dir.is_dir():
        ci_text = (wf_dir / "ci.yml").read_text(encoding="utf-8")
        assert "permissions:\n  contents: read" in ci_text
        assert "persist-credentials: false" in ci_text
        refresh_text = (wf_dir / "refresh-data.yml").read_text(encoding="utf-8")
        assert "git pull --rebase" not in refresh_text
        assert "git reset --hard origin/main" in refresh_text
        assert "--cache-dir" in refresh_text

    # 2. merge_detail_results & publish_all(..., merge_existing=True) preserve prior source_url on fallback
    prev_det = PublishedDetailRow(
        state="Ohio", month="2026-02", operator="Alpha Book", brand="DraftKings",
        handle=100.0, revenue=10.0, hold=10.0, source_url="https://oh.gov/feb26.xlsx",
    )
    fresh_det = PublishedDetailRow(
        state="Ohio", month="2026-02", operator="Alpha Book", brand="DraftKings",
        handle=120.0, revenue=12.0, hold=10.0, source_url=None,
    )
    fresh_other_op = PublishedDetailRow(
        state="Ohio", month="2026-02", operator="Beta Book", brand="FanDuel",
        handle=80.0, revenue=8.0, hold=10.0, source_url=None,
    )
    merged_det = merge_detail_results([prev_det], [fresh_det, fresh_other_op])
    assert all(row.source_url == "https://oh.gov/feb26.xlsx" for row in merged_det)

    docs_dir = tmp_path / "docs_r3"
    data_dir = docs_dir / "data"
    det_dir = data_dir / "details"
    write_state_csv(
        str(data_dir),
        "ohio",
        [_state_row("Ohio", "2026-02", 100.0, 10.0, source_url="https://oh.gov/cached_feb26.xlsx")],
    )
    write_state_detail_csv(
        str(det_dir),
        "ohio",
        [
            PublishedDetailRow(
                state="Ohio",
                month="2026-02",
                operator="Alpha Book",
                brand="DraftKings",
                handle=100.0,
                revenue=10.0,
                hold=10.0,
                source_url="https://oh.gov/cached_feb26.xlsx",
            )
        ],
    )
    fallback_res = _state_row(
        "Ohio",
        "2026-02",
        105.0,
        11.0,
        hold=10.48,
        source_url=None,
        details=[
            OperatorMonthResult(
                month="2026-02",
                operator="Alpha Book",
                handle=105.0,
                revenue=11.0,
                hold=10.48,
            )
        ],
    )
    monkeypatch.setattr(ohio, "fetch", lambda *_a, **_kw: [fallback_res])
    pub_main.publish_all(
        month_start="2026-02",
        month_end="2026-02",
        states=["ohio"],
        cache_dir=str(tmp_path / "c_r3"),
        docs_dir=str(docs_dir),
        merge_existing=True,
        quiet=True,
    )
    persisted_det = read_detail_csv(str(det_dir / "ohio.csv"))
    persisted_all_det = read_detail_csv(str(det_dir / "all.csv"))
    assert persisted_det and persisted_det[0].source_url == "https://oh.gov/cached_feb26.xlsx"
    assert persisted_all_det and persisted_all_det[0].source_url == "https://oh.gov/cached_feb26.xlsx"

    # 3. Headless Node DOM harness against real docs/index.html + docs/data/
    node_bin = shutil.which("node")
    docs_root = root / "docs"
    if node_bin and (docs_root / "data" / "details" / "all.csv").is_file():
        harness = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const docsRoot = process.argv[1];
const html = fs.readFileSync(path.join(docsRoot, "index.html"), "utf8");
const m = html.match(/<script>\s*([\s\S]*?)\s*<\/script>/);
assert(m, "missing <script> block");
const monthReSrc = m[1].match(/const MONTH_RE = [^;]+;/);
const addMonthsSrc = m[1].match(/function addMonths\([\s\S]*?\n      \}/);
const moStartSrc = m[1].match(/function missouriEffectiveStart\([\s\S]*?\n      \}/);
assert(monthReSrc && addMonthsSrc && moStartSrc, "missing addMonths/missouriEffectiveStart");
const pureCtx = vm.createContext({});
vm.runInContext(`${monthReSrc[0]}\n${addMonthsSrc[0]}\n${moStartSrc[0]}`, pureCtx);
assert.strictEqual(pureCtx.missouriEffectiveStart(1), "2025-12");
assert.strictEqual(pureCtx.missouriEffectiveStart(3), "2026-02");
assert.strictEqual(pureCtx.missouriEffectiveStart(12), "2026-11");

function makeClassList() {
  const s = new Set();
  return {
    add: (...xs) => xs.forEach((x) => s.add(x)),
    remove: (...xs) => xs.forEach((x) => s.delete(x)),
    toggle: (x, force) => {
      if (force === undefined) { if (s.has(x)) { s.delete(x); return false; } s.add(x); return true; }
      if (force) s.add(x); else s.delete(x);
      return !!force;
    },
    contains: (x) => s.has(x),
  };
}

function makeEl(tag, id = "") {
  return {
    tagName: tag.toUpperCase(),
    id,
    _text: "",
    _value: "",
    checked: false,
    disabled: false,
    hidden: false,
    children: [],
    attrs: {},
    style: {},
    classList: makeClassList(),
    listeners: new Map(),
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v ?? ""); this.children = []; },
    get text() { return this._text; },
    set text(v) { this._text = String(v ?? ""); },
    set innerHTML(v) { if (v === "") this.children = []; },
    get innerHTML() { return ""; },
    get value() {
      if (this.tagName === "SELECT" && !this._value && this.options.length) return this.options[0].value;
      return this._value;
    },
    set value(v) { this._value = String(v ?? ""); },
    get options() {
      const out = [];
      const walk = (n) => {
        for (const c of n.children || []) {
          if (c.tagName === "OPTION") out.push(c);
          else walk(c);
        }
      };
      walk(this);
      return out;
    },
    get selectedIndex() {
      const idx = this.options.findIndex((o) => o.value === this.value);
      return idx >= 0 ? idx : 0;
    },
    appendChild(child) {
      if (child.tagName === "#FRAGMENT") {
        child.children.forEach((c) => this.children.push(c));
        child.children = [];
      } else {
        this.children.push(child);
      }
      return child;
    },
    remove() {},
    contains(t) { return t === this || this.children.some((c) => c.contains && c.contains(t)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener(t, fn) {
      if (!this.listeners.has(t)) this.listeners.set(t, []);
      this.listeners.get(t).push(fn);
    },
    dispatchEvent(t, ev = {}) {
      const e = Object.assign({ type: t, target: this, stopPropagation() {}, preventDefault() {} }, ev);
      for (const fn of this.listeners.get(t) || []) fn(e);
    },
    click() { this.dispatchEvent("click"); },
    focus() {},
    querySelectorAll() { return []; },
  };
}

const byId = new Map();
const getEl = (id, tag = "div") => (byId.has(id) || byId.set(id, makeEl(tag, id)), byId.get(id));

const stateSel = getEl("stateSelect", "select");
stateSel.appendChild(Object.assign(makeEl("option"), { value: "__ALL__", textContent: "All States (Combined)" }));
for (const slug of ["illinois", "indiana", "iowa", "kansas", "louisiana", "mississippi", "missouri", "nevada", "ohio", "pennsylvania"]) {
  stateSel.appendChild(Object.assign(makeEl("option"), { value: slug, textContent: slug[0].toUpperCase() + slug.slice(1) }));
}
const winSel = getEl("windowSelect", "select");
for (const [v, t] of [["1", "Monthly"], ["3", "Rolling 3M"], ["12", "TTM (12M)"]]) {
  winSel.appendChild(Object.assign(makeEl("option"), { value: v, textContent: t }));
}
const metSel = getEl("metricSelect", "select");
for (const v of ["handle", "revenue", "hold", "mobile_handle", "retail_handle", "promo_deductions", "tax", "yoy_handle", "yoy_revenue", "handle_share", "revenue_share"]) {
  metSel.appendChild(Object.assign(makeEl("option"), { value: v, textContent: v }));
}
getEl("entitySelect", "select");
getEl("includePartial", "input").checked = false;

const ctx = vm.createContext({
  console, Math, Date, Map, Set, Array, Object, Number, String, Boolean, JSON, URLSearchParams, setTimeout, clearTimeout,
  document: {
    getElementById: (id) => getEl(id),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    createDocumentFragment: () => makeEl("#FRAGMENT"),
    createTextNode: (t) => ({ tagName: "#TEXT", textContent: String(t) }),
    addEventListener() {},
    body: makeEl("body"),
    activeElement: null,
  },
  window: { location: { search: "", pathname: "/", hash: "", href: "https://example.test/" }, history: { replaceState() {} } },
  navigator: {},
  fetch: async (url) => {
    const filePath = path.join(docsRoot, url.split("?")[0]);
    if (!fs.existsSync(filePath)) return { ok: false, status: 404, async text() { return ""; }, async json() { return {}; } };
    const txt = fs.readFileSync(filePath, "utf8");
    return { ok: true, status: 200, async text() { return txt; }, async json() { return JSON.parse(txt); } };
  },
});

vm.runInContext(m[1], ctx);

(async () => {
  await new Promise((r) => setTimeout(r, 80));
  const flush = () => new Promise((r) => setTimeout(r, 40));
  const countRects = () => {
    let c = 0;
    const walk = (n) => {
      if (n?.tagName === "RECT" && n.listeners.has("mousemove")) c += 1;
      (n?.children || []).forEach(walk);
    };
    walk(getEl("chartSvg"));
    return c;
  };

  // All States + TTM (12M) + brand:DraftKings must yield complete 6-state months (2026-02..2026-09)
  const entSel = getEl("entitySelect");
  winSel.value = "12";
  winSel.dispatchEvent("change");
  entSel.value = "brand:DraftKings";
  entSel.dispatchEvent("change");
  await flush();
  assert.strictEqual(getEl("fromMonth").value, "2026-02");
  assert(getEl("toMonth").value >= "2026-09");
  assert(countRects() >= 8);
  assert(!getEl("chartNote").textContent.includes("partial"));

  // All States + Rolling 3M + DraftKings must include 2025-12 and 2026-01
  winSel.value = "3";
  winSel.dispatchEvent("change");
  await flush();
  const months3m = Array.from(new Set(getEl("tableBody").children.map((tr) => tr.children[0]?.textContent)));
  assert(months3m.includes("2025-12") && months3m.includes("2026-01"));
  assert.strictEqual(countRects(), 17);

  // Compare Brands + All States + YoY Handle Growth must exclude 2025-03..2025-06 partial baselines
  entSel.value = "__STATE_TOTAL__";
  entSel.dispatchEvent("change");
  winSel.value = "1";
  winSel.dispatchEvent("change");
  await flush();
  getEl("compareBtn").click();
  await flush();
  for (const lbl of getEl("compareList").children) {
    const cb = lbl.children[0];
    if (cb && (cb.getAttribute("data-name") === "DraftKings" || cb.getAttribute("data-name") === "FanDuel")) {
      cb.checked = true;
      cb.dispatchEvent("change");
    }
  }
  metSel.value = "yoy_handle";
  metSel.dispatchEvent("change");
  await flush();
  const yoyTicks = [];
  const walkTicks = (n) => {
    if (n?.tagName === "TEXT" && /^\d{4}-\d{2}$/.test(n.textContent)) yoyTicks.push(n.textContent);
    (n?.children || []).forEach(walkTicks);
  };
  walkTicks(getEl("chartSvg"));
  assert(yoyTicks.length > 0 && yoyTicks.every((m) => m >= "2025-07"));

  // Ohio -> FanDuel -> Rolling 3M -> Retail Handle must omit all-null retail windows rather than plotting $0
  getEl("compareClearBtn").click();
  getEl("compareBtn").click();
  stateSel.value = "ohio";
  stateSel.dispatchEvent("change");
  await flush();
  entSel.value = "brand:FanDuel";
  entSel.dispatchEvent("change");
  winSel.value = "3";
  winSel.dispatchEvent("change");
  metSel.value = "retail_handle";
  metSel.dispatchEvent("change");
  await flush();
  const ohBars = countRects();
  assert(ohBars > 0 && ohBars < getEl("tableBody").children.length);

  // Selecting BetJack in Ohio Compare Brands then switching to Kansas must prune BetJack
  winSel.value = "1";
  winSel.dispatchEvent("change");
  metSel.value = "handle";
  metSel.dispatchEvent("change");
  getEl("compareBtn").click();
  await flush();
  for (const lbl of getEl("compareList").children) {
    const cb = lbl.children[0];
    if (cb && cb.getAttribute("data-name") === "BetJack") {
      cb.checked = true;
      cb.dispatchEvent("change");
    }
  }
  assert.strictEqual(getEl("compareBtn").textContent, "Compare (1)");
  stateSel.value = "kansas";
  stateSel.dispatchEvent("change");
  await flush();
  assert.strictEqual(getEl("compareBtn").textContent, "Compare (0)");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run([node_bin, "-e", harness, str(docs_root)], capture_output=True, text=True)
        assert proc.returncode == 0, f"Node harness failed:\n{proc.stderr}\n{proc.stdout}"



