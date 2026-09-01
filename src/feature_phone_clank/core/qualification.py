"""Feature Phone qualification evidence lifecycle.

This is intentionally a small projection over the existing collector store,
not a replacement for the continuity/data-loss epoch.  The collector run is
the execution authority; this module only records the qualification facts
that the existing runner has established.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class QualificationProvenance(str, Enum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    TEST = "TEST"
    UNKNOWN = "UNKNOWN"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_provenance(value: QualificationProvenance | str | None) -> str:
    if isinstance(value, QualificationProvenance):
        return value.value
    try:
        return QualificationProvenance(str(value or "UNKNOWN").upper()).value
    except ValueError:
        return QualificationProvenance.UNKNOWN.value


def material_identity(inputs: dict[str, Any]) -> str:
    """Hash only stable qualification-relevant inputs, never runtime facts."""
    payload = {str(k): inputs[k] for k in sorted(inputs)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QualificationContext:
    run_id: int
    scope_key: str
    epoch_id: int
    material_identity: str
    provenance: str
    gate_status: str


def _gate(conn, scope_key: str, epoch_id: int, material: str, provenance: str) -> dict[str, Any]:
    if provenance == QualificationProvenance.UNKNOWN.value:
        return {"eligible": False, "status": "UNKNOWN", "reason": "missing or untrusted provenance"}
    rows = conn.execute(
        "SELECT 1 FROM qualification_events WHERE scope_key=? AND epoch_id=? "
        "AND event_type='TERMINAL' AND status='ok' AND counts_for_qualification=1 "
        "AND material_identity=? LIMIT 1",
        (scope_key, epoch_id, material),
    ).fetchone()
    if rows is None:
        return {"eligible": False, "status": "NOT_QUALIFIED", "reason": "no qualifying terminal evidence in current epoch"}
    return {"eligible": True, "status": "QUALIFIED", "reason": "current epoch has qualifying terminal evidence"}


def prepare(store, *, run_id: int, scope_key: str, material: str,
            provenance: QualificationProvenance | str | None,
            reset_reason: str = "material identity changed") -> QualificationContext:
    """Prepare the current scope before any gated evidence is read.

    The reset row is inserted before the caller invokes the collector.  A
    separate terminal row is written by :func:`finish`, so both facts remain
    independently auditable for one execution.
    """
    if not scope_key:
        raise ValueError("qualification scope_key is required")
    provenance_value = normalize_provenance(provenance)
    conn = store.db
    current = conn.execute(
        "SELECT epoch_id, material_identity FROM qualification_state WHERE scope_key=?",
        (scope_key,),
    ).fetchone()
    prior_material = current["material_identity"] if current else None
    if current is None or prior_material != material:
        cur = conn.execute(
            "INSERT INTO qualification_epochs(scope_key, material_identity, prior_material_identity, reset_reason) "
            "VALUES (?,?,?,?)",
            (scope_key, material, prior_material, None if current is None else reset_reason),
        )
        epoch_id = cur.lastrowid
        conn.execute(
            "INSERT INTO qualification_state(scope_key, epoch_id, material_identity, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET epoch_id=excluded.epoch_id, "
            "material_identity=excluded.material_identity, updated_at=excluded.updated_at",
            (scope_key, epoch_id, material, utcnow()),
        )
        if current is not None:
            conn.execute(
                "INSERT OR IGNORE INTO qualification_events(run_id, scope_key, epoch_id, event_type, provenance, "
                "material_identity, prior_material_identity, status, counts_for_qualification) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (run_id, scope_key, epoch_id, "RESET", provenance_value, material, prior_material, reset_reason),
            )
    else:
        epoch_id = current["epoch_id"]
    gate = _gate(conn, scope_key, epoch_id, material, provenance_value)
    conn.execute(
        "UPDATE collector_runs SET provenance=?, qualification_scope=?, qualification_epoch_id=?, "
        "qualification_material_identity=?, qualification_gate_status=? WHERE id=?",
        (provenance_value, scope_key, epoch_id, material, gate["status"], run_id),
    )
    conn.commit()
    return QualificationContext(run_id, scope_key, epoch_id, material, provenance_value, gate["status"])


def finish(store, context: QualificationContext, status: str) -> None:
    """Persist terminal execution evidence idempotently."""
    conn = store.db
    counts = int(status == "ok" and context.provenance == QualificationProvenance.SCHEDULED.value)
    conn.execute(
        "INSERT OR IGNORE INTO qualification_events(run_id, scope_key, epoch_id, event_type, provenance, "
        "material_identity, status, counts_for_qualification) VALUES (?,?,?,?,?,?,?,?)",
        (context.run_id, context.scope_key, context.epoch_id, "TERMINAL", context.provenance,
         context.material_identity, status, counts),
    )
    conn.commit()


def gate(store, scope_key: str, *, material: str | None = None) -> dict[str, Any]:
    row = store.db.execute(
        "SELECT epoch_id, material_identity FROM qualification_state WHERE scope_key=?",
        (scope_key,),
    ).fetchone()
    if row is None:
        return {"eligible": False, "status": "UNKNOWN", "reason": "scope has no qualification epoch"}
    if material is not None and row["material_identity"] != material:
        return {"eligible": False, "status": "STALE", "reason": "material identity diverges from current epoch"}
    rows = store.db.execute(
        "SELECT 1 FROM qualification_events WHERE scope_key=? AND epoch_id=? AND event_type='TERMINAL' "
        "AND status='ok' AND counts_for_qualification=1 LIMIT 1",
        (scope_key, row["epoch_id"]),
    ).fetchone()
    return ({"eligible": True, "status": "QUALIFIED", "reason": "current epoch has qualifying terminal evidence"}
            if rows else {"eligible": False, "status": "NOT_QUALIFIED", "reason": "no qualifying terminal evidence in current epoch"})


def events(store, scope_key: str) -> list:
    return store.db.execute(
        "SELECT * FROM qualification_events WHERE scope_key=? ORDER BY id", (scope_key,)
    ).fetchall()
