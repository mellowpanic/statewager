"""Currency, PDF/OCR, Excel, and fuzzy label parsing helpers."""
from __future__ import annotations

import functools
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable

from .model import hold_pct, round_or_blank, safe_float

_LABEL_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_MONEY_STRIP_RE = re.compile(r"[\$,\s]+")
_PLACEHOLDERS = frozenset({"", "-", "—", "–", "n/a", "na", "none", "null"})
_ORIG_SHUTIL_WHICH = shutil.which


@functools.lru_cache(maxsize=32)
def _cached_which(name: str, path_env: str) -> str | None:
    return _ORIG_SHUTIL_WHICH(name)


def _which(*names: str) -> str | None:
    """Resolve first available executable on PATH with safe test-mock awareness."""
    which_fn = shutil.which
    path_env = os.environ.get("PATH", "")
    for name in names:
        resolved = _cached_which(name, path_env) if which_fn is _ORIG_SHUTIL_WHICH else which_fn(name)
        if resolved:
            return resolved
    return None

MAIN_HEADERS: list[str] = [
    "Month", "Handle", "Revenue", "Hold%",
    "Retail Handle", "Retail Revenue", "Retail Hold%",
    "Mobile Handle", "Mobile Revenue", "Mobile Hold%",
]


def _parse_numeric_token(s: object, *, strip_percent: bool = False) -> float | None:
    """Parse a dollar/numeric token like '$1,234', '($500)', '-500', '500-' into a finite float."""
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return safe_float(s)
    text = str(s).strip()
    if not text:
        return None
    text = text.replace("\u2212", "-")
    text = _MONEY_STRIP_RE.sub("", text)
    if strip_percent:
        text = text.replace("%", "")
    elif "%" in text:
        return None
    if text.lower() in _PLACEHOLDERS or text in {"+", "-"}:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if not negative and text.endswith("-"):
        negative = True
        text = text[:-1]
    if not text or text[0] in "+-" or "(" in text or ")" in text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    if not math.isfinite(amount):
        return None
    if amount == 0.0:
        return 0.0
    return -amount if negative else amount


def parse_dollar(s: object) -> float | None:
    """Parse '$1,234,567' or '(1,234,567)' strings to float. Returns None on failure."""
    return _parse_numeric_token(s)


def _parse_money(text: object) -> float | None:
    return _parse_numeric_token(text)


def is_number(value: object) -> bool:
    """Return True for finite int/float values, excluding bool."""
    return safe_float(value) is not None


def normalize_label(text: object) -> str:
    """Normalize a cell or line for fuzzy comparison.

    Lowercase, strip punctuation/symbols, collapse whitespace.
    """
    if text is None:
        return ""
    lowered = str(text).lower()
    cleaned = _LABEL_NORM_RE.sub(" ", lowered)
    return " ".join(cleaned.split())


def label_matches(line: object, *fragments: str) -> bool:
    """True if every fragment (normalized) appears in line (normalized)."""
    if not fragments:
        return False
    normalized = normalize_label(line)
    if not normalized:
        return False
    return all(normalize_label(frag) in normalized for frag in fragments)


def build_main_row(
    label: str,
    handle: float,
    revenue: float,
    retail_handle: float | None,
    retail_revenue: float | None,
    mobile_handle: float | None,
    mobile_revenue: float | None,
) -> list[object]:
    """Build a standard 10-column main-tab row from validated metrics."""
    hold = hold_pct(handle, revenue)
    return [
        label,
        round_or_blank(handle),
        round_or_blank(revenue),
        hold if hold is not None else "",
        round_or_blank(retail_handle),
        round_or_blank(retail_revenue),
        hold_pct(retail_handle, retail_revenue) if retail_handle else "",
        round_or_blank(mobile_handle),
        round_or_blank(mobile_revenue),
        hold_pct(mobile_handle, mobile_revenue) if mobile_handle else "",
    ]


def extract_pdf_text_with_ocr(
    data: bytes,
    *,
    first_page: int = 1,
    last_page: int | None = None,
    dpi: int = 300,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """Last-resort OCR for image-based PDFs.

    Requires tesseract + pytesseract + pdf2image + pdftoppm. Returns None
    (with a single WARN) when any dependency is missing so the caller can
    proceed without OCR rather than crash.
    """
    if _which("tesseract") is None:
        if log:
            log("WARN tesseract not installed; OCR fallback unavailable (apt install tesseract-ocr)")
        return None
    try:
        import pytesseract  # type: ignore
        from pdf2image import convert_from_bytes  # type: ignore
    except ImportError:
        if log:
            log("WARN pytesseract / pdf2image not installed; OCR fallback unavailable")
        return None
    try:
        kwargs: dict[str, object] = {"dpi": dpi, "first_page": first_page}
        if last_page is not None:
            kwargs["last_page"] = last_page
        images = convert_from_bytes(data, **kwargs)
        return "\n\f\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception as exc:
        if log:
            log(f"WARN OCR extraction failed: {exc}")
        return None


def extract_pdf_text(
    data: bytes,
    *,
    first_page: int | None = None,
    last_page: int | None = None,
    raw: bool = False,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """Extract text from PDF bytes, trying pdfplumber then pdftotext.

    - ``raw=True`` skips pdfplumber and goes straight to ``pdftotext -raw``.
    - When pdfplumber returns empty or fails, falls back to pdftotext.
    - Returns None if both methods fail or are unavailable.
    """
    try:
        import pdfplumber as _pdfplumber
    except ImportError:
        _pdfplumber = None

    if not raw and _pdfplumber is not None:
        try:
            import io as _io
            with _pdfplumber.open(_io.BytesIO(data)) as pdf:
                pages = pdf.pages
                if first_page is not None or last_page is not None:
                    start = (first_page or 1) - 1
                    end = last_page if last_page is not None else len(pages)
                    pages = pages[start:end]
                text = "\n".join(page.extract_text() or "" for page in pages)
                if text.strip():
                    return text
        except Exception as exc:
            if log:
                log(f"WARN pdfplumber failed: {exc}")

    pdftotext_bin = _which("pdftotext")
    if pdftotext_bin is None:
        if log:
            log("WARN pdftotext is not installed")
        return None

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            tmp_path = fh.name
        args = [pdftotext_bin]
        if raw:
            args.append("-raw")
        if first_page is not None:
            args.extend(["-f", str(first_page)])
        if last_page is not None:
            args.extend(["-l", str(last_page)])
        args.extend([tmp_path, "-"])
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            if log:
                log(f"WARN pdftotext execution failed: {exc}")
            return None
        if result.returncode != 0:
            if log:
                log(f"WARN pdftotext failed: {result.stderr.strip()}")
            return None
        return result.stdout
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def ensure_xlsx(
    data: bytes,
    *,
    log: Callable[[str], None] | None = None,
) -> bytes | None:
    """Return XLSX bytes, converting BIFF .xls to OOXML if needed.

    Returns data unchanged if already XLSX (PK magic), converts via
    libreoffice if BIFF (D0CF magic), returns None if unrecognized or
    conversion fails.
    """
    if not data:
        return None
    if data.startswith(b"PK"):
        return data
    if not data.startswith(b"\xd0\xcf\x11\xe0"):
        return None
    bin_name = _which("libreoffice", "soffice")
    if bin_name is None:
        if log:
            log("WARN libreoffice not installed; cannot convert legacy .xls")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, "src.xls")
        profile_dir = os.path.join(tmp, "lo_profile")
        os.makedirs(profile_dir, exist_ok=True)
        with open(src_path, "wb") as fh:
            fh.write(data)
        try:
            proc = subprocess.run(
                [
                    bin_name,
                    f"-env:UserInstallation={Path(profile_dir).as_uri()}",
                    "--headless",
                    "--convert-to",
                    "xlsx",
                    "--outdir",
                    tmp,
                    src_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:
            if log:
                log(f"WARN libreoffice xls→xlsx conversion failed: {exc}")
            return None
        if proc.returncode != 0:
            if log:
                err = (proc.stderr or proc.stdout or "").strip()
                log(f"WARN libreoffice xls→xlsx exited {proc.returncode}: {err}")
            return None
        out_path = os.path.join(tmp, "src.xlsx")
        if not os.path.isfile(out_path):
            if log:
                log("WARN libreoffice did not produce an xlsx output")
            return None
        with open(out_path, "rb") as fh:
            return fh.read()


def find_sheet_fuzzy(
    workbook,
    *candidates: str,
    default_index: int | None = None,
) -> object | None:
    """Find a worksheet by fuzzy name matching.

    Tries exact match, then case-insensitive, then case-insensitive
    contains. Falls back to default_index if provided.
    """
    sheet_names = workbook.sheetnames
    for candidate in candidates:
        if candidate in sheet_names:
            return workbook[candidate]
        lower = candidate.lower()
        for name in sheet_names:
            if name.lower() == lower:
                return workbook[name]
        for name in sheet_names:
            if lower in name.lower():
                return workbook[name]
    if default_index is not None and 0 <= default_index < len(sheet_names):
        return workbook[sheet_names[default_index]]
    return None
