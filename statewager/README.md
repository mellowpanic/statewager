# statewager

Standalone Python library for discovering, downloading, parsing, and validating official monthly US state sports wagering reports.

## Pipeline

`discover -> fetch -> parse -> validate -> return structured data`

`statewager` has zero external sink dependencies and returns typed `StateMonthResult` records directly.

## Supported States

- `illinois` — Illinois Gaming Board (IGB)
- `indiana` — Indiana Gaming Commission (IGC)
- `iowa` — Iowa Racing and Gaming Commission (IRGC)
- `kansas` — Kansas Lottery
- `louisiana` — Louisiana Gaming Control Board / State Police (LGCB / LSP)
- `mississippi` — Mississippi Gaming Commission (MGC)
- `missouri` — Missouri Gaming Commission (MGC)
- `nevada` — Nevada Gaming Control Board (NGCB)
- `ohio` — Ohio Casino Control Commission (OCCC)
- `pennsylvania` — Pennsylvania Gaming Control Board (PGCB)

## Installation

```bash
pip install -e .
```

Optional system tools used as fallbacks on specific state formats:
- `pdftotext` (`poppler-utils`) for raw PDF layout extraction
- `tesseract` (`tesseract-ocr`) with `pip install -e ".[ocr]"` for scanned PDFs
- `libreoffice` for legacy `.xls` conversion

## Usage

```python
from statewager.states import nevada, ohio

results = ohio.fetch("2025-03", "2026-02", cache_dir="cache", offline=True)
for row in results:
    print(row.state, row.month, row.handle, row.revenue, row.hold)
```

Each state module exposes:

```python
fetch(
    month_start,
    month_end=None,
    *,
    cache_dir=None,
    local_dir=None,
    offline=False,
    log=None,
    report=None,
) -> list[StateMonthResult]
```

## Submodules

- `statewager.model` — `StateMonthResult`, `OperatorMonthResult`, date/month helpers, validation, reconciliation, logging, and stale-month detection
- `statewager.cache` — local file cache helpers (`load_cached_or_fetch`, `cache_path`, `load_cache`, `save_cache`, `ensure_cache_dir`)
- `statewager.http` — HTTP retry/backoff helpers, Wayback Machine fallback, and URL variant probing
- `statewager.parse` — currency parsing, PDF/OCR extraction, Excel conversion/sheet lookup, and fuzzy label matching
- `statewager.discover` — HTML anchor crawling, filename classification, and `DiscoveryReport`
- `statewager.quality` — SportsHandle/UNLV cross-checks and trailing anomaly detection
- `statewager.states` — per-state regulatory fetchers
