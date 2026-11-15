"""Standalone US state sports wagering regulatory fetcher and parser."""
from __future__ import annotations

from . import cache, discover, http, model, operators, parse, quality
from .model import CORE_RESULT_FIELDS, OperatorMonthResult, StateMonthResult
from .operators import BRAND_ORDER, classify_operator_brand, normalize_operator_name
from .states import ALL_STATES

STATE_NAMES: tuple[str, ...] = ALL_STATES

__all__ = [
    "BRAND_ORDER",
    "CORE_RESULT_FIELDS",
    "OperatorMonthResult",
    "STATE_NAMES",
    "StateMonthResult",
    "cache",
    "classify_operator_brand",
    "discover",
    "http",
    "model",
    "normalize_operator_name",
    "operators",
    "parse",
    "quality",
]
