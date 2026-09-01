from __future__ import annotations

import threading
from datetime import datetime, timezone


class LocalCollectionController:
    """Manual-only local collection runner behind the dashboard.

    Nothing calls `.start()` / `.start_all_production()` at import or
    startup time anywhere in this codebase (dashboard.py only wires HTTP
    handlers that call them on an explicit POST) -- launching the GUI must
    never, by itself, start a collector.

    2026-08-27 GUI/QC parity pass: generalized from a single hardcoded
    hmd-nokia state machine to per-source_key state, so every *registered*
    collector (production and experimental/soak alike) gets an individual
    control. "Run all" deliberately only ever iterates
    `config/scope.yaml`'s `production_collectors` -- an experimental/soak
    collector (itel, lava, punkt, doro, mudita, sunbeam, tcl-alcatel; see
    docs/FEATURE_PHONE_SCOPE_EXPANSION.md) is never silently swept into
    "run all" just because it's registered. Promoting one out of soak is a
    one-line, deliberate edit to config/scope.yaml (core/scope.py) -- never
    a dashboard action.

    No notifier is ever wired to a GUI-triggered run, production or
    experimental. A Discord/webhook side effect firing because someone
    clicked a button on a local, manual, developer-machine dashboard would
    be a surprise, not a feature -- the scheduled/CLI `run` command is the
    only path that can ever attempt delivery, and only when
    FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL is explicitly set (native/
    windows/launcher.py defaults it to empty for this exact reason).
    """

    def __init__(
        self, database, lock_path, scope_path, overrides_path,
        experimental_db=None, experimental_lock_path=None,
    ):
        self.database, self.lock_path = database, lock_path
        self.scope_path, self.overrides_path = scope_path, overrides_path
        self.experimental_db = experimental_db
        self.experimental_lock_path = experimental_lock_path
        self._guard = threading.Lock()
        self._states: dict[str, dict] = {}

    # -- state ------------------------------------------------------------

    def _idle_state(self, source_key: str, mode: str) -> dict:
        return {
            "state": "idle", "source": source_key, "mode": mode,
            "started_at": None, "finished_at": None,
            "message": "Ready to collect.", "result": None,
        }

    def snapshot(self) -> dict:
        """All known per-collector states, keyed by source_key."""
        with self._guard:
            return {k: dict(v) for k, v in self._states.items()}

    def snapshot_for(self, source_key: str, mode: str = "production") -> dict:
        with self._guard:
            return dict(self._states.get(source_key) or self._idle_state(source_key, mode))

    def _is_busy_locked(self) -> bool:
        return any(s["state"] in {"queued", "running"} for s in self._states.values())

    def _set(self, source_key: str, mode: str, **kw) -> None:
        with self._guard:
            self._states.setdefault(source_key, self._idle_state(source_key, mode))
            self._states[source_key].update(mode=mode, **kw)

    # -- entry points -------------------------------------------------------

    def start(self, source_key: str, mode: str = "production"):
        """Start exactly one collector. mode='production' runs it (still
        subject to the config/scope.yaml gate inside run_production_collector)
        against the real database; mode='experimental' always runs it
        against the isolated experimental database/lock, regardless of
        scope -- the same isolation guarantee `run-experimental` gives the
        CLI."""
        from . import collectors as _collectors  # noqa: F401 — registration side effect
        from .core.registry import collectors

        if source_key not in collectors:
            return False, {"error": "unregistered_collector", "source": source_key}
        if mode == "experimental" and not (self.experimental_db and self.experimental_lock_path):
            return False, {"error": "experimental_store_not_configured"}
        with self._guard:
            if self._is_busy_locked():
                return False, {"error": "collection_already_running", **{k: dict(v) for k, v in self._states.items()}}
            self._states[source_key] = {
                "state": "queued", "source": source_key, "mode": mode,
                "started_at": None, "finished_at": None,
                "message": "Collection queued.", "result": None,
            }
        threading.Thread(target=self._run_one, args=(source_key, mode), daemon=True).start()
        return True, self.snapshot_for(source_key, mode)

    def start_all_production(self):
        """'Run all collectors': production scope ONLY. Never includes an
        experimental/soak source no matter how many are registered."""
        from .core.scope import load_scope

        scope = load_scope(self.scope_path)
        sources = list(scope.production_collectors)
        if not sources:
            return False, {"error": "no_production_collectors"}
        with self._guard:
            if self._is_busy_locked():
                return False, {"error": "collection_already_running", **{k: dict(v) for k, v in self._states.items()}}
            for sk in sources:
                self._states[sk] = {
                    "state": "queued", "source": sk, "mode": "production",
                    "started_at": None, "finished_at": None,
                    "message": "Queued as part of Run all.", "result": None,
                }
        threading.Thread(target=self._run_all_production, args=(sources,), daemon=True).start()
        return True, {"queued": sources}

    # -- worker threads -------------------------------------------------

    def _run_all_production(self, sources: list[str]) -> None:
        from .core.run_lock import LockError, RunLock
        from .core.scope import load_scope
        from .providers.sqlite import SqliteStore

        try:
            lock = RunLock.acquire(self.lock_path)
        except LockError:
            for sk in sources:
                self._set(sk, "production", state="already_running",
                          message="Another local collection is already running.",
                          finished_at=datetime.now(timezone.utc).isoformat())
            return
        store = None
        try:
            store = SqliteStore(str(self.database))
            scope = load_scope(self.scope_path)
            for sk in sources:
                self._run_single(sk, "production", store, scope)
        finally:
            if store is not None:
                store.close()
            lock.release()

    def _run_one(self, source_key: str, mode: str) -> None:
        from .core.run_lock import LockError, RunLock
        from .core.scope import load_scope
        from .providers.sqlite import SqliteStore

        lock_path = self.lock_path if mode == "production" else self.experimental_lock_path
        db_path = self.database if mode == "production" else self.experimental_db
        try:
            lock = RunLock.acquire(lock_path)
        except LockError:
            self._set(source_key, mode, state="already_running",
                      message="Another local collection is already running.",
                      finished_at=datetime.now(timezone.utc).isoformat())
            return
        store = None
        try:
            store = SqliteStore(str(db_path))
            scope = load_scope(self.scope_path) if mode == "production" else None
            self._run_single(source_key, mode, store, scope)
        finally:
            if store is not None:
                store.close()
            lock.release()

    def _run_single(self, source_key: str, mode: str, store, scope) -> None:
        from .core.registry import collectors
        from .core.runner import ScopeError, run_experimental, run_production_collector
        from .core.qualification import QualificationProvenance

        self._set(
            source_key, mode, state="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            message=f"Collecting from {source_key}…",
        )
        try:
            collector_cls = collectors.get(source_key)
            collector = (
                collector_cls(overrides_path=self.overrides_path)
                if source_key == "hmd-nokia" else collector_cls()
            )
            kwargs = dict(
                manufacturer=getattr(collector, "manufacturer", "unknown"),
                source_type=getattr(collector, "source_type", "catalogue"),
                region=getattr(collector, "region", None),
                base_url=getattr(collector, "base_url", ""),
            )
            if mode == "production":
                result, stats = run_production_collector(
                    collector, store, scope, provenance=QualificationProvenance.MANUAL, **kwargs)
            else:
                result, stats = run_experimental(
                    collector, store, provenance=QualificationProvenance.TEST, **kwargs)
            state = (
                "success" if result.status != "failed" and stats.get("status") != "blocked_zero_result"
                else ("blocked" if stats.get("status") == "blocked_zero_result" else "failed")
            )
            self._set(
                source_key, mode, state=state,
                result={"collector_status": result.status, "discoveries": result.discovered, **stats},
                message=(
                    "Collection completed; classifier views refreshed." if state == "success"
                    else str(stats.get("reason") or result.errors)
                ),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except ScopeError as exc:
            self._set(source_key, mode, state="failed", message=str(exc),
                      finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            self._set(source_key, mode, state="failed", message=f"{type(exc).__name__}: {exc}",
                      finished_at=datetime.now(timezone.utc).isoformat())
