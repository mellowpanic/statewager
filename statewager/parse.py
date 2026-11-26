"""Currency, PDF/OCR, Excel, and fuzzy label parsing helpers."""
from __future__ import annotations

from contextlib import contextmanager
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, Iterator
import zipfile

from .model import hold_pct, round_or_blank, safe_float

_LABEL_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_MONEY_STRIP_RE = re.compile(r"[\$,\s]+")
_SUPERSCRIPT_RE = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+")
_PLACEHOLDERS = frozenset({"", "+", "-", "—", "–", "n/a", "na", "none", "null"})
PDFPLUMBER_LOCK = threading.Lock()
XLSX_MAGIC = b"PK"
XLS_BIFF_MAGIC = b"\xd0\xcf\x11\xe0"
XLS_SIGNATURES = (XLSX_MAGIC, XLS_BIFF_MAGIC)
PDF_MAGIC = b"%PDF"


def is_xlsx_bytes(data: bytes | None) -> bool:
    """Return True when ``data`` starts with the OOXML ZIP header."""
    return bool(data) and data.startswith(XLSX_MAGIC)


def is_xls_bytes(data: bytes | None) -> bool:
    """Return True when ``data`` starts with an XLSX or BIFF XLS header."""
    return bool(data) and data.startswith(XLS_SIGNATURES)


def is_pdf_bytes(data: bytes | None) -> bool:
    """Return True when ``data`` starts with the ``%PDF`` header."""
    return bool(data) and data.startswith(PDF_MAGIC)


def collapse_ws(value: object) -> str:
    """Collapse internal whitespace to single spaces and strip ends."""
    return " ".join(str(value or "").split())


def clean_lower(value: object) -> str:
    """Collapse whitespace and lowercase a text cell or label."""
    return collapse_ws(value).lower()


def strip_superscripts(value: object) -> str:
    """Remove Unicode footnote superscript digits from a text label."""
    return _SUPERSCRIPT_RE.sub("", str(value or ""))


def _which(*names: str) -> str | None:
    """Return the first available executable path on PATH."""
    return next((path for name in names if (path := shutil.which(name))), None)


_MAX_XLSX_ENTRIES = 2000
_MAX_XLSX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_XLSX_COMPRESSION_RATIO = 200


def _validate_xlsx_zip(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_XLSX_ENTRIES:
            raise ValueError(f"XLSX archive has too many entries ({len(infos)} > {_MAX_XLSX_ENTRIES})")
        total_uncompressed = sum(max(0, info.file_size) for info in infos)
        if total_uncompressed > _MAX_XLSX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"XLSX uncompressed size {total_uncompressed} exceeds limit {_MAX_XLSX_UNCOMPRESSED_BYTES}"
            )
        total_compressed = sum(max(0, info.compress_size) for info in infos)
        if total_uncompressed > 10 * 1024 * 1024 and (
            total_compressed == 0 or (total_uncompressed / total_compressed) > _MAX_XLSX_COMPRESSION_RATIO
        ):
            raise ValueError("XLSX archive exceeds compression ratio safety threshold")


@contextmanager
def open_xlsx_workbook(
    data: bytes | None,
    *,
    openpyxl_mod: object = None,
    log: Callable[[str], None] | None = None,
    error_prefix: str = "could not open workbook",
) -> Iterator[object | None]:
    """Open XLSX bytes in read-only data-only mode, yielding None on empty/corrupt input."""
    if not data:
        yield None
        return
    if openpyxl_mod is None:
        import openpyxl as openpyxl_mod
    try:
        _validate_xlsx_zip(data)
        workbook = openpyxl_mod.load_workbook(io.BytesIO(data), data_only=True, read_only=True)  # type: ignore[attr-defined]
    except Exception as exc:
        if log:
            log(f"WARN {error_prefix}: {exc}")
        yield None
        return
    try:
        yield workbook
    finally:
        workbook.close()  # type: ignore[attr-defined]

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
    if text.lower() in _PLACEHOLDERS:
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


_parse_money = parse_dollar


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
    r_hold = hold_pct(retail_handle, retail_revenue)
    m_hold = hold_pct(mobile_handle, mobile_revenue)
    return [
        label,
        round_or_blank(handle),
        round_or_blank(revenue),
        hold if hold is not None else "",
        round_or_blank(retail_handle),
        round_or_blank(retail_revenue),
        r_hold if r_hold is not None else "",
        round_or_blank(mobile_handle),
        round_or_blank(mobile_revenue),
        m_hold if m_hold is not None else "",
    ]


def _validate_page_range(first_page: int | None, last_page: int | None) -> None:
    if first_page is not None and (isinstance(first_page, bool) or first_page < 1):
        raise ValueError(f"first_page must be >= 1, got {first_page!r}")
    if last_page is not None and (isinstance(last_page, bool) or last_page < 1):
        raise ValueError(f"last_page must be >= 1, got {last_page!r}")
    if first_page is not None and last_page is not None and last_page < first_page:
        raise ValueError(f"last_page ({last_page}) must be >= first_page ({first_page})")


def extract_pdf_text_with_ocr(
    data: bytes,
    *,
    first_page: int = 1,
    last_page: int | None = None,
    dpi: int = 300,
    max_pages: int = 100,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """Last-resort OCR for image-based PDFs.

    Requires tesseract + pytesseract + pdf2image + pdftoppm. Returns None
    (with a single WARN) when any dependency is missing so the caller can
    proceed without OCR rather than crash.
    """
    _validate_page_range(first_page, last_page)
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
        capped_last = first_page + max(1, max_pages) - 1
        effective_last = min(last_page, capped_last) if last_page is not None else capped_last
        images = convert_from_bytes(
            data, dpi=dpi, first_page=first_page, last_page=effective_last,
        )
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
    layout: bool = False,
    use_pdfplumber: bool = True,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """Extract text from PDF bytes, trying pdfplumber then pdftotext.

    - ``raw=True`` or ``use_pdfplumber=False`` skips pdfplumber.
    - When pdfplumber returns empty or fails, falls back to pdftotext.
    - Returns None if both methods fail or are unavailable.
    """
    _validate_page_range(first_page, last_page)
    if not raw and use_pdfplumber:
        try:
            import pdfplumber as _pdfplumber
        except ImportError:
            _pdfplumber = None
        if _pdfplumber is not None:
            try:
                with PDFPLUMBER_LOCK, _pdfplumber.open(io.BytesIO(data)) as pdf:
                    pages = pdf.pages
                    if first_page is not None or last_page is not None:
                        start = (first_page - 1) if first_page is not None else 0
                        end = last_page if last_page is not None else len(pages)
                        pages = pages[start:end]
                    text = "\n".join(
                        (page.extract_text(layout=True) if layout else page.extract_text()) or ""
                        for page in pages
                    )
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
            tmp_path = fh.name
            fh.write(data)
        args = [pdftotext_bin]
        if raw:
            args.append("-raw")
        elif layout:
            args.append("-layout")
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
    if data.startswith(XLSX_MAGIC):
        return data
    if not data.startswith(XLS_BIFF_MAGIC):
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
    sheet_set = set(sheet_names)
    lowered = [(name, name.lower()) for name in sheet_names]
    for candidate in candidates:
        if candidate in sheet_set:
            return workbook[candidate]
        lower = candidate.lower()
        for name, name_low in lowered:
            if name_low == lower:
                return workbook[name]
        for name, name_low in lowered:
            if lower in name_low:
                return workbook[name]
    if default_index is not None and 0 <= default_index < len(sheet_names):
        return workbook[sheet_names[default_index]]
    return None
