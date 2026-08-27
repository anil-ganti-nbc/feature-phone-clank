"""Thin Windows launcher for the Feature Phone Clank dashboard.

Unlike native/macos/launcher.py (a packaged .app bundle that isolates its
state under ~/Library/Application Support, since a frozen app bundle has no
"repo" of its own), this Windows deployment runs directly out of a checked
out git clone: the desktop launcher (`_Launchers\\Feature Phone Clank
Dashboard.cmd`) cds into the repo and invokes this file with the repo's own
.venv\\Scripts\\python.exe, and the real collector runs via
`.venv\\Scripts\\feature-phone-clank.exe run` (manually, or via the
Task Scheduler entry -> scripts\\run-production.cmd) using the CLI's
default, CWD-relative `data/feature_phone_clank.db`.

For the dashboard to ever show real data, it MUST read the exact same
database file the collector writes to. So this launcher deliberately does
NOT set FEATURE_PHONE_CLANK_DATA_DIR to a per-user app-data directory the
way the macOS bundle does -- leaving it unset makes
`paths.resolve_data_path()` fall back to its CWD-relative default
(repo/data/feature_phone_clank.db), the same file the CLI and the
scheduled task use. To make that CWD-relative resolution correct
regardless of who launched this script (double-click, a differently-cwd'd
shell, Task Scheduler, ...), we explicitly chdir to the repo root derived
from this file's own location before anything else runs (requirement: the
launcher resolves paths relative to itself, not the caller's cwd).

The dashboard is unconditionally read-only in dashboard.py during Phase 0
(POST always 403s), so no mutation risk here regardless of the
LocalCollectionController wiring -- collection must be run out-of-band via
the CLI (`feature-phone-clank.exe run`) or scripts\\run-collection.cmd.
"""
from __future__ import annotations
import logging
import os, socket, threading, webbrowser, sys, time, urllib.request
from pathlib import Path

# native/windows/launcher.py -> native/windows -> native -> <repo root>
#
# When frozen by PyInstaller (onefile), __file__ points into the
# transient _MEIPASS extraction directory, not the checked-out repo --
# parents[2] off of that is meaningless (and that directory is wiped
# when the process exits). In that case there is no "own location" to
# derive the repo root from, so we trust the process's starting working
# directory instead: the frozen build's contract (see native/windows,
# and Feature Phone Clank.spec) is that it must be launched with the
# repo root as its CWD, exactly like the source-run case documented
# above (the desktop launcher / any wrapper is expected to `cd` into the
# repo before invoking the .exe -- a bare double-click from a directory
# that is not the repo root will not find repo/data).
if getattr(sys, "frozen", False):
    REPO_ROOT = Path.cwd().resolve()
else:
    REPO_ROOT = Path(__file__).resolve().parents[2]
os.chdir(REPO_ROOT)  # everything below resolves relative to the repo, not the caller's cwd

LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "dashboard.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("feature_phone_clank.native.windows")

from feature_phone_clank.dashboard import serve


def main():
    # Local field-test state (currently just the dashboard's advisory lock
    # file) lives under repo/data by default -- the same directory the CLI
    # already uses for the db and its own lock -- unless explicitly
    # overridden. FEATURE_PHONE_CLANK_DATA_DIR is intentionally left unset
    # (see module docstring): the dashboard/controller must resolve the db
    # path exactly like the CLI does.
    state = Path(os.environ.get("FEATURE_PHONE_FIELD_TEST_HOME") or (REPO_ROOT / "data")).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL", "")
    if hasattr(sys, "_MEIPASS"):
        os.environ["FEATURE_PHONE_CLANK_CONFIG_ROOT"] = str(Path(sys._MEIPASS).resolve())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from feature_phone_clank.local_collection import LocalCollectionController
    from feature_phone_clank.paths import resolve_config_path, resolve_data_path

    db_path = resolve_data_path("data/feature_phone_clank.db")
    log.info("Repo root: %s", REPO_ROOT)
    log.info("Database (same file the CLI/scheduled run write to): %s", db_path)
    log.info("Database exists: %s", db_path.exists())

    experimental_db_path = resolve_data_path("data/feature_phone_clank_experimental.db")
    controller = LocalCollectionController(
        db_path, state / "feature-phone-clank.lock", resolve_config_path("scope.yaml"), resolve_config_path("hmd_overrides.yaml"),
        experimental_db=experimental_db_path, experimental_lock_path=state / "feature-phone-clank-experimental.lock",
    )
    server = serve(port=port, controller=controller)
    url = f"http://127.0.0.1:{port}/"

    def ready():
        for _ in range(200):
            try:
                if urllib.request.urlopen(url + "healthz", timeout=1).status == 200:
                    if os.environ.get("FEATURE_PHONE_CLANK_NO_BROWSER") != "1":
                        webbrowser.open(url)
                    log.info("Feature Phone Clank dashboard -> %s", url)
                    print(f"Feature Phone Clank dashboard -> {url}")
                    print("This dashboard is read-only. To collect real data, run a collection")
                    print(r"manually: scripts\run-collection.cmd (or .venv\Scripts\feature-phone-clank.exe run)")
                    return
            except Exception:
                time.sleep(.15)
        log.error("Dashboard server never became healthy after 30s.")

    threading.Thread(target=ready, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
