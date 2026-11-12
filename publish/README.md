# publish

Static CSV and HTML publisher built on top of `statewager`.

`publish` depends **only** on `statewager` and the Python standard library. It writes static CSV files and a zero-build vanilla-JS viewer into `docs/`, ready to serve directly from GitHub Pages (`/docs` root).

## Outputs

- `docs/data/<state>.csv` — Per-state monthly time series (`illinois.csv`, `indiana.csv`, `iowa.csv`, `kansas.csv`, `louisiana.csv`, `mississippi.csv`, `missouri.csv`, `nevada.csv`, `ohio.csv`, `pennsylvania.csv`).
- `docs/data/all.csv` — Combined dataset across all states sorted by `(state, month)`.
- `docs/index.html` — Static interactive table and SVG chart that fetches `data/all.csv` and `data/<state>.csv` in the browser with vanilla JavaScript (no bundler or external CDN dependencies).

### CSV Schema

All CSV files share the 12 core fields of `statewager.StateMonthResult`:

```text
state,month,handle,revenue,hold,retail_handle,retail_revenue,retail_hold,mobile_handle,mobile_revenue,mobile_hold,source_url
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
