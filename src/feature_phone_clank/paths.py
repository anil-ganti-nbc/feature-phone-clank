from __future__ import annotations

from pathlib import Path


def resolve_data_path(configured: str) -> Path:
    """Resolve a configured path (e.g. `data/feature_phone_clank.db`)
    relative to the current working directory, same convention as OEM
    Radar's `paths.py`."""
    return Path(configured).resolve()
