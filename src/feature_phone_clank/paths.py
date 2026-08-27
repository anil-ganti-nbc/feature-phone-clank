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
    #
    # Originally special-cased to the single literal
    # "data/feature_phone_clank.db" path; generalized (2026-08-27, QC/GUI
    # parity pass) to any "data/<name>" path so every sibling file that
    # belongs next to the main database -- the experimental db, and the QC
    # archive db added this pass -- moves with it under
    # FEATURE_PHONE_CLANK_DATA_DIR instead of silently falling back to a
    # CWD-relative path and diverging from an isolated/field-test database
    # directory (a real bug this generalization fixes: the QC archive db
    # was resolving against the real repo cwd even inside an isolated
    # FEATURE_PHONE_CLANK_DATA_DIR test/field-test run).
    data_dir = os.environ.get("FEATURE_PHONE_CLANK_DATA_DIR")
    configured_path = Path(configured)
    if data_dir and configured_path.parent.as_posix() == "data":
        directory = Path(data_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / configured_path.name
    return configured_path.resolve()


def resolve_config_path(name: str) -> Path:
    root = os.environ.get("FEATURE_PHONE_CLANK_CONFIG_ROOT")
    return (Path(root).resolve() if root else Path.cwd()) / "config" / name
