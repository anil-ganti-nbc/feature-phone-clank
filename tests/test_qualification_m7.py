from __future__ import annotations

from feature_phone_clank.core.qualification import (
    QualificationProvenance,
    gate,
    events,
    finish,
    material_identity,
    prepare,
)
from feature_phone_clank.providers.sqlite import SqliteStore


def _run(store, source, scope, material, provenance=QualificationProvenance.SCHEDULED):
    run_id = store.run_started(source)
    context = prepare(
        store, run_id=run_id, scope_key=scope,
        material=material_identity({"source": source, "material": material}),
        provenance=provenance,
    )
    return run_id, context


def test_changed_execution_resets_before_gate_and_preserves_lineage(tmp_path):
    store = SqliteStore(str(tmp_path / "qualification.db"))
    try:
        _, first = _run(store, "source-a", "production:source-a", "A")
        finish(store, first, "ok")
        assert gate(store, "production:source-a")["eligible"]

        run_id, changed = _run(store, "source-a", "production:source-a", "B")
        assert changed.epoch_id != first.epoch_id
        assert changed.gate_status == "NOT_QUALIFIED"
        reset = [row for row in events(store, "production:source-a") if row["event_type"] == "RESET"]
        assert len(reset) == 1
        assert reset[0]["run_id"] == run_id
        assert reset[0]["prior_material_identity"]
        assert reset[0]["material_identity"] != reset[0]["prior_material_identity"]
        assert not gate(store, "production:source-a")["eligible"]

        finish(store, changed, "ok")
        finish(store, changed, "ok")  # terminal persistence is idempotent
        terminals = [row for row in events(store, "production:source-a") if row["event_type"] == "TERMINAL"]
        assert len(terminals) == 2
        assert gate(store, "production:source-a")["eligible"]
    finally:
        store.close()


def test_scopes_are_isolated_and_unknown_never_qualifies(tmp_path):
    store = SqliteStore(str(tmp_path / "qualification.db"))
    try:
        _, a = _run(store, "source-a", "production:source-a", "A")
        finish(store, a, "ok")
        _, b = _run(store, "source-b", "production:source-b", "B")
        finish(store, b, "ok")
        assert gate(store, "production:source-a")["eligible"]
        assert gate(store, "production:source-b")["eligible"]

        _, a_changed = _run(store, "source-a", "production:source-a", "A2")
        assert not gate(store, "production:source-a")["eligible"]
        assert gate(store, "production:source-b")["eligible"]

        _, unknown = _run(store, "source-c", "production:source-c", "C", QualificationProvenance.UNKNOWN)
        finish(store, unknown, "ok")
        assert not gate(store, "production:source-c")["eligible"]
        assert events(store, "production:source-c")[0]["provenance"] == "UNKNOWN"
    finally:
        store.close()


def test_migration_is_additive_and_existing_runs_survive(tmp_path):
    path = tmp_path / "qualification.db"
    first = SqliteStore(str(path))
    run_id = first.run_started("legacy-source")
    first.run_finished(run_id, "ok", {"legacy": True}, products_observed=1)
    first.close()

    second = SqliteStore(str(path))
    try:
        assert second.schema_version() == 5
        row = second.db.execute("SELECT status, stats_json, provenance FROM collector_runs WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "ok" and '"legacy": true' in row["stats_json"]
        assert row["provenance"] == "UNKNOWN"
        assert {r["name"] for r in second.db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "qualification_state", "qualification_epochs", "qualification_events",
        }
    finally:
        second.close()
