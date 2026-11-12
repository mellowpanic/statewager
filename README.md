# StateWager

Standalone Python library and static CSV/HTML publisher for official U.S. state sports wagering regulatory reports.

## Overview

- **[`statewager/`](statewager/README.md)** — Core `pip`-installable package that discovers, fetches, parses, and validates monthly regulatory filings across 10 U.S. jurisdictions (`illinois`, `indiana`, `iowa`, `kansas`, `louisiana`, `mississippi`, `missouri`, `nevada`, `ohio`, `pennsylvania`), returning typed `StateMonthResult` records.
- **[`publish/`](publish/README.md)** — Zero-build static publisher that writes per-state CSVs (`docs/data/<state>.csv`), a combined dataset (`docs/data/all.csv`), and a vanilla-JS interactive table + SVG chart (`docs/index.html`) ready for GitHub Pages.
- **`docs/`** — Published CSV datasets and static web viewer.

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
