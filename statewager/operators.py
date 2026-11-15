"""Operator display-name normalization and cross-state brand classification."""
from __future__ import annotations

import re

BRAND_ORDER: tuple[str, ...] = (
    "DraftKings",
    "FanDuel",
    "BetMGM",
    "Fanatics",
    "Caesars",
    "bet365",
    "PENN / ESPN Bet",
    "BetRivers",
    "Hard Rock Bet",
    "betPARX",
    "Circa Sports",
    "Bally Bet",
    "BetJack",
    "Other / Retail Venue",
)

_SUPERSCRIPT_RE = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+")
_WS_RE = re.compile(r"\s+")
_SEPARATOR_HYPHEN_RE = re.compile(r"(?:\s+-\s*|-\s+)")
_WRAP_TOKEN_RE = re.compile(r"^([(\[\"']*)(.*?)([)\],.;:!?\"]*)$")

_UPPER_ACRONYMS: frozenset[str] = frozenset({
    "LLC",
    "LLP",
    "LP",
    "USA",
    "US",
    "IA",
    "IL",
    "IN",
    "KS",
    "MO",
    "OH",
    "PA",
    "KC",
    "SC",
    "FC",
    "HOF",
    "MGM",
    "RSI",
    "TSB",
    "FBG",
    "FHR",
    "HC",
    "II",
    "III",
    "IV",
    "WC",
    "SBK",
    "ESPN",
    "FOX",
    "DRF",
})

_SPECIAL_WORDS: dict[str, str] = {
    "inc": "Inc.",
    "inc.": "Inc.",
    "ltd": "Ltd.",
    "ltd.": "Ltd.",
    "corp": "Corp.",
    "corp.": "Corp.",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "bet365": "bet365",
    "espnbet": "ESPN Bet",
    "espn": "ESPN",
    "thescore": "theScore",
    "betrivers": "BetRivers",
    "betparx": "betPARX",
    "ballybet": "Bally Bet",
    "bally's": "Bally's",
    "ballys": "Bally's",
    "caesar's": "Caesars",
    "caesars": "Caesars",
    "harrah's": "Harrah's",
    "harrahs": "Harrah's",
    "walker's": "Walker's",
    "betfred": "Betfred",
    "sporttrade": "Sporttrade",
    "pointsbet": "PointsBet",
    "bluebet": "BlueBet",
    "superbook": "SuperBook",
    "betjack": "BetJack",
    "mvgbet": "MVGBet",
    "sbopco": "SBOpco",
    "par-a-dice": "Par-A-Dice",
    "wynnbet": "WynnBET",
    "tipico": "Tipico",
    "unibet": "Unibet",
    "barstool": "Barstool",
    "clutchbet": "ClutchBet",
    "maximbet": "MaximBet",
    "playup": "PlayUp",
    "betway": "Betway",
    "betr": "Betr",
    "fanatics": "Fanatics",
    "fhr-illinois": "FHR-Illinois",
    "iowa": "Iowa",
    "illinois": "Illinois",
}

_LOWER_CONNECTORS: frozenset[str] = frozenset({"at", "and", "of", "the", "for", "in", "by", "on"})

_CANONICAL_OVERRIDES: dict[str, str] = {
    "jack cleveland (betjack)": "Jack Cleveland - BetJack",
    "jack cleveland - betjack": "Jack Cleveland - BetJack",
    "hollywood columbus - penn interactive (tsb)": "Hollywood Columbus - Penn Interactive (theScore Bet)",
    "hollywood columbus - penn interactive (espn bet)": "Hollywood Columbus - Penn Interactive (ESPN Bet)",
    "hollywood - espnbet": "Hollywood - ESPN Bet",
    "hollywood - thescore": "Hollywood - theScore Bet",
    "ks crossing - caesar's": "Kansas Crossing - Caesars",
    "kansas crossing - caesar's": "Kansas Crossing - Caesars",
    "ks crossing - caesars": "Kansas Crossing - Caesars",
    "kansas star - bet365": "Kansas Star - bet365",
    "caesar's sportsbook- horseshoe": "Caesars Sportsbook - Horseshoe",
    "caesar's sportsbook - horseshoe": "Caesars Sportsbook - Horseshoe",
    "caesar's sportsbook-harrah's": "Caesars Sportsbook - Harrah's",
    "caesar's sportsbook - harrah's": "Caesars Sportsbook - Harrah's",
    "caesars sportsbook": "Caesars Sportsbook",
    "fanatics-ameristar kc": "Fanatics - Ameristar KC",
    "fanatics - ameristar sc": "Fanatics - Ameristar SC",
    "fhr-illinois llc": "FHR-Illinois, LLC",
    "walker's bluff casino resort, llc": "Walker's Bluff Casino Resort, LLC",
    "penn sports interactive, llc.": "Penn Sports Interactive, LLC",
    "valley forge casino": "Valley Forge Casino",
}


def _cap_word(word: str) -> str:
    if not word:
        return word
    return word[0].upper() + word[1:].lower()


def _normalize_single_part(core: str, *, allow_connector_lower: bool) -> str:
    low = core.lower()
    if low in _SPECIAL_WORDS:
        return _SPECIAL_WORDS[low]
    if low.upper() in _UPPER_ACRONYMS:
        return low.upper()
    if "/" in core:
        return "/".join(
            _normalize_single_part(sub, allow_connector_lower=idx > 0)
            for idx, sub in enumerate(core.split("/"))
        )
    if "-" in core:
        return "-".join(
            _normalize_single_part(sub, allow_connector_lower=idx > 0)
            for idx, sub in enumerate(core.split("-"))
        )
    if allow_connector_lower and low in _LOWER_CONNECTORS:
        return low
    return _cap_word(core)


def _normalize_token(token: str, *, allow_connector_lower: bool) -> str:
    if token == "-":
        return "-"
    match = _WRAP_TOKEN_RE.match(token)
    if not match or not match.group(2):
        return token
    prefix, core, suffix = match.groups()
    if core.lower() in {"inc", "inc.", "ltd", "ltd.", "corp", "corp."} and suffix == ".":
        suffix = ""
        core = core + "."
    normalized = _normalize_single_part(
        core,
        allow_connector_lower=allow_connector_lower and "(" not in prefix,
    )
    return f"{prefix}{normalized}{suffix}"


def normalize_operator_name(raw: str, *, state: str | None = None) -> str:
    """Normalize regulator operator/venue names away from ALL-CAPS into readable display names."""
    cleaned = _SUPERSCRIPT_RE.sub("", str(raw or ""))
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return ""
    if cleaned.lower().startswith("ks crossing - "):
        cleaned = "Kansas Crossing - " + cleaned[len("KS Crossing - "):]

    lookup_key = _SEPARATOR_HYPHEN_RE.sub(" - ", cleaned).lower()
    if lookup_key in _CANONICAL_OVERRIDES:
        return _CANONICAL_OVERRIDES[lookup_key]

    spaced = _SEPARATOR_HYPHEN_RE.sub(" - ", cleaned)
    tokens = spaced.split(" ")
    out: list[str] = []
    at_segment_start = True
    for tok in tokens:
        if not tok:
            continue
        if tok == "-":
            out.append("-")
            at_segment_start = True
            continue
        norm = _normalize_token(tok, allow_connector_lower=not at_segment_start)
        out.append(norm)
        at_segment_start = False

    result = " ".join(out)
    # Fix punctuation variants produced when "Inc."/"LLC." carries trailing dot before comma/end
    result = result.replace("LLC.,", "LLC,").rstrip()
    if result.endswith("LLC."):
        result = result[:-1]
    if state == "Indiana" and result.endswith(" - getSBK"):
        result = result[:-len("getSBK")] + "getSBK"
    return result


_STATE_PROPERTY_BRANDS: dict[str, tuple[tuple[str, str], ...]] = {
    "Illinois": (
        ("casino queen", "DraftKings"),
        ("northside crown gaming", "DraftKings"),
        ("fairmount park", "FanDuel"),
        ("par-a-dice", "BetMGM"),
        ("hawthorne race course", "Fanatics"),
        ("alton casino", "Fanatics"),
        ("elgin riverboat", "Caesars"),
        ("des plaines development", "Caesars"),
        ("walker's bluff", "bet365"),
        ("hc aurora", "PENN / ESPN Bet"),
        ("hc joliet", "PENN / ESPN Bet"),
        ("midwest gaming", "BetRivers"),
        ("815 entertainment", "Hard Rock Bet"),
        ("fhr-illinois", "Circa Sports"),
    ),
    "Indiana": (
        ("ameristar casino", "DraftKings"),
        ("blue chip casino", "FanDuel"),
        ("belterra casino", "BetMGM"),
        ("harrah's hoosier park", "Caesars"),
        ("horseshoe hammond", "Caesars"),
        ("horseshoe indianapolis", "Caesars"),
        ("caesars southern indiana", "Caesars"),
        ("bally's evansville", "Bally Bet"),
        ("hard rock casino", "Hard Rock Bet"),
        ("hollywood lawrenceburg (retail)", "PENN / ESPN Bet"),
    ),
    "Pennsylvania": (
        ("valley forge", "FanDuel"),
        ("meadows", "DraftKings"),
        ("morgantown", "BetMGM"),
        ("hollywood casino york", "Fanatics"),
        ("hollywood casino", "PENN / ESPN Bet"),
        ("presque isle", "bet365"),
        ("harrah's", "Caesars"),
        ("rivers -", "BetRivers"),
        ("parx", "betPARX"),
        ("south philadelphia race", "betPARX"),
    ),
    "Missouri": (
        ("hollywood casino", "PENN / ESPN Bet"),
        ("argosy casino", "PENN / ESPN Bet"),
        ("river city casino", "PENN / ESPN Bet"),
    ),
    "Ohio": (
        ("hollywood columbus", "PENN / ESPN Bet"),
        ("hollywood gaming at dayton", "PENN / ESPN Bet"),
        ("hollywood gaming at mahoning", "PENN / ESPN Bet"),
        ("hollywood toledo", "PENN / ESPN Bet"),
    ),
}

_DIRECT_BRAND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("draftkings", "DraftKings"),
    ("crown ia gaming", "DraftKings"),
    ("fanduel", "FanDuel"),
    ("betfair interactive", "FanDuel"),
    ("betmgm", "BetMGM"),
    ("fanatics", "Fanatics"),
    ("fbg iowa", "Fanatics"),
    ("pointsbet", "Fanatics"),
    ("caesar", "Caesars"),
    ("williamhill", "Caesars"),
    ("american wagering", "Caesars"),
    ("bet365", "bet365"),
    ("hillside (iowa)", "bet365"),
    ("espn", "PENN / ESPN Bet"),
    ("thescore", "PENN / ESPN Bet"),
    ("(tsb)", "PENN / ESPN Bet"),
    ("penn interactive", "PENN / ESPN Bet"),
    ("penn sports interactive", "PENN / ESPN Bet"),
    ("betrivers", "BetRivers"),
    ("rush street", "BetRivers"),
    ("rsi oh", "BetRivers"),
    ("hard rock", "Hard Rock Bet"),
    ("parx", "betPARX"),
    ("circa", "Circa Sports"),
    ("bally", "Bally Bet"),
    ("betjack", "BetJack"),
)


def classify_operator_brand(raw: str, *, state: str | None = None) -> str:
    """Map an operator/licensee string to its canonical cross-state sportsbook brand."""
    low = _WS_RE.sub(" ", _SUPERSCRIPT_RE.sub("", str(raw or "")).strip()).lower()
    if not low:
        return "Other / Retail Venue"

    # Iowa separates retail casino books from online operator LLCs; keep retail rows unattributed.
    if state == "Iowa" and "(retail)" in low:
        return "Other / Retail Venue"

    for needle, brand in _DIRECT_BRAND_PATTERNS:
        if needle in low:
            return brand

    if state and state in _STATE_PROPERTY_BRANDS:
        for needle, brand in _STATE_PROPERTY_BRANDS[state]:
            if needle in low:
                return brand

    return "Other / Retail Venue"
