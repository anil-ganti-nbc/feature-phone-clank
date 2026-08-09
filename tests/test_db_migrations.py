from __future__ import annotations

from feature_phone_clank.providers.sqlite import SCHEMA_VERSION, SqliteStore


def test_migration_creates_expected_tables(store):
    tables = {
        r["name"]
        for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "schema_migrations", "sources", "products", "observations",
        "events", "collector_runs", "run_errors", "notifications",
    }
    assert expected <= tables
    assert store.schema_version() == SCHEMA_VERSION


def test_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    s1 = SqliteStore(db_path)
    s1.ensure_source("src-a", "TestCo", "catalogue", None, "https://example.test", {})
    s1.close()

    # Re-opening (which re-runs migrate()) must not raise, duplicate rows,
    # or lose existing data.
    s2 = SqliteStore(db_path)
    try:
        assert s2.schema_version() == SCHEMA_VERSION
        count = s2.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
        assert count == 1
        migration_rows = s2.db.execute(
            "SELECT COUNT(*) c FROM schema_migrations"
        ).fetchone()["c"]
        assert migration_rows == SCHEMA_VERSION
    finally:
        s2.close()

    # A third open, still idempotent.
    s3 = SqliteStore(db_path)
    try:
        assert s3.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
    finally:
        s3.close()
