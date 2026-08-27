# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).parents[1]

# Unlike Watch Clank (FastAPI/uvicorn + Jinja templates + alembic
# migrations), this dashboard is stdlib http.server with HTML built
# inline in dashboard.py -- there is no templates/ dir and no uvicorn.
# The only non-.py runtime assets it needs bundled are:
#   - providers/sqlite/schema.sql (read via package data at runtime)
#   - config/ (scope.yaml, hmd_overrides.yaml) -- resolve_config_path()
#     reads these relative to FEATURE_PHONE_CLANK_CONFIG_ROOT, which
#     launcher.py points at sys._MEIPASS when frozen.
# It deliberately does NOT bundle data/*.db -- those are operator data
# that must keep living next to the checked-out repo, not inside the
# frozen app.
a = Analysis(
    [str(root / "native" / "windows" / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "src" / "feature_phone_clank" / "providers" / "sqlite" / "schema.sql"),
         "feature_phone_clank/providers/sqlite"),
        (str(root / "config"), "config"),
    ],
    hiddenimports=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Feature Phone Clank",
    console=False,
)
