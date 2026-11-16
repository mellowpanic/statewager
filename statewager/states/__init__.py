"""Per-state sports wagering fetchers."""
from __future__ import annotations

import importlib
from types import ModuleType

ALL_STATES: tuple[str, ...] = (
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "louisiana",
    "mississippi",
    "missouri",
    "nevada",
    "ohio",
    "pennsylvania",
)

STATE_ABBR_TO_SLUG: dict[str, str] = {
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "la": "louisiana",
    "ms": "mississippi",
    "mo": "missouri",
    "nv": "nevada",
    "oh": "ohio",
    "pa": "pennsylvania",
}


def load_state_module(state: str) -> ModuleType:
    """Import and return ``statewager.states.<state>``."""
    slug = state.strip().lower().replace(" ", "_")
    slug = STATE_ABBR_TO_SLUG.get(slug, slug)
    if slug not in ALL_STATES:
        raise ValueError(f"Unsupported state {state!r}; expected one of {ALL_STATES}")
    return importlib.import_module(f"statewager.states.{slug}")


__all__ = ["ALL_STATES", "STATE_ABBR_TO_SLUG", "load_state_module"]
