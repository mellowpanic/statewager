# publish

Static CSV and HTML publisher built on top of `statewager`.

`publish` depends **only** on `statewager` and the Python standard library. It writes static CSV files and a zero-build vanilla-JS viewer into `docs/`, ready to serve directly from GitHub Pages (`/docs` root).

## Outputs

- `docs/data/<state>.csv` — Per-state monthly time series (`illinois.csv`, `indiana.csv`, `iowa.csv`, `kansas.csv`, `louisiana.csv`, `mississippi.csv`, `missouri.csv`, `nevada.csv`, `ohio.csv`, `pennsylvania.csv`).
- `docs/data/all.csv` — Combined statewide dataset across all states sorted by `(state, month)`.
- `docs/data/details/<state>.csv` & `docs/data/details/all.csv` — Operator/venue & normalized brand breakdown across the 7 disclosing jurisdictions (`illinois`, `indiana`, `iowa`, `kansas`, `missouri`, `ohio`, `pennsylvania`).
- `docs/index.html` — Static interactive table and SVG chart viewer in vanilla JavaScript supporting `Monthly` / `Rolling 3M` / `TTM (12M)` windows, `Brand Handle/Revenue Share (%)`, multi-series `Compare`, same-store YoY growth & hold Δ (bps), regulator release freshness badges, fiscal columns, shareable query-parameter links, and client-side View CSV export.

### CSV Schemas

Statewide CSVs (`docs/data/*.csv`, 15 columns):

```text
state,month,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url,gross_revenue,promo_deductions,tax
```

Operator/brand detail CSVs (`docs/data/details/*.csv`, 17 columns):

```text
state,month,operator,brand,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url,gross_revenue,promo_deductions,tax
```

## Usage

Generate `docs/` from all 10 states:

```bash
./scripts/publish_all.sh
```

Generate `docs/` strictly offline using local `cache/` files (zero network calls):

```bash
./scripts/publish_all.sh --offline --cache-dir cache
```

Preview locally:

```bash
python3 -m http.server -d docs 8000
```
