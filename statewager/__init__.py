"""Standalone US state sports wagering regulatory fetcher and parser."""
from __future__ import annotations

from . import cache, discover, http, model, parse, quality
from .model import CORE_RESULT_FIELDS, OperatorMonthResult, StateMonthResult
from .states import ALL_STATES

STATE_NAMES: tuple[str, ...] = ALL_STATES

__all__ = [
    "CORE_RESULT_FIELDS",
    "OperatorMonthResult",
    "STATE_NAMES",
    "StateMonthResult",
    "cache",
    "discover",
    "http",
    "model",
    "parse",
    "quality",
]
