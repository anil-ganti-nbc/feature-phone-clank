"""M14 persistent-state compatibility barrier (STD-DEPLOY-COM-002).

Every test here runs against disposable local SQLite files — no production
database is touched, no collector executes live, no deployment action is
taken. The v4-shaped fixture mirrors the real production database's
documented pre-M14 state (schema_migrations stamped 1..4, qualification
tables absent, collector_runs without its v5 columns) so the canonical
migration path is exercised honestly.

Covers the M14 contract: fresh-vs-unknown distinction, read-only
inspection, canonical bootstrap/migration with post-admission reverification,
fail-closed refusals (newer / unknown / corrupt / partial / failed-migration
state), entry-point coverage (CLI run/status, dashboard render + QC POST,
local collection controller), lock/qualification orthogonality, and
normal-work regressions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from feature_phone_clank.cli import main
from feature_phone_clank.core.qualification import (
    QualificationProvenance,
    finish as finish_qualification,
    prepare as prepare_qualification,
)
from feature_phone_clank.core.run_lock import RunLock
from feature_phone_clank.dashboard import render, serve
from feature_phone_clank.local_collection import LocalCollectionController
from feature_phone_clank.paths import resolve_data_path
from feature_phone_clank.providers.qc_store import (
    QcArchiveStore,
    QcStateCompatibilityError,
)
from feature_phone_clank.providers.sqlite import (
    SCHEMA_VERSION,
    SqliteStore,
    StateCompatibilityError,
)
from feature_phone_clank.providers.sqlite.compatibility import (
    EXPECTED_TABLES,
    StateCompatibility,
    inspect_compatibility,
)

# -- fixtures ----------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_v4_db(path: Path) -> None:
    """A faithful v4-shaped database: the exact pre-M14 production shape
    (marker stamped 1..4, no qualification tables, collector_runs without
    the v5 columns). Built with plain DDL — no git, no network."""
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL UNIQUE,
            manufacturer TEXT NOT NULL,
            source_type TEXT NOT NULL,
            region TEXT,
            base_url TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id),
            product_key TEXT NOT NULL UNIQUE,
            manufacturer TEXT NOT NULL,
            model TEXT NOT NULL,
            model_number TEXT,
            region TEXT,
            url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            consecutive_absences INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            content_hash TEXT NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '{}',
            spec_completeness TEXT NOT NULL DEFAULT 'complete',
            price REAL, currency TEXT, availability TEXT,
            observed_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(product_id, content_hash)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            collector TEXT NOT NULL,
            event_type TEXT NOT NULL,
            field TEXT, previous_value_json TEXT, new_value_json TEXT,
            changed_fields_json TEXT NOT NULL DEFAULT '[]',
            previous_observation_id INTEGER REFERENCES observations(id),
            current_observation_id INTEGER REFERENCES observations(id),
            dedup_key TEXT,
            alert_level TEXT NOT NULL,
            confidence TEXT NOT NULL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            meta_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX idx_events_dedup ON events(dedup_key) WHERE dedup_key IS NOT NULL;
        CREATE TABLE collector_runs (
            id INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            products_observed INTEGER,
            previous_products_observed INTEGER,
            stats_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE run_errors (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES collector_runs(id),
            message TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY,
            event_id INTEGER REFERENCES events(id),
            provider TEXT NOT NULL,
            dedup_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at TEXT
        );
        CREATE TABLE classification_log (
            id INTEGER PRIMARY KEY,
            source_key TEXT NOT NULL,
            slug TEXT NOT NULL,
            url TEXT NOT NULL,
            classification TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source_key, slug)
        );
        """
    )
    for v in (1, 2, 3, 4):
        c.execute("INSERT INTO schema_migrations(version) VALUES (?)", (v,))
    c.execute(
        "INSERT INTO sources(source_key, manufacturer, source_type, base_url) "
        "VALUES ('legacy', 'LegacyCo', 'catalogue', 'https://example.test')"
    )
    c.commit()
    c.close()


@pytest.fixture
def v4_db(tmp_path) -> Path:
    db = tmp_path / "v4.db"
    _make_v4_db(db)
    return db


def _marker_version(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    finally:
        con.close()


# -- 1-3: fresh identification, canonical bootstrap, expected state ----------


def test_genuinely_fresh_db_is_identified_as_fresh(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")  # 0-byte file is a valid, untouched SQLite database
    con = sqlite3.connect(f"file:{empty.as_posix()}?mode=ro", uri=True)
    try:
        report = inspect_compatibility(con, expected_version=SCHEMA_VERSION)
    finally:
        con.close()
    assert report.state is StateCompatibility.FRESH
    assert report.observed_version is None


def test_marker_less_existing_db_is_unknown_never_fresh(tmp_path):
    db = tmp_path / "premarker.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, model TEXT)")
    con.commit()
    con.close()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        report = inspect_compatibility(con, expected_version=SCHEMA_VERSION)
    finally:
        con.close()
    # FRESH != UNKNOWN: an existing database with a missing version marker
    # must never be classified as fresh (and never silently bootstrapped).
    assert report.state is StateCompatibility.UNKNOWN
    assert report.state is not StateCompatibility.FRESH


def test_fresh_db_bootstraps_through_canonical_mechanism(tmp_path):
    db = tmp_path / "fresh.db"
    store = SqliteStore(str(db))
    try:
        assert store.schema_version() == SCHEMA_VERSION
        assert store.compatibility_report.state is StateCompatibility.COMPATIBLE
        stamped = [
            r["version"] for r in
            store.db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert stamped == list(range(1, SCHEMA_VERSION + 1))
        # the store is usable: canonical persistence works end to end
        source_id = store.ensure_source("s", "Co", "catalogue", None, "https://x", {})
        assert source_id == 1
    finally:
        store.close()


def test_expected_version_state_is_compatible(tmp_path):
    db = tmp_path / "current.db"
    SqliteStore(str(db)).close()
    store = SqliteStore(str(db))
    try:
        assert store.schema_version() == SCHEMA_VERSION
        report = store.compatibility_report
        assert report.state is StateCompatibility.COMPATIBLE
        assert report.observed_version == SCHEMA_VERSION == report.expected_version
        assert EXPECTED_TABLES <= set(report.evidence["user_tables"])
    finally:
        store.close()


# -- 4: non-destructive inspection -------------------------------------------


def test_compatible_open_and_close_is_non_destructive(tmp_path):
    db = tmp_path / "stable.db"
    SqliteStore(str(db)).close()  # create + checkpoint
    before = _sha(db)
    store = SqliteStore(str(db))
    try:
        assert store.compatibility_report.state is StateCompatibility.COMPATIBLE
    finally:
        store.close()
    assert _sha(db) == before  # a compatible open performs no mutation


def test_inspection_of_refused_states_is_read_only(tmp_path):
    db = tmp_path / "newer.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    before = _sha(db)
    with pytest.raises(StateCompatibilityError):
        SqliteStore(str(db))
    assert _sha(db) == before  # refusal left the file byte-identical


# -- 5-7: older valid state (the real production DB's documented shape) ------


def test_older_valid_state_migrates_only_through_canonical_mechanism(v4_db):
    assert _marker_version(v4_db) == 4
    con = sqlite3.connect(f"file:{v4_db.as_posix()}?mode=ro", uri=True)
    try:
        pre = inspect_compatibility(con, expected_version=SCHEMA_VERSION)
    finally:
        con.close()
    assert pre.state is StateCompatibility.MIGRATION_REQUIRED

    store = SqliteStore(str(v4_db))  # admission = canonical migration
    try:
        assert store.schema_version() == SCHEMA_VERSION
        # the migration steps are the canonical _MIGRATIONS, not a rebuild:
        # pre-existing data survives and qualification tables now exist
        assert store.db.execute(
            "SELECT COUNT(*) c FROM sources WHERE source_key='legacy'"
        ).fetchone()["c"] == 1
        for table in ("qualification_state", "qualification_epochs", "qualification_events"):
            assert store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None
        assert [r["version"] for r in store.db.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == list(range(1, SCHEMA_VERSION + 1))
    finally:
        store.close()


def test_migrated_state_is_explicitly_reverified(tmp_path, v4_db, monkeypatch):
    """If post-admission reverification did not run, a broken migration could
    hand back an unusable store. Sabotage the migration step: admission must
    fail closed and normal work must remain impossible."""
    calls = {"migrated": False}

    def sabotaged(self, from_version):
        calls["migrated"] = True
        raise sqlite3.OperationalError("sabotaged canonical migration")

    monkeypatch.setattr(SqliteStore, "_migrate_incremental", sabotaged)
    with pytest.raises(StateCompatibilityError):
        SqliteStore(str(v4_db))
    assert calls["migrated"] is True  # migration was attempted and refused
    assert _marker_version(v4_db) == 4  # state unchanged, not stamped ready
    monkeypatch.undo()
    # and the preserved state still admits cleanly through the real path
    store = SqliteStore(str(v4_db))
    try:
        assert store.schema_version() == SCHEMA_VERSION
    finally:
        store.close()


# -- 8-12: fail-closed refusals ----------------------------------------------


def test_newer_state_fails_closed(tmp_path):
    db = tmp_path / "newer.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    before = _sha(db)
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    report = excinfo.value.report
    assert report.state is StateCompatibility.INCOMPATIBLE_NEWER
    assert report.observed_version == SCHEMA_VERSION + 1
    assert "FORWARD_ONLY_EXPLICIT" in report.reason
    evidence = json.loads(json.dumps(report.as_evidence()))  # JSON-serializable
    assert evidence["compatibility_state"] == "INCOMPATIBLE_NEWER"
    assert _sha(db) == before
    # the newer marker must still be there: nothing was downgraded or removed
    assert _marker_version(db) == SCHEMA_VERSION + 1


def test_missing_version_authority_on_existing_db_fails_closed(tmp_path):
    db = tmp_path / "unknown.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, model TEXT)")
    con.execute("INSERT INTO products(model) VALUES ('kept for diagnosis')")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    assert excinfo.value.report.state is StateCompatibility.UNKNOWN
    # unknown state is preserved for diagnosis, never deleted or stamped
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT model FROM products").fetchone()[0] == "kept for diagnosis"
    finally:
        con.close()


def test_corrupt_version_metadata_fails_closed(tmp_path):
    db = tmp_path / "corrupt_marker.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE schema_migrations (version TEXT, applied_at TEXT)")
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO schema_migrations(version) VALUES ('garbage')")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    assert excinfo.value.report.state is StateCompatibility.UNKNOWN


def test_contradictory_state_fails_closed(tmp_path):
    """A marker table without the expected shape (no 'version' column) is
    contradictory authority, not compatibility."""
    db = tmp_path / "contradictory.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE schema_migrations (something_else TEXT)")
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    assert excinfo.value.report.state is StateCompatibility.UNKNOWN


def test_partial_state_fails_closed(tmp_path):
    db = tmp_path / "partial.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE notifications")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    report = excinfo.value.report
    assert report.state is StateCompatibility.PARTIAL
    assert "notifications" in report.evidence["missing_tables"]


def test_not_a_database_file_fails_closed(tmp_path):
    db = tmp_path / "junk.db"
    db.write_bytes(b"not a sqlite database at all" * 64)
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(db))
    assert excinfo.value.report.state is StateCompatibility.CORRUPT


# -- 13: failed migration cannot mark state ready -----------------------------


def test_failed_migration_cannot_mark_state_ready(v4_db):
    """Simulate a half-applied canonical migration (the v5 column already
    added, marker still at 4): the canonical migration must fail, roll back,
    and leave the state unadmitted with the failure preserved as evidence."""
    con = sqlite3.connect(str(v4_db))
    con.execute("ALTER TABLE collector_runs ADD COLUMN provenance TEXT NOT NULL DEFAULT 'UNKNOWN'")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError) as excinfo:
        SqliteStore(str(v4_db))
    report = excinfo.value.report
    assert report.state is StateCompatibility.MIGRATION_REQUIRED
    assert "admission_failure" in report.evidence
    assert _marker_version(v4_db) == 4  # version authority not advanced
    # retry crosses compatibility inspection again and still refuses
    with pytest.raises(StateCompatibilityError):
        SqliteStore(str(v4_db))


# -- 14-15: CLI entry points cross the barrier --------------------------------


def _write_scope(tmp_path, collectors):
    scope = tmp_path / "scope.yaml"
    scope.write_text(f"production_collectors: {collectors}\n", encoding="utf-8")
    return scope


def test_cli_run_refuses_incompatible_state_with_evidence(tmp_path, capsys):
    """The scheduler-invoked path (`feature-phone-clank run`) crosses the
    barrier: incompatible state is refused before any collector executes."""
    db = tmp_path / "newer.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    before = _sha(db)
    scope = _write_scope(tmp_path, ["hmd-nokia"])
    rc = main(["--db", str(db), "--scope", str(scope), "run", "--no-lock", "--no-deliver"])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "state_incompatible"
    assert payload["gate"] == "persistent_state_compatibility"
    assert payload["compatibility_state"] == "INCOMPATIBLE_NEWER"
    assert _sha(db) == before


def test_cli_run_admits_older_valid_state_through_migration(v4_db, tmp_path, capsys):
    """The scheduler path on the real pre-M14 production shape: admission
    migrates canonically at the store boundary before any run logic executes
    (the collector name is deliberately unregistered, so nothing live runs)."""
    scope = _write_scope(tmp_path, ["not-a-registered-collector"])
    rc = main(["--db", str(v4_db), "--scope", str(scope), "run", "--no-lock", "--no-deliver"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["results"][0]["status"] == "unregistered"
    assert _marker_version(v4_db) == SCHEMA_VERSION


def test_cli_read_commands_refuse_incompatible_state(tmp_path, capsys):
    db = tmp_path / "unknown.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    for argv in (
        ["--db", str(db), "status"],
        ["--db", str(db), "events"],
        ["--db", str(db), "notifications"],
    ):
        rc = main(argv)
        assert rc == 3, argv
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "state_incompatible"


def test_cli_run_experimental_refusal_never_touches_production_db(tmp_path, capsys):
    exp_db = tmp_path / "experimental.db"
    con = sqlite3.connect(str(exp_db))
    con.execute("CREATE TABLE leftover (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    before = _sha(exp_db)
    scope = _write_scope(tmp_path, [])
    prod_db = tmp_path / "prod.db"
    rc = main([
        "--db", str(prod_db), "--scope", str(scope),
        "run-experimental", "--experimental-db", str(exp_db),
        "--sources", "itel-india", "--no-lock",
    ])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "state_incompatible"
    assert _sha(exp_db) == before
    assert not prod_db.exists()  # scope isolation intact: production path untouched


# -- 16: qualification/collection path cannot bypass the barrier --------------


def _controller(tmp_path, database: Path, collectors) -> LocalCollectionController:
    return LocalCollectionController(
        database=str(database),
        lock_path=str(tmp_path / "controller.lock"),
        scope_path=str(_write_scope(tmp_path, collectors)),
        overrides_path=str(tmp_path / "overrides.yaml"),
    )


def test_controller_run_blocked_by_barrier_with_evidence(tmp_path):
    db = tmp_path / "newer.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    before = _sha(db)
    controller = _controller(tmp_path, db, ["hmd-nokia"])

    controller._run_one("hmd-nokia", "production")  # worker body, synchronous

    state = controller.snapshot_for("hmd-nokia", "production")
    assert state["state"] == "blocked"
    assert "persistent-state compatibility refused" in state["message"]
    assert state["result"]["compatibility_state"] == "INCOMPATIBLE_NEWER"
    assert _sha(db) == before  # no collector ever touched the refused state


def test_qualification_evidence_flow_works_after_admission(v4_db):
    """OPS-COM-003 qualification lifecycle intact on admitted (migrated)
    state: the barrier gates admission, it does not alter the qualification
    contract once admitted."""
    store = SqliteStore(str(v4_db))
    try:
        run_id = store.run_started("legacy", provenance="SCHEDULED", qualification_scope="production:legacy")
        context = prepare_qualification(
            store, run_id=run_id, scope_key="production:legacy",
            material="material-identity-1", provenance=QualificationProvenance.SCHEDULED,
        )
        finish_qualification(store, context, "ok")
        gate = store.db.execute(
            "SELECT qualification_gate_status FROM collector_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert gate is not None
        events = store.db.execute(
            "SELECT event_type FROM qualification_events WHERE scope_key='production:legacy' ORDER BY id"
        ).fetchall()
        assert [e["event_type"] for e in events] == ["TERMINAL"]
    finally:
        store.close()


# -- 17: dashboard surfaces cross the barrier ---------------------------------


def test_dashboard_render_refuses_incompatible_state(tmp_path):
    db = tmp_path / "unknown.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE leftover (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(StateCompatibilityError):
        render(db)


def test_dashboard_http_refuses_with_503_and_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    database = resolve_data_path("data/feature_phone_clank.db")
    con = sqlite3.connect(str(database))
    con.execute("CREATE TABLE leftover (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    server = serve(port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3)
            raise AssertionError("dashboard must be refused on incompatible state")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = exc.read().decode()
            assert "Persistent-state compatibility refused" in body
            assert "UNKNOWN" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_dashboard_qc_post_refuses_with_503_and_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    database = resolve_data_path("data/feature_phone_clank.db")
    con = sqlite3.connect(str(database))
    con.execute("CREATE TABLE leftover (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    controller = LocalCollectionController(
        database=str(database),
        lock_path=str(tmp_path / "dash.lock"),
        scope_path=str(_write_scope(tmp_path, ["hmd-nokia"])),
        overrides_path=str(tmp_path / "overrides.yaml"),
    )
    server = serve(port=0, controller=controller)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/qc/review/1",
            data=json.dumps({"decision": "USEFUL"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
            raise AssertionError("QC POST must be refused on incompatible state")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            payload = json.loads(exc.read().decode())
            assert payload.get("error") == "state_incompatible"
            assert payload["gate"] == "persistent_state_compatibility"
    finally:
        server.shutdown()
        thread.join(timeout=3)


# -- 18: lock/qualification ownership cannot bypass the barrier ---------------


def test_run_lock_ownership_cannot_admit_incompatible_state(tmp_path):
    lock = RunLock.acquire(tmp_path / "run.lock")
    try:
        db = tmp_path / "newer.db"
        SqliteStore(str(db)).close()
        con = sqlite3.connect(str(db))
        con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
        con.commit()
        con.close()
        with pytest.raises(StateCompatibilityError):
            SqliteStore(str(db))
    finally:
        lock.release()


# -- 19: normal current-version execution remains intact ----------------------


def test_normal_current_version_execution_remains_intact(tmp_path):
    from tests.helpers import ScriptedCollector, classification_entry, make_discovery
    from feature_phone_clank.core.runner import run_production_collector
    from feature_phone_clank.core.scope import load_scope

    db = tmp_path / "normal.db"
    store = SqliteStore(str(db))
    try:
        scope = load_scope(str(_write_scope(tmp_path, ["test-scripted"])))
        collector = ScriptedCollector([
            ([make_discovery("nokia-105")], [classification_entry("nokia-105", "feature_phone")]),
        ])
        result, stats = run_production_collector(
            collector, store, scope,
            manufacturer="TestCo", source_type="catalogue",
            region=None, base_url="https://example.test",
        )
        assert result.status == "ok"
        assert stats["new_products"] == 1
        # a first run establishes the baseline: no change events yet
        assert stats["events_created"] == 0
        assert store.active_product_count("test-scripted") == 1
        assert store.schema_version() == SCHEMA_VERSION
    finally:
        store.close()


# -- QC archive barrier --------------------------------------------------------


def test_qc_archive_fresh_bootstraps_and_reopens_compatible(tmp_path):
    qc_db = tmp_path / "qc.db"
    store = QcArchiveStore(str(qc_db))
    store.close()
    store = QcArchiveStore(str(qc_db))
    try:
        assert store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='qc_reviews'"
        ).fetchone() is not None
    finally:
        store.close()


def test_qc_archive_preexisting_exact_shape_is_grandfathered(tmp_path):
    """A QC archive written before M14 (no gate existed then) matches the
    expected shape and must keep working — read-write, not refused."""
    qc_db = tmp_path / "legacy_qc.db"
    con = sqlite3.connect(str(qc_db))
    con.executescript(
        """
        CREATE TABLE qc_reviews (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            product_key TEXT,
            manufacturer TEXT,
            model TEXT,
            model_number TEXT,
            url TEXT,
            event_type TEXT NOT NULL,
            changed_fields_json TEXT NOT NULL DEFAULT '[]',
            meta_json TEXT NOT NULL DEFAULT '{}',
            detected_at TEXT,
            run_id INTEGER,
            run_started_at TEXT,
            decision TEXT NOT NULL,
            reason TEXT,
            decided_at TEXT NOT NULL,
            is_corrected INTEGER NOT NULL DEFAULT 0,
            review_metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(event_id)
        );
        """
    )
    con.commit()
    con.close()
    store = QcArchiveStore(str(qc_db))
    try:
        result = store.submit_review(
            event_id=1, source_key="hmd-nokia", event_type="new_product",
            decision="USEFUL",
        )
        assert result["event_id"] == 1
    finally:
        store.close()


def test_qc_archive_wrong_shape_fails_closed(tmp_path):
    qc_db = tmp_path / "bad_qc.db"
    con = sqlite3.connect(str(qc_db))
    con.execute("CREATE TABLE qc_reviews (id INTEGER PRIMARY KEY, event_id INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(QcStateCompatibilityError) as excinfo:
        QcArchiveStore(str(qc_db))
    assert "shape differs" in excinfo.value.evidence["reason"]


def test_qc_archive_foreign_tables_only_fails_closed(tmp_path):
    qc_db = tmp_path / "foreign_qc.db"
    con = sqlite3.connect(str(qc_db))
    con.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(QcStateCompatibilityError) as excinfo:
        QcArchiveStore(str(qc_db))
    assert excinfo.value.evidence["compatibility_state"] == "UNKNOWN"
    assert "not fresh" in excinfo.value.evidence["reason"]


def test_qc_archive_corrupt_file_fails_closed(tmp_path):
    qc_db = tmp_path / "junk_qc.db"
    qc_db.write_bytes(b"definitely not a database" * 32)
    with pytest.raises(QcStateCompatibilityError):
        QcArchiveStore(str(qc_db))


# -- health reports compatibility honestly ------------------------------------


def test_health_probe_reports_incompatible_state_without_mutation(tmp_path):
    from feature_phone_clank.runtime_bridge import _probe_sqlite

    db = tmp_path / "newer.db"
    SqliteStore(str(db)).close()
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    before = _sha(db)
    writable, last, reasons, compat = _probe_sqlite(db)
    assert compat is StateCompatibility.INCOMPATIBLE_NEWER
    assert any(r.startswith("persistent_state: INCOMPATIBLE_NEWER") for r in reasons)
    assert _sha(db) == before  # the probe never mutates


def test_health_probe_reports_compatible_state_as_healthy(tmp_path):
    from feature_phone_clank.runtime_bridge import _probe_sqlite

    db = tmp_path / "current.db"
    SqliteStore(str(db)).close()
    writable, last, reasons, compat = _probe_sqlite(db)
    assert compat is StateCompatibility.COMPATIBLE
    assert not any(r.startswith("persistent_state:") for r in reasons)


# -- semantics pin: the states are distinct values -----------------------------


def test_state_vocabulary_is_distinct():
    values = {s.value for s in StateCompatibility}
    assert values == {
        "FRESH", "COMPATIBLE", "MIGRATION_REQUIRED", "INCOMPATIBLE_NEWER",
        "UNKNOWN", "PARTIAL", "CORRUPT",
    }
    assert StateCompatibility.UNKNOWN is not StateCompatibility.COMPATIBLE
    assert StateCompatibility.FRESH is not StateCompatibility.UNKNOWN
