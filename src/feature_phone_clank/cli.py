"""feature-phone-clank CLI: version | identity | health | run | status.

All machine-consumable output is JSON printed to stdout — no command's
contract depends on parsing human-readable text (user constraint 9).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("feature_phone_clank.cli")

DEFAULT_DB_PATH = "data/feature_phone_clank.db"
DEFAULT_SCOPE_PATH = "config/scope.yaml"


def cmd_version(args: argparse.Namespace) -> int:
    from . import runtime_bridge

    print(json.dumps(runtime_bridge.get_version_info(), indent=2, default=str))
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    from . import runtime_bridge

    identity = runtime_bridge.as_jsonable(runtime_bridge.get_identity())
    print(json.dumps(identity, indent=2, default=str))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from . import runtime_bridge
    from .paths import resolve_data_path

    db_path = resolve_data_path(args.db)
    payload = runtime_bridge.as_jsonable(runtime_bridge.get_health(db_path))
    print(json.dumps(payload, indent=2, default=str))
    return 1 if payload.get("operational_state") == "failed" else 0


def cmd_run(args: argparse.Namespace) -> int:
    from . import collectors as _collectors  # noqa: F401 — import for registration side effect
    from .core.registry import collectors
    from .core.run_lock import LockError, RunLock
    from .core.runner import run_production_collector
    from .core.scope import load_scope
    from .providers.sqlite import SqliteStore

    scope = load_scope(args.scope)
    if not scope.production_collectors:
        print(json.dumps({
            "status": "no_production_collectors",
            "message": "config/scope.yaml has no approved collectors yet "
                       "(expected until a collector completes Stage 2 validation)",
        }, indent=2))
        return 0

    lock = None
    if not args.no_lock:
        try:
            lock = RunLock.acquire(args.lock_path)
        except LockError as e:
            print(json.dumps({"status": "locked", "message": str(e)}, indent=2))
            return 2

    store = SqliteStore(args.db)
    results = []
    try:
        for source_key in scope.production_collectors:
            if source_key not in collectors:
                results.append({"source_key": source_key, "status": "unregistered"})
                continue
            collector = collectors.get(source_key)()
            result, stats = run_production_collector(
                collector, store, scope,
                manufacturer=getattr(collector, "manufacturer", "unknown"),
                source_type=getattr(collector, "source_type", "catalogue"),
                region=getattr(collector, "region", None),
                base_url=getattr(collector, "base_url", ""),
            )
            results.append({"source_key": source_key, **stats})
    finally:
        store.close()
        if lock is not None:
            lock.release()

    print(json.dumps({"status": "ok", "results": results}, indent=2, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database", "runs": []}, indent=2))
        return 0
    store = SqliteStore(args.db)
    try:
        rows = [dict(r) for r in store.recent_runs(args.limit)]
    finally:
        store.close()
    print(json.dumps({"status": "ok", "runs": rows}, indent=2, default=str))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """Machine-readable recent-event listing (brief section 18 / Unified
    Clank compatibility) — structured JSON, no network API, no delivery
    concept at all."""
    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database", "events": []}, indent=2))
        return 0
    store = SqliteStore(args.db)
    try:
        rows = store.recent_events(
            source_key=args.source, limit=args.limit, min_alert_level=args.min_alert_level,
        )
        events = []
        for r in rows:
            d = dict(r)
            d["changed_fields"] = json.loads(d.pop("changed_fields_json") or "[]")
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
            events.append(d)
    finally:
        store.close()
    print(json.dumps({"status": "ok", "events": events}, indent=2, default=str))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Unattended-soak reporting (Stage 3.1): everything section 9 of the
    brief asks for, aggregated from already-persisted state. Deliberately
    a CLI command, not a dashboard."""
    from datetime import datetime, timedelta, timezone

    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database"}, indent=2))
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    store = SqliteStore(args.db)
    try:
        report = store.soak_report(args.source, since)
    finally:
        store.close()
    print(json.dumps({"status": "ok", **report}, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feature-phone-clank", description="Feature-phone product intelligence (FEATURE-01)"
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--scope", default=DEFAULT_SCOPE_PATH, help="production scope YAML path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print runtime version info as JSON").set_defaults(func=cmd_version)
    sub.add_parser("identity", help="print Unified Clank runtime identity as JSON").set_defaults(func=cmd_identity)
    sub.add_parser("health", help="print runtime health as JSON").set_defaults(func=cmd_health)

    p_run = sub.add_parser("run", help="run all production-scope collectors")
    p_run.add_argument("--no-lock", action="store_true",
                       help="skip single-instance lock (not recommended)")
    p_run.add_argument("--lock-path", default="data/feature-phone-clank.lock")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="recent collector run telemetry")
    p_status.add_argument("--limit", type=int, default=20)
    p_status.set_defaults(func=cmd_status)

    p_events = sub.add_parser("events", help="recent deterministic change events")
    p_events.add_argument("--source", help="filter to one source_key")
    p_events.add_argument("--limit", type=int, default=50)
    p_events.add_argument("--min-alert-level", choices=["noise", "low", "medium", "high"],
                          help="only events at or above this severity")
    p_events.set_defaults(func=cmd_events)

    p_report = sub.add_parser("report", help="unattended-soak report (Stage 3.1)")
    p_report.add_argument("--source", default="hmd-nokia", help="source_key to report on")
    p_report.add_argument("--days", type=int, default=7, help="window size in days")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
