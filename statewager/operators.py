"""Operator display-name normalization and cross-state brand classification."""
from __future__ import annotations

import functools
import re

from .parse import clean_lower, collapse_ws, strip_superscripts
from .states import STATE_ABBR_TO_SLUG

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

_KS_CROSSING_RE = re.compile(r"^ks crossing\s*-\s*", re.IGNORECASE)
_DOT_ABBREVS: frozenset[str] = frozenset({"inc", "ltd", "corp"})
_DOT_ENTITY_ABBREVS: frozenset[str] = _DOT_ABBREVS | {"llc", "llp", "lp"}
_PUNCT_TRANS = str.maketrans({"\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-"})
_MULTI_HYPHEN_PART_RE = re.compile(r"^[a-z0-9']{2,}(?:-[a-z0-9']{2,})+$", re.IGNORECASE)

_SPECIAL_WORDS: dict[str, str] = {
    "inc": "Inc.",
    "ltd": "Ltd.",
    "corp": "Corp.",
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
    "getsbk": "getSBK",
    "fhr-illinois": "FHR-Illinois",
    "iowa": "Iowa",
    "illinois": "Illinois",
}

_LOWER_CONNECTORS: frozenset[str] = frozenset({"at", "and", "of", "the", "for", "in", "by", "on"})

_CANONICAL_OVERRIDES: dict[str, str] = {
    "jack cleveland (betjack)": "Jack Cleveland - BetJack",
    "hollywood columbus - penn interactive (tsb)": "Hollywood Columbus - Penn Interactive (theScore Bet)",
    "hollywood - thescore": "Hollywood - theScore Bet",
    "hollywood - the score": "Hollywood - theScore Bet",
    "caesar's sportsbook-harrah's": "Caesars Sportsbook - Harrah's",
    "fanatics-ameristar kc": "Fanatics - Ameristar KC",
    "fhr-illinois llc": "FHR-Illinois, LLC",
}


def _cap_word(word: str) -> str:
    if not word:
        return word
    return word[0].upper() + word[1:].lower()


def _normalize_single_part(
    core: str,
    *,
    allow_connector_lower: bool,
    allow_in_connector: bool = False,
) -> str:
    low = core.lower()
    if low in _SPECIAL_WORDS:
        return _SPECIAL_WORDS[low]
    if allow_connector_lower and low in _LOWER_CONNECTORS and (low != "in" or allow_in_connector):
        return low
    upper = core.upper()
    if upper in _UPPER_ACRONYMS:
        return upper
    for sep in ("/", "-"):
        if sep in core:
            return sep.join(
                _normalize_single_part(sub, allow_connector_lower=idx > 0)
                for idx, sub in enumerate(core.split(sep))
            )
    return _cap_word(core)


def _normalize_token(
    token: str,
    *,
    allow_connector_lower: bool,
    allow_in_connector: bool = False,
) -> str:
    if token == "-":
        return "-"
    match = _WRAP_TOKEN_RE.match(token)
    if not match or not match.group(2):
        return token
    prefix, core, suffix = match.groups()
    if core.lower() in _DOT_ENTITY_ABBREVS and suffix.startswith("."):
        suffix = suffix[1:]
    normalized = _normalize_single_part(
        core,
        allow_connector_lower=allow_connector_lower and "(" not in prefix,
        allow_in_connector=allow_in_connector and not suffix,
    )
    return f"{prefix}{normalized}{suffix}"


def _expand_hyphen_tokens(text: str) -> str:
    spaced = _SEPARATOR_HYPHEN_RE.sub(" - ", text)
    tokens: list[str] = []
    for tok in spaced.split():
        if "-" in tok and tok != "-":
            match = _WRAP_TOKEN_RE.match(tok)
            if match and match.group(2):
                prefix, core, suffix = match.groups()
                if (
                    core.lower() not in _SPECIAL_WORDS
                    and _MULTI_HYPHEN_PART_RE.match(core)
                ):
                    parts = core.split("-")
                    parts[0] = f"{prefix}{parts[0]}"
                    parts[-1] = f"{parts[-1]}{suffix}"
                    tokens.extend(parts[0:1])
                    for part in parts[1:]:
                        tokens.extend(["-", part])
                    continue
        tokens.append(tok)
    return " ".join(tokens)


@functools.lru_cache(maxsize=1024)
def _normalize_operator_cached(raw: str) -> str:
    cleaned = collapse_ws(strip_superscripts(raw).translate(_PUNCT_TRANS))
    if not cleaned:
        return ""
    cleaned = _KS_CROSSING_RE.sub("Kansas Crossing - ", cleaned, count=1)

    spaced = _expand_hyphen_tokens(cleaned)
    lookup_key = spaced.lower()
    if lookup_key in _CANONICAL_OVERRIDES:
        return _CANONICAL_OVERRIDES[lookup_key]

    parts = spaced.split()
    out: list[str] = []
    at_segment_start = True
    for idx, tok in enumerate(parts):
        if tok == "-":
            out.append("-")
            at_segment_start = True
            continue
        next_tok = parts[idx + 1] if idx + 1 < len(parts) else "-"
        norm = _normalize_token(
            tok,
            allow_connector_lower=not at_segment_start,
            allow_in_connector=next_tok != "-",
        )
        out.append(norm)
        at_segment_start = False

    return " ".join(out).rstrip()


def normalize_operator_name(raw: str, *, state: str | None = None) -> str:
    """Normalize regulator operator/venue names away from ALL-CAPS into readable display names."""
    del state
    return _normalize_operator_cached(str(raw or ""))


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
    ("the score", "PENN / ESPN Bet"),
    ("barstool", "PENN / ESPN Bet"),
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


def _normalize_state_arg(state: str | None) -> str | None:
    if not state:
        return None
    cleaned = clean_lower(state)
    return STATE_ABBR_TO_SLUG.get(cleaned, cleaned).title()


@functools.lru_cache(maxsize=1024)
def classify_operator_brand(raw: str, *, state: str | None = None) -> str:
    """Map an operator/licensee string to its canonical cross-state sportsbook brand."""
    low = clean_lower(strip_superscripts(raw).translate(_PUNCT_TRANS))
    if not low:
        return "Other / Retail Venue"

    norm_state = _normalize_state_arg(state)
    # Iowa separates retail casino books from online operator LLCs; keep retail rows unattributed.
    if norm_state == "Iowa" and "(retail)" in low:
        return "Other / Retail Venue"

    for needle, brand in _DIRECT_BRAND_PATTERNS:
        if needle in low:
            return brand

    if state_patterns := (_STATE_PROPERTY_BRANDS.get(norm_state) if norm_state else None):
        for needle, brand in state_patterns:
            if needle in low:
                return brand

    return "Other / Retail Venue"
