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


def load_state_module(state: str) -> ModuleType:
    """Import and return ``statewager.states.<state>``."""
    slug = state.strip().lower().replace(" ", "_")
    if slug == "ms":
        slug = "mississippi"
    if slug not in ALL_STATES:
        raise ValueError(f"Unsupported state {state!r}; expected one of {ALL_STATES}")
    return importlib.import_module(f"statewager.states.{slug}")


__all__ = ["ALL_STATES", "load_state_module"]
