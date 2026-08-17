from __future__ import annotations

import threading
from datetime import datetime, timezone


class LocalCollectionController:
    source_key = "hmd-nokia"

    def __init__(self, database, lock_path, scope_path, overrides_path):
        self.database, self.lock_path = database, lock_path
        self.scope_path, self.overrides_path = scope_path, overrides_path
        self._guard = threading.Lock()
        self._state = {"state":"idle","source":self.source_key,"started_at":None,"finished_at":None,
                       "message":"Ready to collect HMD/Nokia.","result":None}

    def snapshot(self):
        with self._guard: return dict(self._state)

    def start(self):
        with self._guard:
            if self._state["state"] in {"queued","running"}:
                return False, {"error":"collection_already_running", **self._state}
            self._state = {"state":"queued","source":self.source_key,"started_at":None,"finished_at":None,
                           "message":"Collection queued.","result":None}
        threading.Thread(target=self._run, daemon=True).start()
        return True, self.snapshot()

    def _run(self):
        from .collectors.hmd import HmdCollector
        from .core.run_lock import LockError, RunLock
        from .core.runner import run_production_collector
        from .core.scope import load_scope
        from .providers.sqlite import SqliteStore

        with self._guard: self._state.update(state="running", started_at=datetime.now(timezone.utc).isoformat(), message="Collecting from HMD/Nokia public sources…")
        lock = store = None
        try:
            lock = RunLock.acquire(self.lock_path)
            store = SqliteStore(str(self.database))
            collector = HmdCollector(overrides_path=self.overrides_path)
            result, stats = run_production_collector(
                collector, store, load_scope(self.scope_path), manufacturer=collector.manufacturer,
                source_type=collector.source_type, region=collector.region, base_url=collector.base_url,
            )
            state = "success" if result.status != "failed" and stats.get("status") != "blocked_zero_result" else ("blocked" if stats.get("status") == "blocked_zero_result" else "failed")
            with self._guard: self._state.update(state=state, result={"collector_status":result.status,"discoveries":result.discovered,**stats}, message="Collection completed; classifier views refreshed." if state=="success" else str(stats.get("reason") or result.errors), finished_at=datetime.now(timezone.utc).isoformat())
        except LockError:
            with self._guard: self._state.update(state="already_running",message="Another local collection is already running.",finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            with self._guard: self._state.update(state="failed",message=f"{type(exc).__name__}: {exc}",finished_at=datetime.now(timezone.utc).isoformat())
        finally:
            if store is not None: store.close()
            if lock is not None: lock.release()
