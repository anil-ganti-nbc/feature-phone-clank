"""feature-phone-clank CLI: version | identity | health | run |
run-experimental | status | events | report | deliver | notifications |
test-notify | backup | continuity.

All machine-consumable output is JSON printed to stdout — no command's
contract depends on parsing human-readable text (user constraint 9).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("feature_phone_clank.cli")

DEFAULT_DB_PATH = "data/feature_phone_clank.db"
DEFAULT_SCOPE_PATH = "config/scope.yaml"

# Never hard-coded, never checked into git/fixtures/docs, never logged
# (brief section 7). Empty/unset means "no delivery configured" — enqueueing
# still works, `deliver`/`run` just report 0 sent and leave rows pending.
DISCORD_WEBHOOK_ENV = "FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL"


def _resolve_webhook_url() -> str | None:
    return os.environ.get(DISCORD_WEBHOOK_ENV) or None


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
    from .core.qualification import QualificationProvenance
    from .core.scope import load_scope
    from .providers.discord import DiscordNotifier
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
    notifier = DiscordNotifier(store, _resolve_webhook_url())
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
                notifier=notifier,
                provenance=QualificationProvenance.SCHEDULED,
            )
            results.append({"source_key": source_key, **stats})
        # Delivery is attempted after collection, never in place of it: a
        # dead webhook cannot block or fail a collector run (brief section
        # 4/17) — this call cannot raise (DiscordNotifier.drain never does)
        # and its result is purely informational.
        delivery = notifier.drain() if not args.no_deliver else {"skipped": True}
    finally:
        store.close()
        if lock is not None:
            lock.release()

    print(json.dumps({"status": "ok", "results": results, "delivery": delivery}, indent=2, default=str))
    return 0


def cmd_run_experimental(args: argparse.Namespace) -> int:
    """Run one or more registered-but-not-production-scoped collectors
    (Stage E: itel, Lava, ...) against a caller-specified experimental
    database — never the production `--db` path, and never subject to
    `config/scope.yaml`. This is the CLI entry point `run_experimental()`
    (core/runner.py) needed but never had one wired up for: that function
    already refuses to touch scope, so the isolation guarantee here is
    entirely "which store did we open" (`--experimental-db`), not a second
    scope check. Deliberately its own lock file — an experimental run must
    never be blocked by, or block, a concurrent production `run`."""
    from . import collectors as _collectors  # noqa: F401 — import for registration side effect
    from .core.registry import collectors
    from .core.run_lock import LockError, RunLock
    from .core.runner import run_experimental
    from .core.qualification import QualificationProvenance
    from .core.scope import load_scope
    from .providers.sqlite import SqliteStore

    scope = load_scope(args.scope)
    all_experimental = [name for name in collectors.names() if name not in scope.production_collectors]
    requested = args.sources.split(",") if args.sources else all_experimental

    lock = None
    if not args.no_lock:
        try:
            lock = RunLock.acquire(args.lock_path)
        except LockError as e:
            print(json.dumps({"status": "locked", "message": str(e)}, indent=2))
            return 2

    store = SqliteStore(args.experimental_db)
    results = []
    try:
        for source_key in requested:
            if source_key not in collectors:
                results.append({"source_key": source_key, "status": "unregistered"})
                continue
            if source_key in scope.production_collectors:
                # a collector promoted to production scope stops being
                # runnable through this experimental path with its old
                # experimental history left dangling by accident
                results.append({"source_key": source_key, "status": "now_production_scoped"})
                continue
            collector = collectors.get(source_key)()
            result, stats = run_experimental(
                collector, store,
                manufacturer=getattr(collector, "manufacturer", "unknown"),
                source_type=getattr(collector, "source_type", "catalogue"),
                region=getattr(collector, "region", None),
                base_url=getattr(collector, "base_url", ""),
                provenance=QualificationProvenance.TEST,
            )
            results.append({"source_key": source_key, **stats})
    finally:
        store.close()
        if lock is not None:
            lock.release()

    print(json.dumps({
        "status": "ok", "experimental_db": args.experimental_db, "results": results,
    }, indent=2, default=str))
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
    """Operator report (Stage 3.1 soak reporting + Stage 4 delivery
    visibility, brief section 11): everything an owner needs — source
    health, run/event/notification state, runtime provenance — in one
    deterministic JSON document. Deliberately a CLI command, not a
    dashboard; extends the existing soak report rather than duplicating
    it."""
    from datetime import datetime, timedelta, timezone

    from . import runtime_bridge
    from .paths import resolve_data_path
    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database"}, indent=2))
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    db_path = resolve_data_path(args.db)
    health = runtime_bridge.as_jsonable(runtime_bridge.get_health(db_path))
    version_info = runtime_bridge.get_version_info()
    store = SqliteStore(args.db)
    try:
        report = store.soak_report(args.source, since)
    finally:
        store.close()

    counts = report["notification_counts"]
    pending = counts.get("pending", 0)
    failed = counts.get("failed", 0)
    # Delivery health is a distinct axis from source/collector health (brief
    # section 17): a webhook outage never makes `source_health` anything
    # other than what the collector itself reported.
    if failed:
        delivery_health = "degraded"
    elif pending:
        delivery_health = "pending"
    else:
        delivery_health = "healthy"

    print(json.dumps({
        "status": "ok",
        "source_health": health,
        "runtime_revision": version_info["source_revision_short"],
        "schema_version": report["schema_version"],
        "delivery_health": delivery_health,
        **report,
    }, indent=2, default=str))
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    """Attempt delivery of every pending notification, independent of a
    collector run. Recovery/testing path (brief section 10) — the normal
    `run` command already does this at the end of every collection, but a
    Discord outage or a config fix can be retried standalone without
    re-running collectors."""
    from .providers.discord import DiscordNotifier
    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database"}, indent=2))
        return 0
    store = SqliteStore(args.db)
    try:
        if args.requeue_failed:
            n = store.requeue_failed_notifications("discord")
            print(json.dumps({"status": "ok", "requeued": n}, indent=2))
        notifier = DiscordNotifier(store, _resolve_webhook_url())
        result = notifier.drain()
    finally:
        store.close()
    print(json.dumps({"status": "ok", **result}, indent=2, default=str))
    return 0


def cmd_notifications(args: argparse.Namespace) -> int:
    """Inspect the notification outbox without touching SQLite directly
    (brief section 10/12): counts by status, plus recent rows for a given
    status."""
    from .providers.sqlite import SqliteStore

    if not Path(args.db).exists():
        print(json.dumps({"status": "no_database", "counts": {}}, indent=2))
        return 0
    store = SqliteStore(args.db)
    try:
        counts = store.notification_counts("discord")
        rows = []
        if args.status:
            rows = [
                {**dict(r), "payload_json": None}  # payload is provider-internal, not operator-facing
                for r in store.notifications_by_status(args.status, "discord", args.limit)
            ]
    finally:
        store.close()
    print(json.dumps({"status": "ok", "counts": counts, "notifications": rows}, indent=2, default=str))
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    """Owner field-test support (brief section 12/13): send an unmistakably
    marked FEATURE-01 TEST notification through the real delivery path,
    without fabricating a product/event and without touching production
    collector state. Requires an explicit --confirm-production flag if the
    resolved webhook isn't overridden with --webhook, so a real production
    channel is never contacted by accident."""
    from .providers.discord import DiscordNotifier
    from .providers.sqlite import SqliteStore

    webhook = args.webhook or _resolve_webhook_url()
    if webhook and not args.webhook and not args.confirm_production:
        print(json.dumps({
            "status": "refused",
            "message": f"a webhook is configured via {DISCORD_WEBHOOK_ENV}; sending a test "
                       "notification to it requires --confirm-production (explicit owner "
                       "approval), or pass --webhook to target a different/test endpoint.",
        }, indent=2))
        return 1

    store = SqliteStore(args.db)
    try:
        notifier = DiscordNotifier(store, webhook)
        result = notifier.enqueue_test(note=args.note or "")
    finally:
        store.close()
    print(json.dumps({"status": "ok", **result}, indent=2, default=str))
    return 0 if result["sent"] else 1


def cmd_backup(args: argparse.Namespace) -> int:
    """Create a verified SQLite-safe recovery point (DATA_SURVIVABILITY
    Layer A). Takes the same single-instance lock as `run` so the snapshot
    can never race a writer; verifies integrity before reporting success;
    refuses to overwrite an existing recovery point without --force. The
    continuity registry beside the database is snapshotted alongside the
    .db file so epoch evidence travels with every backup."""
    import hashlib

    from .core.run_lock import LockError, RunLock
    from .paths import resolve_data_path
    from .providers.sqlite import SqliteStore

    db_path = resolve_data_path(args.db)
    if not Path(db_path).exists():
        print(json.dumps({"status": "no_database", "database": str(db_path)}, indent=2))
        return 1

    lock = None
    if not args.no_lock:
        try:
            lock = RunLock.acquire(args.lock_path)
        except LockError as e:
            print(json.dumps({"status": "locked", "message": str(e)}, indent=2))
            return 2

    store = None
    try:
        store = SqliteStore(str(db_path))
        result = store.backup_to(args.output, overwrite=args.force)
        continuity_src = Path(db_path).parent / "continuity" / "continuity-events.jsonl"
        if continuity_src.exists():
            continuity_dst = Path(args.output).with_name(
                Path(args.output).name + ".continuity.jsonl"
            )
            continuity_dst.write_bytes(continuity_src.read_bytes())
            raw = continuity_dst.read_bytes()
            result["continuity_snapshot"] = {
                "path": str(continuity_dst),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
    finally:
        if store is not None:
            store.close()
        if lock is not None:
            lock.release()

    print(json.dumps({"status": "ok", **result}, indent=2, default=str))
    return 0


def cmd_continuity(args: argparse.Namespace) -> int:
    """Inspect (and, with --ensure-seed, materialise) the append-only
    ADR-0006 continuity registry for this runtime. Read-only by default;
    hash mismatches are reported, never silently repaired."""
    from .core import continuity
    from .paths import resolve_data_path

    db_path = resolve_data_path(args.db)
    if args.ensure_seed:
        path = continuity.ensure_registry(db_path)
        events = continuity.read_events(db_path)
    elif Path(db_path).exists() or continuity.registry_path(db_path).exists():
        path = continuity.registry_path(db_path)
        events = continuity.read_events(db_path)
    else:
        print(json.dumps({
            "status": "no_registry",
            "message": "no continuity registry yet (run --ensure-seed to create it "
                       "with the operator-verified epoch-2 seed records)",
            "registry_path": str(continuity.registry_path(db_path)),
        }, indent=2))
        return 1
    bad = continuity.verify_hashes(events)
    epochs = sorted({e.get("new_epoch_id") for e in events if e.get("new_epoch_id")})
    print(json.dumps({
        "status": "ok",
        "registry_path": str(path),
        "epoch_id": continuity.EPOCH_ID,
        "epochs": epochs,
        "event_count": len(events),
        "hash_mismatches": bad,
        "events": events,
    }, indent=2, default=str))
    return 1 if bad else 0


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
    p_run.add_argument("--no-deliver", action="store_true",
                       help="skip attempting notification delivery after collection "
                            "(pending notifications remain queued for `deliver`)")
    p_run.set_defaults(func=cmd_run)

    p_run_exp = sub.add_parser(
        "run-experimental",
        help="run collectors NOT in config/scope.yaml (itel, Lava, ...) against an "
             "isolated experimental database — never production",
    )
    p_run_exp.add_argument(
        "--experimental-db", default="data/feature_phone_clank_experimental.db",
        help="SQLite path for experimental-source state (separate from --db)",
    )
    p_run_exp.add_argument(
        "--sources", help="comma-separated source_keys to run (default: every "
                          "registered collector not in production scope)",
    )
    p_run_exp.add_argument("--no-lock", action="store_true")
    p_run_exp.add_argument("--lock-path", default="data/feature-phone-clank-experimental.lock")
    p_run_exp.set_defaults(func=cmd_run_experimental)

    p_status = sub.add_parser("status", help="recent collector run telemetry")
    p_status.add_argument("--limit", type=int, default=20)
    p_status.set_defaults(func=cmd_status)

    p_events = sub.add_parser("events", help="recent deterministic change events")
    p_events.add_argument("--source", help="filter to one source_key")
    p_events.add_argument("--limit", type=int, default=50)
    p_events.add_argument("--min-alert-level", choices=["noise", "low", "medium", "high"],
                          help="only events at or above this severity")
    p_events.set_defaults(func=cmd_events)

    p_report = sub.add_parser("report", help="operator report: health, events, notifications, provenance")
    p_report.add_argument("--source", default="hmd-nokia", help="source_key to report on")
    p_report.add_argument("--days", type=int, default=7, help="window size in days")
    p_report.set_defaults(func=cmd_report)

    p_deliver = sub.add_parser("deliver", help="attempt delivery of pending notifications")
    p_deliver.add_argument("--requeue-failed", action="store_true",
                           help="move terminally-failed notifications back to pending first")
    p_deliver.set_defaults(func=cmd_deliver)

    p_notif = sub.add_parser("notifications", help="inspect the notification outbox")
    p_notif.add_argument("--status", choices=["pending", "sent", "failed", "suppressed"],
                         help="list recent notifications in this status (omit for counts only)")
    p_notif.add_argument("--limit", type=int, default=20)
    p_notif.set_defaults(func=cmd_notifications)

    p_test = sub.add_parser("test-notify", help="send a marked FEATURE-01 TEST notification")
    p_test.add_argument("--webhook", help="override webhook URL (bypasses --confirm-production)")
    p_test.add_argument("--confirm-production", action="store_true",
                        help="explicit approval to send to the configured production webhook")
    p_test.add_argument("--note", help="extra text appended to the test message")
    p_test.set_defaults(func=cmd_test_notify)

    p_backup = sub.add_parser("backup", help="create a verified SQLite-safe recovery point")
    p_backup.add_argument("output", help="backup file path (refused if it exists, unless --force)")
    p_backup.add_argument("--force", action="store_true",
                          help="overwrite an existing backup file (never done by default)")
    p_backup.add_argument("--no-lock", action="store_true",
                          help="skip the single-instance lock (not recommended)")
    p_backup.add_argument("--lock-path", default="data/feature-phone-clank.lock")
    p_backup.set_defaults(func=cmd_backup)

    p_cont = sub.add_parser("continuity", help="inspect the ADR-0006 continuity registry")
    p_cont.add_argument("--ensure-seed", action="store_true",
                        help="create the registry with operator-verified epoch seed records "
                             "if absent (append-only; never edits existing records)")
    p_cont.set_defaults(func=cmd_continuity)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
