"""Static CSV + GitHub Pages publisher powered solely by statewager."""
from __future__ import annotations

from .csv_writer import (
    CSV_HEADERS,
    DETAIL_CSV_HEADERS,
    PublishedDetailRow,
    detail_to_csv_row,
    extract_published_details,
    merge_detail_results,
    merge_state_results,
    read_detail_csv,
    read_state_csv,
    result_to_csv_row,
    write_combined_csv,
    write_combined_detail_csv,
    write_state_csv,
    write_state_detail_csv,
)
from .site import render_index_html, write_index_html

__all__ = [
    "CSV_HEADERS",
    "DETAIL_CSV_HEADERS",
    "PublishedDetailRow",
    "detail_to_csv_row",
    "extract_published_details",
    "merge_detail_results",
    "merge_state_results",
    "read_detail_csv",
    "read_state_csv",
    "render_index_html",
    "result_to_csv_row",
    "write_combined_csv",
    "write_combined_detail_csv",
    "write_index_html",
    "write_state_csv",
    "write_state_detail_csv",
]
