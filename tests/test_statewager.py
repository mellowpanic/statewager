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


def test_missouri_pdf_name_re_matches_underscores_and_revised() -> None:
    m1 = missouri.PDF_NAME_RE.search("FY26_SWFinReport/03_Mar/SW_Monthly_Financials_0326_revised.pdf")
    m2 = missouri.PDF_NAME_RE.search("FY27_SWFinReport/09_Sep/SW Monthly Financials 0926.pdf")
    assert m1 is not None and m1.group(1) == "0326"
    assert m2 is not None and m2.group(1) == "0926"
