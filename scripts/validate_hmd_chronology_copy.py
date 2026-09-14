"""Offline acceptance drill: SQLite-safe COPY of recon snapshot, never delivery.

Input must be the recon's copied v6 Windows snapshot and captured HMD
result.json. The new output DB is disposable. No collectors or network
clients are invoked; the fake outbox records newly derived events only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.pipeline import process_run
from feature_phone_clank.providers.discord import initial_status
from feature_phone_clank.providers.sqlite import SqliteStore
from feature_phone_clank.providers.sqlite.compatibility import inspect_compatibility


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--captured", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.report.exists():
        parser.error("output DB and report must be new paths")
    if args.output.resolve() == args.snapshot.resolve():
        parser.error("the source snapshot is immutable")
    original_hash = digest(args.snapshot)
    source = sqlite3.connect(f"file:{args.snapshot.resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    if source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 6:
        parser.error("this acceptance specimen must be v6")
    before_history = {t: [tuple(r) for r in source.execute(f"SELECT * FROM {t} ORDER BY id")]
                      for t in ("observations", "events", "notifications")}
    dest = sqlite3.connect(args.output)
    source.backup(dest)
    assert dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    dest.close()
    source.close()
    discoveries = [Discovery.model_validate(d) for d in json.loads(
        args.captured.read_text(encoding="utf-8"))["discoveries"]]
    assert len(discoveries) == 44 and all(d.source_key == "hmd-nokia" for d in discoveries)
    store = SqliteStore(str(args.output))
    try:
        migrated_history = {t: [tuple(r)[:len(before_history[t][0])] for r in
                                store.db.execute(f"SELECT * FROM {t} ORDER BY id")]
                            for t in before_history}
        assert migrated_history == before_history
        assert store.db.execute("SELECT COUNT(*) FROM observation_occurrences").fetchone()[0] == 0
        sid = store.db.execute("SELECT id FROM sources WHERE source_key='hmd-nokia'").fetchone()[0]
        before = {d.product_key: dict(store.latest_observation(store.get_product(d.product_key)["id"]))
                  for d in discoveries}
        mismatches = sum(before[d.product_key]["content_hash"] != d.content_hash() for d in discoveries)
        def enqueue(event, event_id):
            store.notification_put("acceptance-only", event.dedup_key(), {"offline": True},
                                   event_id, initial_status(event))
        run = store.run_started("hmd-nokia", provenance="MANUAL")
        stats = process_run(store, "hmd-nokia", sid, discoveries, [], False, run_id=run, notify=enqueue)
        events = [dict(r) for r in store.db.execute("SELECT * FROM events WHERE id>? ORDER BY id",
                                                   (max(r[0] for r in before_history["events"]),))]
        assert len(events) == 36 and all(e["event_type"] == "specs_became_available" for e in events)
        assert stats["new_products"] == 0 and stats["events_created"] == 36
        assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == len(before_history["observations"])
        for d in discoveries:
            assert store.latest_observation(store.get_product(d.product_key)["id"])["content_hash"] == d.content_hash()
        def sizes():
            return {t: store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("observations", "observation_occurrences", "events", "notifications")}
        first_sizes = sizes()
        repeat = process_run(store, "hmd-nokia", sid, discoveries, [], False,
                             run_id=store.run_started("hmd-nokia", provenance="MANUAL"), notify=enqueue)
        assert repeat["events_created"] == 0 and repeat["unchanged_observations"] == 44
        repeat_sizes = sizes()
        process_run(store, "hmd-nokia", sid, discoveries, [], False, run_id=run, notify=enqueue)
        assert sizes() == repeat_sizes
        examples = []
        for slug in ("hmd-105-4g", "hmd-101", "nokia-215-4g-2nd-edition"):
            key = f"hmd-nokia:{slug}"
            product = store.get_product(key)
            now = dict(store.latest_observation(product["id"]))
            event = next(e for e in events if e["product_id"] == product["id"])
            examples.append({"product_key": key, "model": product["model"],
                             "before_observation_id": before[key]["id"],
                             "before_completeness": before[key]["spec_completeness"],
                             "before_chronology_basis": before[key]["chronology_basis"],
                             "current_observation_id": now["id"],
                             "current_completeness": now["spec_completeness"],
                             "occurrence_id": now["occurrence_id"], "event": event})
        assert digest(args.snapshot) == original_hash
        evidence = {"snapshot_sha256_before_and_after": original_hash,
                    "captured_sha256": digest(args.captured), "schema_version": store.schema_version(),
                    "old_binary_verdict": inspect_compatibility(store.db, expected_version=6).state.value,
                    "backup_integrity_check": "ok", "migration_history_preserved": True,
                    "invented_historical_occurrences": 0,
                    "hmd_hash_mismatches_before": mismatches, "hmd_hash_mismatches_after": 0,
                    "first_stats": stats, "repeat_stats": repeat, "exact_run_replay_no_mutation": True,
                    "first_sizes": first_sizes, "repeat_sizes": repeat_sizes,
                    "new_outbox_statuses": {r[0]: r[1] for r in store.db.execute(
                        "SELECT status,COUNT(*) FROM notifications WHERE provider='acceptance-only' GROUP BY status")},
                    "examples": examples, "integrity_check": store.db.execute("PRAGMA integrity_check").fetchone()[0],
                    "network_calls": 0, "notifications_sent": 0}
        args.report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in evidence.items() if k != "examples"}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
