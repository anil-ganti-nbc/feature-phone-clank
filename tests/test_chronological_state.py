"""Chronological content reuse, committed-run replay and crash recovery."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.pipeline import process_run
from feature_phone_clank.providers.sqlite import SqliteStore
from feature_phone_clank.providers.sqlite.compatibility import (
    StateCompatibility, inspect_compatibility,
)
from helpers import make_discovery

FIXTURES = Path(__file__).parent / "fixtures" / "chronology"


def setup_source(store, key="test-scripted"):
    return store.ensure_source(key, "TestCo", "catalogue", "en_int", "https://example.test", {})


def persist(store, sid, discoveries, *, baseline=False, run_id=None, notify=None):
    run_id = run_id or store.run_started("test-scripted")
    stats = process_run(store, "test-scripted", sid, discoveries, [], baseline,
                        notify=notify, run_id=run_id)
    return run_id, stats


def content(value, completeness="complete"):
    return make_discovery("p1", fields={"usb-connection": {"values": [value]}},
                          spec_completeness=completeness)


def counts(store):
    return {t: store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("observations", "observation_occurrences", "events", "notifications")}


def enqueue(store):
    def callback(event, event_id):
        store.notification_put("test", event.dedup_key(), {"test": True}, event_id, "pending")
    return callback


def test_repeated_and_recurring_field_transitions_preserve_content_dedup(store):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("usb-type-c")
    sequence = [a, a, b, a, a, b]
    expected_events = [0, 0, 1, 1, 0, 1]
    sighting_ids = []
    for i, (d, expected) in enumerate(zip(sequence, expected_events)):
        _, stats = persist(store, sid, [d], baseline=i == 0, notify=enqueue(store))
        assert stats["events_created"] == expected
        latest = store.latest_observation(store.get_product(d.product_key)["id"])
        assert latest["content_hash"] == d.content_hash()
        assert latest["chronology_basis"] == "SIGHTING"
        assert latest["last_sighted_at"] == d.observed_at.isoformat()
        sighting_ids.append(latest["occurrence_id"])
    assert len(set(sighting_ids)) == 6
    assert counts(store) == dict(observations=2, observation_occurrences=6, events=3, notifications=3)
    events = store.db.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [e["event_type"] for e in events] == ["field_changed"] * 3
    assert events[0]["previous_observation_id"] == events[2]["previous_observation_id"]
    assert events[0]["current_observation_id"] == events[2]["current_observation_id"]
    assert events[0]["dedup_key"] != events[2]["dedup_key"]
    for event in events:
        assert event["transition_occurrence_id"] in sighting_ids
        changes = json.loads(event["changed_fields_json"])
        assert changes[0]["old_value"] != changes[0]["new_value"]


def test_complete_incomplete_historical_complete_recurs(store):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("micro-usb", "incomplete")
    for i, d in enumerate([a, b, a, a, b, a]):
        persist(store, sid, [d], baseline=i == 0)
    events = store.db.execute("SELECT event_type FROM events ORDER BY id").fetchall()
    assert [e[0] for e in events] == ["specs_became_unavailable", "specs_became_available"] * 2
    assert counts(store)["observations"] == 2
    assert store.incomplete_spec_products("test-scripted") == []


def test_exact_committed_run_replay_cannot_rewind_later_state(store):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("usb-type-c")
    persist(store, sid, [a], baseline=True)
    run_b, receipt = persist(store, sid, [b], notify=enqueue(store))
    persist(store, sid, [a], notify=enqueue(store))
    before = counts(store)
    _, replay = persist(store, sid, [b], run_id=run_b, notify=enqueue(store))
    assert replay == receipt
    assert counts(store) == before
    assert store.latest_observation(store.get_product(a.product_key)["id"])["content_hash"] == a.content_hash()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_enqueue_rolls_back_state_sighting_event_and_outbox(store, failure):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("usb-type-c")
    persist(store, sid, [a], baseline=True)
    before = counts(store)
    run_id = store.run_started("test-scripted")
    def fail(event, event_id):
        enqueue(store)(event, event_id)
        raise failure("interrupted after enqueue")
    with pytest.raises(failure):
        persist(store, sid, [b], run_id=run_id, notify=fail)
    assert counts(store) == before
    assert store.processed_run_stats(run_id, "test-scripted") is None
    assert store.latest_observation(store.get_product(a.product_key)["id"])["content_hash"] == a.content_hash()
    _, stats = persist(store, sid, [b], run_id=run_id, notify=enqueue(store))
    assert stats["events_created"] == 1
    after = counts(store)
    persist(store, sid, [b], run_id=run_id, notify=enqueue(store))
    assert counts(store) == after


def test_process_death_before_commit_retries_whole_transition(store):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("usb-type-c")
    persist(store, sid, [a], baseline=True)
    run_id = store.run_started("test-scripted")
    before = counts(store)
    script = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
from feature_phone_clank.providers.sqlite import SqliteStore
from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.pipeline import process_run
s = SqliteStore(sys.argv[2])
d = Discovery.model_validate_json(sys.argv[5])
def die(event, event_id):
    s.notification_put('test', event.dedup_key(), {'test': True}, event_id, 'pending')
    os._exit(73)
process_run(s, d.source_key, int(sys.argv[3]), [d], [], False, notify=die, run_id=int(sys.argv[4]))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script,
                             str(Path(__file__).parents[1] / "src"), store.db_path,
                             str(sid), str(run_id), b.model_dump_json()], timeout=20)
    assert result.returncode == 73
    assert counts(store) == before
    _, stats = persist(store, sid, [b], run_id=run_id, notify=enqueue(store))
    assert stats["events_created"] == 1
    assert counts(store)["notifications"] == 1


def test_commit_failure_does_not_leave_partial_transition_visible(store):
    sid = setup_source(store)
    a, b = content("micro-usb"), content("usb-type-c")
    persist(store, sid, [a], baseline=True)
    run_id = store.run_started("test-scripted")
    before = counts(store)
    connection = store.db
    class FailCommitOnce:
        def __getattr__(self, name):
            return getattr(connection, name)
        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")
    store.db = FailCommitOnce()
    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        persist(store, sid, [b], run_id=run_id, notify=enqueue(store))
    store.db = connection
    assert not connection.in_transaction and counts(store) == before
    assert store.processed_run_stats(run_id, "test-scripted") is None
    _, stats = persist(store, sid, [b], run_id=run_id, notify=enqueue(store))
    assert stats["events_created"] == 1


def test_removal_reappearance_and_replayed_absence_preserve_semantics(store):
    sid = setup_source(store)
    a = content("micro-usb")
    persist(store, sid, [a], baseline=True)
    absence_run, _ = persist(store, sid, [])
    persist(store, sid, [], run_id=absence_run)
    assert store.get_product(a.product_key)["consecutive_absences"] == 1
    persist(store, sid, [])
    persist(store, sid, [])
    assert store.get_product(a.product_key)["status"] == "removed"
    _, stats = persist(store, sid, [a])
    assert stats["events_created"] == 0
    assert stats["new_products"] == 0
    assert store.get_product(a.product_key)["status"] == "active"
    for _ in range(3):
        persist(store, sid, [])
    events = store.db.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [e["event_type"] for e in events] == ["product_removed", "product_removed"]
    assert events[0]["dedup_key"] != events[1]["dedup_key"]


def legacy_database(path):
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / "schema-v6.sql").read_text(encoding="utf-8"))
    con.executemany("INSERT INTO schema_migrations(version) VALUES (?)", [(i,) for i in range(1, 7)])
    return con


def test_v6_migration_and_three_real_hmd_recoveries(tmp_path):
    path = tmp_path / "legacy.db"
    cases = json.loads((FIXTURES / "hmd-recovery.json").read_text(encoding="utf-8"))
    con = legacy_database(path)
    con.execute("INSERT INTO sources(id,source_key,manufacturer,source_type,base_url) VALUES (1,'hmd-nokia','HMD','catalogue','https://www.hmd.com')")
    for case in cases:
        p = {**case["product"], "source_id": 1}
        con.execute(f"INSERT INTO products({','.join(p)}) VALUES ({','.join('?' for _ in p)})", list(p.values()))
        for obs in case["observations"]:
            con.execute(f"INSERT INTO observations({','.join(obs)}) VALUES ({','.join('?' for _ in obs)})", list(obs.values()))
    con.execute("INSERT INTO events(id,product_id,collector,event_type,alert_level,confidence,dedup_key) VALUES (1,?,?, 'specs_became_unavailable','low','medium','legacy-event')", (cases[0]["product"]["id"], "hmd-nokia"))
    con.execute("INSERT INTO notifications(event_id,provider,dedup_key,payload_json,status,attempts) VALUES (1,'test','legacy-notification','{}','sent',2)")
    before = {t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in ("observations", "events", "notifications")}
    con.commit()
    assert inspect_compatibility(con).state is StateCompatibility.MIGRATION_REQUIRED
    con.close()
    store = SqliteStore(str(path))
    try:
        assert store.schema_version() == 7
        assert inspect_compatibility(store.db, expected_version=6).state is StateCompatibility.INCOMPATIBLE_NEWER
        for table, rows in before.items():
            after = store.db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            assert [tuple(r)[:len(rows[0])] for r in after] == rows
        assert counts(store)["observation_occurrences"] == 0
        for case in cases:
            latest = store.latest_observation(case["product"]["id"])
            assert latest["id"] == max(o["id"] for o in case["observations"])
            assert latest["spec_completeness"] == "incomplete"
            assert latest["chronology_basis"] == "LEGACY_LAST_UNIQUE_CONTENT"
            assert latest["occurrence_id"] is None and latest["last_sighted_at"] is None
        discoveries = [Discovery.model_validate(c["current"]) for c in cases]
        run_id = store.run_started("hmd-nokia")
        stats = process_run(store, "hmd-nokia", 1, discoveries, [], False, run_id=run_id, notify=enqueue(store))
        assert stats["new_products"] == 0 and stats["events_created"] == 3
        assert counts(store)["observations"] == len(before["observations"])
        for d, case in zip(discoveries, cases):
            latest = store.latest_observation(case["product"]["id"])
            assert latest["content_hash"] == d.content_hash()
            assert latest["spec_completeness"] == "complete"
        assert [r[0] for r in store.db.execute("SELECT event_type FROM events WHERE id>1")] == ["specs_became_available"] * 3
        prior = counts(store)
        process_run(store, "hmd-nokia", 1, discoveries, [], False, run_id=run_id, notify=enqueue(store))
        assert counts(store) == prior
        repeat = process_run(store, "hmd-nokia", 1, discoveries, [], False, run_id=store.run_started("hmd-nokia"), notify=enqueue(store))
        assert repeat["events_created"] == 0 and repeat["unchanged_observations"] == 3
    finally:
        store.close()


def test_v7_marker_missing_chronology_column_fails_closed(store):
    store.db.execute("ALTER TABLE collector_runs DROP COLUMN persistence_stats_json")
    assert inspect_compatibility(store.db).state is StateCompatibility.PARTIAL


def test_run_receipt_rejects_cross_source(store):
    sid = setup_source(store)
    wrong_run = store.run_started("other-source")
    with pytest.raises(ValueError, match="belong to this source"):
        persist(store, sid, [content("micro-usb")], run_id=wrong_run)
    assert counts(store)["observations"] == 0
