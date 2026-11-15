# StateWager

Standalone Python library and static CSV/HTML publisher for official U.S. state sports wagering regulatory reports.

## Overview

- **[`statewager/`](statewager/README.md)** — Core `pip`-installable package that discovers, fetches, parses, and validates monthly regulatory filings across 10 U.S. jurisdictions (`illinois`, `indiana`, `iowa`, `kansas`, `louisiana`, `mississippi`, `missouri`, `nevada`, `ohio`, `pennsylvania`), returning typed `StateMonthResult` records.
- **[`publish/`](publish/README.md)** — Zero-build static publisher that writes per-state CSVs (`docs/data/<state>.csv`), a combined dataset (`docs/data/all.csv`), operator/brand detail CSVs (`docs/data/details/<state>.csv` and `docs/data/details/all.csv`), and a vanilla-JS interactive table + SVG chart (`docs/index.html`) ready for GitHub Pages.
- **`docs/`** — Published CSV datasets and static web viewer.

## Dataset Schemas

- **Statewide CSVs (`docs/data/*.csv`, 15 columns)**: `state`, `month`, `handle`, `revenue`, `hold`, `retail_handle`, `retail_revenue`, `retail_hold`, `mobile_handle`, `mobile_revenue`, `mobile_hold`, `source_url`, plus optional fiscal fields `gross_revenue`, `promo_deductions`, and `tax` (populated where disclosed by state regulators: `IA`, `IL` tax, `IN`, `KS`, `LA`, `MO`, `OH`, `PA`).
- **Operator & Brand Detail CSVs (`docs/data/details/*.csv`, 17 columns)**: `state`, `month`, `operator`, `brand`, followed by the same handle/revenue/hold, channel splits, `source_url`, and fiscal columns (`gross_revenue`, `promo_deductions`, `tax`) across the 7 disclosing jurisdictions (`IL`, `IN`, `IA`, `KS`, `MO`, `OH`, `PA`).

## Interactive Viewer (`docs/index.html`)

- **Brand Market Share & Multi-Series Compare**: View `Brand Handle Share (%)` or `Brand Revenue Share (%)` as a 100% stacked bar chart, or use `Compare` to overlay 1–5 brands or states as multi-series trend lines.
- **Rolling Windows & Same-Store YoY**: Switch between `Monthly`, `Rolling 3M`, and `TTM (12M)` windows alongside same-store `YoY Handle Growth (%)`, `YoY Revenue Growth (%)`, and `YoY Hold Δ (bps)` (computed strictly over jurisdictions present in both periods).
- **Regulator Release Freshness**: Clickable status strip tracking each state's latest published reporting month relative to peers.
- **Fiscal Metrics & Exports**: Dynamic table columns for `Gross Revenue`, `Gross Hold %`, `Promo / Credits`, and `Wagering Tax`, shareable URL query state (`Copy Share Link`), and client-side CSV export of the active filtered table view (`Download View CSV`).

## Automated Data Refresh

A scheduled GitHub Actions workflow (`.github/workflows/refresh-data.yml`) runs every other day (`17 13 */2 * *` UTC) and supports manual `workflow_dispatch` triggers with a configurable `months` input (`3` by default), tolerating transient single-state regulator outages while preserving previously published rows in `docs/data/`.

## Quick Start

```bash
pip install -e .
```

Fetch monthly regulatory metrics in Python:

```python
from statewager.states import nevada, ohio

rows = ohio.fetch("2025-03", "2026-02", cache_dir="cache")
for r in rows:
    print(r.state, r.month, r.handle, r.revenue, r.hold)
```

Rebuild static CSVs and `docs/index.html`:

```bash
./scripts/publish_all.sh --cache-dir cache
```

Run tests:

```bash
pytest
```
