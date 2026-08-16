from __future__ import annotations

import os
from pathlib import Path


def resolve_data_path(configured: str) -> Path:
    """Resolve a configured path (e.g. `data/feature_phone_clank.db`)
    relative to the current working directory, same convention as OEM
    Radar's `paths.py`."""
    # Native field-test builds opt into an explicit application-data boundary.
    # Existing server/default invocations retain their current CWD-relative
    # convention when the override is absent.
    data_dir = os.environ.get("FEATURE_PHONE_CLANK_DATA_DIR")
    if data_dir and configured == "data/feature_phone_clank.db":
        directory = Path(data_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "feature_phone_clank.db"
    return Path(configured).resolve()
