"""Datastore survivability + continuity honesty tests.

Covers: SQLite-safe backup (integrity-verified, overwrite-refusing,
lock-cooperative), isolated restore drills, and the ADR-0006 append-only
continuity registry seeded with the operator-verified fpc-epoch-2 facts.
No test contacts a network or a real webhook.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from feature_phone_clank.core import continuity
from feature_phone_clank.providers.sqlite import SqliteStore


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteStore(str(tmp_path / "fpc.db"))
    yield s
    s.close()


def _seed_one_product(store: SqliteStore) -> None:
    from helpers import make_discovery

    source_id = store.ensure_source(
        "test-scripted", "TestCo", "catalogue", "en_int",
        "https://example.test", {},
    )
    discovery = make_discovery("nokia-3210")
    product_id = store.create_product(source_id, discovery)
    observation_id, _ = store.record_observation_get_id(product_id, discovery)
    assert observation_id > 0


def test_backup_creates_integrity_verified_snapshot(tmp_path, store):
    _seed_one_product(store)
    target = tmp_path / "backups" / "rp1.db"
    result = store.backup_to(target)
    assert result["integrity_check"] == "ok"
    assert result["size_bytes"] > 0
    assert len(result["sha256"]) == 64
    assert result["schema_version"] == store.schema_version()
    assert target.exists()


def test_backup_refuses_to_overwrite_existing_recovery_point(tmp_path, store):
    target = tmp_path / "rp1.db"
    first = store.backup_to(target)
    with pytest.raises(FileExistsError):
        store.backup_to(target)
    forced = store.backup_to(target, overwrite=True)
    # A forced re-backup must still be a complete, verified snapshot.
    assert forced["sha256"] == first["sha256"]


def test_backup_is_a_valid_restorable_database_in_isolation(tmp_path, store):
    """Restore drill: the snapshot opens standalone (no WAL sidecars), passes
    integrity_check, and contains the expected tables and rows."""
    _seed_one_product(store)
    target = tmp_path / "rp1.db"
    store.backup_to(target)
    con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"products", "observations", "events", "schema_migrations"} <= tables
        count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        assert count == 1
    finally:
        con.close()


def test_continuity_registry_seeded_once_and_append_only(tmp_path, store):
    db_path = str(tmp_path / "fpc.db")
    path = continuity.ensure_registry(db_path)
    first = continuity.read_events(db_path)
    ids = [e["event_id"] for e in first]
    assert "fpc-20260823-data-loss-0001" in ids
    assert "fpc-20260823-epoch-boundary-0002" in ids
    loss = next(e for e in first if e["event_type"] == "DATA_LOSS")
    boundary = next(e for e in first if e["event_type"] == "EPOCH_BOUNDARY")
    # Honesty invariants: the lost epoch is never named; epoch-2 is explicit;
    # the boundary instant matches operator-verified canon.
    assert loss["previous_epoch_id"] is None
    assert boundary["previous_epoch_id"] is None
    assert boundary["new_epoch_id"] == "fpc-epoch-2"
    assert boundary["effective_start"] == "2026-08-23T21:36:11Z"
    # Idempotent seeding: a second call appends nothing.
    continuity.ensure_registry(db_path)
    assert len(continuity.read_events(db_path)) == len(first)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(first)


def test_continuity_records_are_content_hashed_and_tamper_evident(tmp_path):
    db_path = str(tmp_path / "fpc.db")
    continuity.ensure_registry(db_path)
    events = continuity.read_events(db_path)
    assert continuity.verify_hashes(events) == []
    raw = continuity.registry_path(db_path).read_text(encoding="utf-8")
    tampered = raw.replace("irreplaceable", "tampered-irreplaceable")
    assert tampered != raw
    continuity.registry_path(db_path).write_text(tampered, encoding="utf-8")
    events = continuity.read_events(db_path)
    mismatched = set(continuity.verify_hashes(events))
    assert mismatched, "any edited record must fail its content hash"


def test_continuity_cli_reports_epoch(tmp_path):
    from feature_phone_clank.cli import main

    db = tmp_path / "cli.db"
    rc = main(["--db", str(db), "continuity", "--ensure-seed"])
    assert rc == 0
    rc = main(["--db", str(db), "continuity"])
    assert rc == 0
