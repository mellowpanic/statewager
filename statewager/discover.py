"""Anchor crawling, filename classification, and discovery diagnostics."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import os
import re
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

from .http import fetch_text
from .model import MONTH_NAME_TO_NUM

if TYPE_CHECKING:
    import requests

MONTH_NAMES: dict[str, int] = MONTH_NAME_TO_NUM

_FULL_MONTH_PAT = (
    r"january|february|march|april|may|june|july|august|september|october|november|december"
)
_ABBR_MONTH_PAT = r"jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec"

MONTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        rf"(?<!\d)(?P<year>20\d{{2}})[\W_]{{0,2}}(?P<month>0[1-9]|1[0-2])(?!\d)"
        rf"(?:[\W_]{{0,2}}(?P<name>{_FULL_MONTH_PAT}|{_ABBR_MONTH_PAT}))?"
    ),
    re.compile(r"(?P<month>0[1-9]|1[0-2])[\W_]{1,2}(?P<year>20\d{2})"),
    re.compile(rf"(?P<year>20\d{{2}})[\W_]{{1,2}}(?P<name>{_FULL_MONTH_PAT})"),
    re.compile(rf"(?P<name>{_FULL_MONTH_PAT})[\W_]{{1,2}}(?P<year>20\d{{2}})"),
    re.compile(
        rf"(?:(?<=^)|(?<=[\W_]))"
        rf"(?P<name>{_ABBR_MONTH_PAT})"
        rf"[\W_]{{1,2}}(?P<year>20\d{{2}})"
    ),
]

TEXT_MONTH_RE: re.Pattern[str] = re.compile(
    r"\b("
    + "|".join(sorted(MONTH_NAMES.keys(), key=len, reverse=True))
    + r")\b(?:\s+report)?(?:\s*\(pdf\))?[\s,\-]+(20\d{2})",
    re.IGNORECASE,
)

DEFAULT_CHANNEL_KEYWORDS: dict[str, list[str]] = {
    "retail": ["retail"],
    "mobile": ["mobile", "online"],
}
_DEFAULT_CHANNEL_ORDER: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((word, channel) for channel, words in DEFAULT_CHANNEL_KEYWORDS.items() for word in words if word),
        key=lambda item: (-len(item[0]), item[1]),
    )
)

_SPORTSBOOK_HINT_RE = re.compile(
    r"(sportsbook|sports[\s_-]?book|sports[\s_-]?wagering|"
    r"(?:^|[\s_/.-])sb(?:[\s_/.-]|$))"
)
_PER_MONTH_SUFFIX_RE = re.compile(r"20\d{2}[_\- ](0[1-9]|1[0-2])(?:[_\- ]|\.|$)")
_TIMESTAMP_RE = re.compile(r"20\d{2}(0[1-9]|1[0-2])[0-3]\d")


def normalize_filename(value: str) -> str:
    """Lowercase, HTML-unescape, collapse whitespace."""
    if not value:
        return ""
    return " ".join(unescape(str(value)).lower().split())


def extract_month_label(name: str) -> str | None:
    """Return 'Mon YYYY' from a string, preferring the trailing match."""
    if not name:
        return None
    normalized = normalize_filename(name)
    candidates: list[tuple[int, int, int]] = []
    for pattern in MONTH_PATTERNS:
        for match in pattern.finditer(normalized):
            year = int(match.group("year"))
            month_token = match.groupdict().get("month")
            if month_token is not None:
                month_num = int(month_token)
            else:
                name_token = match.group("name")
                month_num = MONTH_NAMES.get(name_token) if name_token else None
                if month_num is None:
                    continue
            if not (1 <= month_num <= 12):
                continue
            name_token = match.groupdict().get("name")
            if name_token:
                expected = MONTH_NAMES.get(name_token)
                if expected and expected != month_num:
                    continue
            candidates.append((match.start(), year, month_num))
    if not candidates:
        return None
    candidates.sort(key=lambda triple: triple[0])
    _, year, month_num = candidates[-1]
    return datetime(year, month_num, 1).strftime("%b %Y")


def parse_text_month(text: str) -> str | None:
    """Extract the first spelled-out ``Mon YYYY`` label matching ``TEXT_MONTH_RE``."""
    match = TEXT_MONTH_RE.search(text)
    if not match:
        return None
    month_num = MONTH_NAMES.get(match.group(1).lower())
    if month_num is None:
        return None
    return datetime(int(match.group(2)), month_num, 1).strftime("%b %Y")


def _resolve_channel(combined: str, channel_keywords: dict[str, list[str]] | None) -> str | None:
    if channel_keywords is None:
        for word, channel in _DEFAULT_CHANNEL_ORDER:
            if word in combined:
                return channel
        return None
    matches: list[tuple[int, str]] = []
    for channel, words in channel_keywords.items():
        for word in words:
            if word and word in combined:
                matches.append((len(word), channel))
    if not matches:
        return None
    matches.sort(key=lambda pair: (-pair[0], pair[1]))
    return matches[0][1]


def _combined_anchor_text(href: str, title: str | None, text: str | None) -> str:
    filename = os.path.basename(urlparse(href).path) or href
    return normalize_filename(" ".join([filename, href, title or "", text or ""]))


def classify_sportsbook_filename(
    href: str,
    *,
    title: str | None = None,
    text: str | None = None,
    channel_keywords: dict[str, list[str]] | None = None,
    exclude_cumulative: bool = True,
) -> tuple[str, str] | None:
    """Classify a sportsbook file anchor by ``(month-label, channel)``."""
    if not href:
        return None
    combined = _combined_anchor_text(href, title, text)
    if not _SPORTSBOOK_HINT_RE.search(combined):
        return None
    if exclude_cumulative and _TIMESTAMP_RE.search(combined):
        return None
    channel = _resolve_channel(combined, channel_keywords)
    if channel is None:
        return None
    label = extract_month_label(combined)
    if not label:
        return None
    return label, channel


_YEAR_RANGE_RE = re.compile(r"(?<!\d)(?P<a>20\d{2})[_\- ](?P<b>20\d{2})(?!\d)")
_FY_RANGE_RE = re.compile(r"fy[_\- ]?(?P<a>\d{2})[_\- ](?P<b>\d{2})(?!\d)")
_FY_SOLO_RE = re.compile(r"fy[_\- ]?(?P<fy>\d{2})(?!\d)")


def classify_cumulative_filename(
    href: str,
    *,
    title: str | None = None,
    text: str | None = None,
    channel_keywords: dict[str, list[str]] | None = None,
) -> tuple[set[str], str] | None:
    """Identify cumulative/FY-spanning sportsbook files."""
    if not href:
        return None
    combined = _combined_anchor_text(href, title, text)

    has_per_month = _PER_MONTH_SUFFIX_RE.search(combined)
    has_timestamp = _TIMESTAMP_RE.search(combined)
    if has_per_month and not has_timestamp:
        return None
    if not _SPORTSBOOK_HINT_RE.search(combined):
        return None
    channel = _resolve_channel(combined, channel_keywords)
    if channel is None:
        return None

    cleaned = _TIMESTAMP_RE.sub(" ", combined)
    range_match = _YEAR_RANGE_RE.search(cleaned)
    fy_range_match = _FY_RANGE_RE.search(cleaned)
    fy_solo_match = _FY_SOLO_RE.search(cleaned)
    if range_match:
        a = int(range_match.group("a"))
        b = int(range_match.group("b"))
        start_year, end_year = sorted((a, b))
    elif fy_range_match:
        a = 2000 + int(fy_range_match.group("a"))
        b = 2000 + int(fy_range_match.group("b"))
        start_year, end_year = sorted((a, b))
    elif fy_solo_match:
        end_year = 2000 + int(fy_solo_match.group("fy"))
        start_year = end_year - 1
    else:
        return None
    if not (0 < end_year - start_year <= 10):
        return None
    months: set[str] = set()
    y, m = start_year, 7
    while (y, m) <= (end_year, 6):
        months.add(datetime(y, m, 1).strftime("%b %Y"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months, channel


class AnchorParser(HTMLParser):
    """Collect anchor ``href`` / ``title`` / ``text`` triples from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._flush()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        self._anchor = {
            "href": attr_map.get("href", ""),
            "title": attr_map.get("title", ""),
        }
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._flush()

    def close(self) -> None:
        self._flush()
        super().close()

    def _flush(self) -> None:
        if self._anchor is None:
            return
        self._anchor["text"] = " ".join(part.strip() for part in self._text_parts if part.strip())
        self.links.append(self._anchor)
        self._anchor = None
        self._text_parts = []


def crawl_anchors(
    url: str,
    *,
    retries: int = 3,
    timeout: int = 30,
    session: requests.Session | None = None,
    headers: dict[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Fetch ``url`` and return a list of ``{href, title, text}`` dicts."""
    try:
        html = fetch_text(url, retries=retries, timeout=timeout, headers=headers, session=session)
    except Exception as exc:
        if log:
            log(f"WARN could not fetch anchor index {url}: {exc}")
        return []
    if not html:
        if log:
            log(f"WARN anchor index {url} returned no body")
        return []
    parser = AnchorParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        if log:
            log(f"WARN HTML parse failed for {url}: {exc}")
        return []
    return parser.links


class DiscoveryReport:
    """Structured diagnostic record for ``--explain`` mode."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.index_urls: list[str] = []
        self.anchors_total: dict[str, int] = {}
        self.classified: dict[str, list[str]] = {}
        self.unmatched: list[tuple[str, str, str, str]] = []
        self.matches: dict[str, str] = {}
        self.probes: dict[str, list[tuple[str, str, str]]] = {}
        self.http_status: Counter[int | str] = Counter()
        self.notes: list[str] = []

    def add_index(self, url: str, anchor_count: int = 0) -> None:
        if url not in self.index_urls:
            self.index_urls.append(url)
        self.anchors_total[url] = self.anchors_total.get(url, 0) + max(0, anchor_count)

    def add_anchor(self, key: str, href: str) -> None:
        """Record a successfully classified anchor."""
        self.classified.setdefault(key, []).append(href)

    def add_classification(
        self,
        href: str,
        *,
        title: str = "",
        text: str = "",
        reason: str,
    ) -> None:
        """Record an anchor that did NOT classify (with a reason)."""
        self.unmatched.append((href, title, text, reason))

    def add_match(self, key: str, url: str) -> None:
        """Record the URL we chose for a (month, channel) target."""
        self.matches[key] = url

    def add_probe(self, key: str, url: str, *, status: object = "", reason: str = "") -> None:
        """Record a URL we tried (success or failure) per target."""
        self.probes.setdefault(key, []).append((url, str(status), reason))
        if isinstance(status, int) and not isinstance(status, bool):
            self.http_status[status] += 1

    def add_status(self, status: int) -> None:
        self.http_status[status] += 1

    def note(self, message: str) -> None:
        self.notes.append(message)

    def render(self, *, max_unmatched: int = 20) -> str:
        lines: list[str] = []
        lines.append(f"=== DiscoveryReport: {self.state} ===")
        if self.index_urls:
            lines.append("Index URLs crawled:")
            for url in self.index_urls:
                lines.append(f"  - {url}  (anchors={self.anchors_total.get(url, 0)})")
        else:
            lines.append("Index URLs crawled: (none)")
        lines.append(
            f"Anchors: total={sum(self.anchors_total.values())} "
            f"classified={sum(len(v) for v in self.classified.values())} "
            f"unmatched={len(self.unmatched)}"
        )
        if self.classified:
            lines.append("Classified anchors:")
            for c_key in sorted(self.classified):
                hrefs = self.classified[c_key]
                lines.append(f"  {c_key}: {len(hrefs)}")
                for href in hrefs[:3]:
                    lines.append(f"    -> {href}")
                if len(hrefs) > 3:
                    lines.append(f"    ... and {len(hrefs) - 3} more")
        if self.matches:
            lines.append("Chosen URL per target:")
            for m_key in sorted(self.matches):
                lines.append(f"  {m_key}: {self.matches[m_key]}")
        if self.probes:
            lines.append("Probes:")
            for p_key in sorted(self.probes):
                lines.append(f"  {p_key}:")
                for url, status, reason in self.probes[p_key]:
                    bits = [url]
                    if status:
                        bits.append(f"status={status}")
                    if reason:
                        bits.append(f"reason={reason}")
                    lines.append("    " + " ".join(bits))
        if self.unmatched:
            lines.append(f"Unmatched anchors (first {max_unmatched} of {len(self.unmatched)}):")
            for href, title, text, reason in self.unmatched[:max_unmatched]:
                hint = title or text
                line = f"  - {href}"
                if hint:
                    line += f"  [\"{hint[:60]}\"]"
                line += f"   reason={reason}"
                lines.append(line)
        if self.http_status:
            histogram = ", ".join(
                f"{code}: {count}"
                for code, count in sorted(self.http_status.items(), key=lambda kv: str(kv[0]))
            )
            lines.append(f"HTTP status histogram: {histogram}")
        if self.notes:
            lines.append("Notes:")
            for note in self.notes:
                lines.append(f"  - {note}")
        lines.append("=" * 32)
        return "\n".join(lines)
