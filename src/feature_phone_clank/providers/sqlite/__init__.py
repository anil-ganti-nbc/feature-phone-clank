"""SQLite provider: state-compatibility barrier + schema lifecycle +
product/observation/event persistence + run telemetry.

Construction of `SqliteStore` is the persistent-state compatibility
barrier (M14 / STD-DEPLOY-COM-002): the database is adjudicated read-only
by `compatibility.inspect_compatibility` before anything mutates; only
FRESH state bootstraps and known-older state migrates, both through the
canonical mechanism here, and both are re-verified before the store is
admitted. Unknown, corrupt, partial, and newer-than-expected state raise
`StateCompatibilityError` with full evidence and are left untouched.

Stage 3 adds deterministic diff/event generation (`core/diff.py` +
`core/pipeline.py`); this module only gained the storage primitives those
need (latest-observation lookup, absence counters, dedup'd event insert).
Decision logic (what counts as a change, baseline handling, removal
confirmation) lives in `core/pipeline.py`, not here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ...core.models import Discovery, Event
from .compatibility import (
    EXPECTED_SCHEMA_VERSION,
    UNADMITTABLE_STATES,
    CompatibilityReport,
    StateCompatibility,
    StateCompatibilityError,
    inspect_compatibility,
)

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# The expected persistent-state contract lives in compatibility.py (one
# authority shared by the schema, the migrations, and the compatibility
# gate); re-exported here under the historical name.
SCHEMA_VERSION = EXPECTED_SCHEMA_VERSION
# Future incremental migrations for databases created at an earlier version.
# Fresh databases get schema.sql directly and record all versions at once —
# same idempotent pattern as OEM Radar's sqlite provider.
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "CREATE TABLE IF NOT EXISTS classification_log ("
        "id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, slug TEXT NOT NULL, "
        "url TEXT NOT NULL, classification TEXT NOT NULL, "
        "evidence_json TEXT NOT NULL DEFAULT '{}', "
        "first_seen_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "last_seen_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE(source_key, slug))",
        "CREATE INDEX IF NOT EXISTS idx_classification_log_class "
        "ON classification_log(classification, last_seen_at)",
    ],
    3: [
        "ALTER TABLE observations ADD COLUMN spec_completeness TEXT NOT NULL DEFAULT 'complete'",
    ],
    4: [
        "ALTER TABLE products ADD COLUMN consecutive_absences INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE events ADD COLUMN changed_fields_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE events ADD COLUMN previous_observation_id INTEGER REFERENCES observations(id)",
        "ALTER TABLE events ADD COLUMN current_observation_id INTEGER REFERENCES observations(id)",
        "ALTER TABLE events ADD COLUMN dedup_key TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup ON events(dedup_key) "
        "WHERE dedup_key IS NOT NULL",
    ],
    5: [
        "ALTER TABLE collector_runs ADD COLUMN provenance TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "ALTER TABLE collector_runs ADD COLUMN qualification_scope TEXT",
        "ALTER TABLE collector_runs ADD COLUMN qualification_epoch_id INTEGER",
        "ALTER TABLE collector_runs ADD COLUMN qualification_material_identity TEXT",
        "ALTER TABLE collector_runs ADD COLUMN qualification_gate_status TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "CREATE TABLE IF NOT EXISTS qualification_state ("
        "scope_key TEXT PRIMARY KEY, epoch_id INTEGER NOT NULL, "
        "material_identity TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now'))) ",
        "CREATE TABLE IF NOT EXISTS qualification_epochs ("
        "id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, material_identity TEXT NOT NULL, "
        "prior_material_identity TEXT, reset_reason TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(scope_key, id))",
        "CREATE INDEX IF NOT EXISTS idx_qualification_epochs_scope ON qualification_epochs(scope_key, id)",
        "CREATE TABLE IF NOT EXISTS qualification_events ("
        "id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES collector_runs(id), "
        "scope_key TEXT NOT NULL, epoch_id INTEGER NOT NULL REFERENCES qualification_epochs(id), "
        "event_type TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT 'UNKNOWN', "
        "material_identity TEXT NOT NULL, prior_material_identity TEXT, status TEXT, "
        "counts_for_qualification INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(run_id, event_type))",
        "CREATE INDEX IF NOT EXISTS idx_qualification_events_scope ON qualification_events(scope_key, epoch_id, event_type)",
    ],
    6: [
        # Durable retry floor so a 429's Retry-After survives the process
        # that observed it. NULL means "eligible now" — every pre-existing
        # row keeps its current eligibility.
        "ALTER TABLE notifications ADD COLUMN not_before TEXT",
        # Durable activation policy (core/delivery_policy.py). Empty at
        # migration time: installing a cutoff is an explicit operator act,
        # never a side effect of upgrading.
        "CREATE TABLE IF NOT EXISTS delivery_policy ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
    ],
}


def _last_sent_at(row: sqlite3.Row | None) -> str | None:
    return row["sent_at"] if row is not None else None


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open read-only; never migrates, never locks. Safe to call against a
    live database (health checks, status queries)."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


class SqliteStore:
    # Depth of the current explicit `transaction()` block. While non-zero,
    # write helpers skip their own commit so a caller can make several
    # statements durable together (event + its outbox row).
    _tx_depth = 0

    def __init__(self, db_path: str = "data/feature_phone_clank.db") -> None:
        self.db_path = db_path
        self._tx_depth = 0
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Phase 1 — read-only inspection before any read-write handle exists.
        # A refused state is left byte-identical: not even the WAL pragma
        # touches it. (mode=ro cannot open a not-yet-existing file, and can
        # fail on a hot WAL needing recovery; both fall through to the
        # historical read-write open, which remains adjudicated in phase 2.)
        pre_report: CompatibilityReport | None = None
        if db_path != ":memory:" and Path(db_path).exists():
            try:
                ro = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
                try:
                    pre_report = inspect_compatibility(ro, expected_version=SCHEMA_VERSION)
                finally:
                    ro.close()
            except sqlite3.Error:
                pre_report = None
        if pre_report is not None and pre_report.state in UNADMITTABLE_STATES:
            raise StateCompatibilityError(pre_report)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # some filesystems (network mounts) can't do WAL; default journal is fine
        self.db.execute("PRAGMA foreign_keys=ON")
        self._admit_compatibility(pre_report)

    # -- compatibility barrier (M14 / STD-DEPLOY-COM-002) ----------------
    #
    # Construction is the barrier: normal work cannot begin on this store
    # until the persistent state has been adjudicated against the expected
    # contract. Inspection is read-only; mutation happens only after an
    # explicit compatibility decision (fresh bootstrap or canonical
    # migration), and the result is re-verified before the store is usable.
    # Every incompatible verdict raises StateCompatibilityError with the
    # full read-only evidence — the database is never deleted, stamped, or
    # silently upgraded, and unadmittable state can never be laundered into
    # "current" by opening it.

    def _admit_compatibility(self, pre_report: CompatibilityReport | None) -> None:
        if pre_report is not None:
            report = pre_report
        else:
            report = inspect_compatibility(self.db, expected_version=SCHEMA_VERSION)
        if report.state is StateCompatibility.COMPATIBLE:
            self.compatibility_report = report
            return
        failure: str | None = None
        try:
            if report.state is StateCompatibility.FRESH:
                self._bootstrap_fresh()
            elif report.state is StateCompatibility.MIGRATION_REQUIRED:
                self._migrate_incremental(report.observed_version)
            # every other state falls through to the refusal below
        except sqlite3.Error as exc:
            failure = f"{type(exc).__name__}: {exc}"
        post = self._post_admission_report(failure)
        if post.state is StateCompatibility.COMPATIBLE:
            self.compatibility_report = post
            return
        self.db.close()
        raise StateCompatibilityError(post)

    def _post_admission_report(self, failure: str | None) -> CompatibilityReport:
        """Re-verify after any mutating admission step (or after its
        failure). A bootstrap/migration that did not actually produce
        compatible state must not mark the state ready."""
        try:
            post = inspect_compatibility(self.db, expected_version=SCHEMA_VERSION)
        except sqlite3.Error as exc:
            post = CompatibilityReport(
                state=StateCompatibility.CORRUPT,
                expected_version=SCHEMA_VERSION,
                observed_version=None,
                reason=f"post-admission inspection failed: {exc}",
            )
        if failure:
            post.evidence["admission_failure"] = failure
        return post

    def _bootstrap_fresh(self) -> None:
        """Canonical fresh-state bootstrap: the current full schema, then
        the version marker stamped 1..N at once (the historical, documented
        fresh-database path)."""
        self.db.executescript(_SCHEMA)  # CREATE TABLE IF NOT EXISTS throughout
        for v in range(1, SCHEMA_VERSION + 1):
            self.db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (v,)
            )
        self.db.commit()

    def _migrate_incremental(self, from_version: int) -> None:
        """Canonical forward-only migration of known-older state, in one
        transaction so a failure mid-way leaves the state exactly as it was
        (still MIGRATION_REQUIRED, never half-stamped), followed by the
        idempotent current-schema finalize the historical migrate() always
        applied."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for v in range(from_version + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS.get(v, []):
                    self.db.execute(stmt)
                self.db.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (v,)
                )
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise
        # Now safe: every column schema.sql's CREATE TABLE/INDEX statements
        # reference has just been added by the ALTER TABLEs above.
        # Idempotent (IF NOT EXISTS throughout).
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # -- lifecycle ------------------------------------------------------

    def _maybe_commit(self) -> None:
        """Commit unless an explicit `transaction()` owns the boundary."""
        if self._tx_depth == 0:
            self.db.commit()

    @contextmanager
    def transaction(self):
        """Make several writes durable together, or not at all.

        Exists for one specific invariant: an event and the notification
        outbox row it implies must commit atomically. Before this,
        `record_event` committed on its own and the enqueue committed
        separately, so a crash between them left a committed event with no
        outbox row — and because event insertion is deduplicated by
        `dedup_key`, replaying the same collection could never repair the
        omission (the event already existed, so no notify callback fired).

        Reentrant by depth so nesting cannot commit early. Any exception
        rolls the whole block back and propagates.
        """
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            if self._tx_depth == 1:
                self.db.rollback()
            raise
        finally:
            self._tx_depth -= 1
        if self._tx_depth == 0:
            self.db.commit()

    def schema_version(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(MAX(version), 0) v FROM schema_migrations"
        ).fetchone()["v"]

    def backup_to(self, target: str | Path, *, overwrite: bool = False) -> dict:
        """SQLite-safe snapshot into `target` (DATA_SURVIVABILITY Layer A).

        Uses the SQLite online backup API (safe against the live WAL
        database — never a filesystem copy of sidecar files), writes to
        `<target>.partial` and atomically renames so a killed process can
        never leave a torn "backup". The snapshot is then verified in place
        (PRAGMA integrity_check) and identified (size + SHA-256). An
        existing target is refused unless `overwrite=True`, because a
        backup command that silently destroys the previous recovery point
        is itself a destructive operation."""
        import os

        target_path = Path(target)
        if target_path.exists() and not overwrite:
            raise FileExistsError(
                f"backup target already exists (refusing to overwrite a "
                f"recovery point without --force): {target_path}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        partial = target_path.with_name(target_path.name + ".partial")
        if partial.exists():
            partial.unlink()
        dest = sqlite3.connect(str(partial))
        try:
            with dest:
                self.db.backup(dest)
        finally:
            dest.close()
        # Verify the snapshot before it earns the name: integrity plus identity.
        check = sqlite3.connect(str(partial))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"backup failed integrity_check: {result}")
        os.replace(partial, target_path)
        data = target_path.read_bytes()
        return {
            "path": str(target_path),
            "integrity_check": result,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "schema_version": self.schema_version(),
        }

    def close(self) -> None:
        self.db.close()

    # -- sources ----------------------------------------------------------

    def ensure_source(
        self, source_key: str, manufacturer: str, source_type: str,
        region: str | None, base_url: str, config: dict,
    ) -> int:
        self.db.execute(
            "INSERT INTO sources(source_key, manufacturer, source_type, region, "
            "base_url, config_json) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source_key) DO UPDATE SET manufacturer=excluded.manufacturer, "
            "source_type=excluded.source_type, region=excluded.region, "
            "base_url=excluded.base_url, config_json=excluded.config_json",
            (source_key, manufacturer, source_type, region, base_url, json.dumps(config, default=str)),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT id FROM sources WHERE source_key=?", (source_key,)
        ).fetchone()["id"]

    # -- products / observations -------------------------------------------

    def active_product_count(self, source_key: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) c FROM products p JOIN sources s ON p.source_id=s.id "
            "WHERE s.source_key=? AND p.status='active'",
            (source_key,),
        ).fetchone()["c"]

    def incomplete_spec_products(self, source_key: str) -> list[sqlite3.Row]:
        """Products whose latest observation has spec_completeness
        'incomplete' — a real catalogue entry with no usable spec data yet
        (brief: existence/classification is separate from extraction
        success)."""
        return self.db.execute(
            "SELECT p.product_key, p.model, p.url, o.spec_completeness, o.observed_at "
            "FROM products p JOIN sources s ON p.source_id = s.id "
            "JOIN observations o ON o.id = ("
            "  SELECT id FROM observations WHERE product_id = p.id ORDER BY id DESC LIMIT 1"
            ") WHERE s.source_key = ? AND o.spec_completeness = 'incomplete'",
            (source_key,),
        ).fetchall()

    def get_product(self, product_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM products WHERE product_key=?", (product_key,)
        ).fetchone()

    def create_product(self, source_id: int, d: Discovery) -> int:
        self.db.execute(
            "INSERT INTO products(source_id, product_key, manufacturer, model, "
            "model_number, region, url) VALUES (?,?,?,?,?,?,?)",
            (source_id, d.product_key, d.manufacturer, d.model,
             d.model_number, d.region, d.url),
        )
        self.db.commit()
        return self.get_product(d.product_key)["id"]

    def touch_product(self, product_id: int, url: str) -> None:
        """Mark a product as seen again this run: refresh url/last_seen_at,
        reset its absence streak, and reactivate it if a prior run had
        marked it removed (brief section 9: "a product that reappears...
        should simply clear its suspected-removal state")."""
        self.db.execute(
            "UPDATE products SET last_seen_at=datetime('now'), status='active', "
            "url=?, consecutive_absences=0 WHERE id=?",
            (url, product_id),
        )
        self.db.commit()

    def active_products_for_source(self, source_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM products WHERE source_id=? AND status='active'", (source_id,)
        ).fetchall()

    def set_absence_count(self, product_id: int, count: int) -> None:
        self.db.execute(
            "UPDATE products SET consecutive_absences=? WHERE id=?", (count, product_id)
        )
        self.db.commit()

    def mark_removed(self, product_id: int) -> None:
        self.db.execute("UPDATE products SET status='removed' WHERE id=?", (product_id,))
        self.db.commit()

    # -- observations -------------------------------------------------------

    def latest_observation(self, product_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM observations WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    def record_observation_get_id(self, product_id: int, d: Discovery) -> tuple[int, bool]:
        """Append an observation if its content differs from any existing
        one for this product; return (observation_id, is_new). `INSERT OR
        IGNORE` (rather than a bare INSERT after a pre-check) so a value
        that reverts to an exact historical state — matching an older row's
        content_hash, not just the latest — can never raise a UNIQUE
        constraint violation; it just resolves to that existing row with
        is_new=False."""
        content_hash = d.content_hash()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO observations(product_id, content_hash, fields_json, "
            "spec_completeness, price, currency, availability) VALUES (?,?,?,?,?,?,?)",
            (product_id, content_hash, json.dumps(d.fields, default=str),
             d.spec_completeness, d.price, d.currency, d.availability),
        )
        self.db.commit()
        is_new = cur.rowcount > 0
        row = self.db.execute(
            "SELECT id FROM observations WHERE product_id=? AND content_hash=?",
            (product_id, content_hash),
        ).fetchone()
        return row["id"], is_new

    # -- events ---------------------------------------------------------

    def record_event(self, event: Event) -> int | None:
        """Insert an event; returns its id, or None if an event with the
        same dedup_key already exists (brief section 13 — idempotent by
        construction, not by a pre-check).

        Inside `transaction()` this does not commit: the event and the
        notification outbox row it implies become durable together or not at
        all (see core/pipeline.py's `_record_and_notify`).
        """
        dedup_key = event.dedup_key()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events(product_id, collector, event_type, "
            "changed_fields_json, previous_observation_id, current_observation_id, "
            "dedup_key, alert_level, confidence, detected_at, meta_json) "
            "SELECT id, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? FROM products WHERE product_key=?",
            (
                event.source_key, event.event_type.value,
                json.dumps([fc.model_dump(mode="json") for fc in event.changed_fields]),
                event.previous_observation_id, event.current_observation_id, dedup_key,
                event.alert_level.value, event.confidence.value,
                event.detected_at.isoformat(), json.dumps(event.meta, default=str),
                event.product_key,
            ),
        )
        self._maybe_commit()
        if cur.rowcount == 0:
            return None
        return self.db.execute(
            "SELECT id FROM events WHERE dedup_key=?", (dedup_key,)
        ).fetchone()["id"]

    def recent_events(
        self, source_key: str | None = None, limit: int = 50,
        min_alert_level: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if source_key:
            clauses.append("e.collector=?")
            params.append(source_key)
        if min_alert_level:
            order = {"noise": 0, "low": 1, "medium": 2, "high": 3}
            threshold = order.get(min_alert_level, 0)
            allowed = [lvl for lvl, rank in order.items() if rank >= threshold]
            clauses.append(f"e.alert_level IN ({','.join('?' * len(allowed))})")
            params.extend(allowed)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.db.execute(
            f"SELECT e.*, p.product_key, p.model, p.model_number, p.region, p.url "
            f"FROM events e JOIN products p ON p.id = e.product_id {where} "
            f"ORDER BY e.id DESC LIMIT ?",
            params,
        ).fetchall()

    # -- soak / operational reporting ---------------------------------------

    def soak_report(self, source_key: str, since_iso: str) -> dict:
        """Everything a human needs to review an unattended soak window
        (brief: 'A CLI/report command is sufficient' — no dashboard). Pure
        aggregation over already-persisted state; decides nothing."""
        runs = self.db.execute(
            "SELECT * FROM collector_runs WHERE source_key=? AND started_at>=? "
            "ORDER BY id", (source_key, since_iso),
        ).fetchall()
        run_ids = [r["id"] for r in runs]
        ok_runs = [r for r in runs if r["status"] == "ok"]
        failed_runs = [r for r in runs if r["status"] not in ("ok",)]

        failure_reasons = []
        if run_ids:
            placeholders = ",".join("?" * len(run_ids))
            failure_reasons = [
                dict(r) for r in self.db.execute(
                    f"SELECT run_id, message, occurred_at FROM run_errors "
                    f"WHERE run_id IN ({placeholders}) ORDER BY run_id", run_ids,
                ).fetchall()
            ]

        product_counts = [r["products_observed"] for r in ok_runs if r["products_observed"] is not None]

        events = self.db.execute(
            "SELECT e.* FROM events e JOIN products p ON p.id=e.product_id "
            "WHERE e.collector=? AND e.detected_at>=? ORDER BY e.id", (source_key, since_iso),
        ).fetchall()
        events_by_type: dict[str, int] = {}
        for e in events:
            events_by_type[e["event_type"]] = events_by_type.get(e["event_type"], 0) + 1

        dupe_check = self.db.execute(
            "SELECT dedup_key, COUNT(*) c FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY dedup_key HAVING c > 1"
        ).fetchall()

        suspected = self.db.execute(
            "SELECT product_key, consecutive_absences FROM products p "
            "JOIN sources s ON s.id=p.source_id "
            "WHERE s.source_key=? AND consecutive_absences > 0", (source_key,),
        ).fetchall()

        classification_counts = {
            r["classification"]: r["c"] for r in self.db.execute(
                "SELECT classification, COUNT(*) c FROM classification_log "
                "WHERE source_key=? GROUP BY classification", (source_key,),
            ).fetchall()
        }

        return {
            "source_key": source_key,
            "window_since": since_iso,
            "runs_attempted": len(runs),
            "runs_ok": len(ok_runs),
            "runs_failed_or_blocked": len(failed_runs),
            "run_statuses": [dict(r) for r in runs],
            "failure_reasons": failure_reasons,
            "product_count_min": min(product_counts) if product_counts else None,
            "product_count_max": max(product_counts) if product_counts else None,
            "product_count_last": product_counts[-1] if product_counts else None,
            "classification_counts_current": classification_counts,
            "events_by_type": events_by_type,
            "events_total": len(events),
            "suspected_removals": [dict(r) for r in suspected],
            "incomplete_spec_products": [dict(r) for r in self.incomplete_spec_products(source_key)],
            "duplicate_dedup_keys": [dict(r) for r in dupe_check],
            "active_product_count": self.active_product_count(source_key),
            "schema_version": self.schema_version(),
            "notification_counts": self.notification_counts("discord"),
            "last_successful_delivery": _last_sent_at(self.last_sent_notification("discord")),
        }

    # -- classification quarantine ---------------------------------------

    def get_classification(self, source_key: str, slug: str) -> sqlite3.Row | None:
        """The classification recorded as of BEFORE this run's update —
        callers must read this first, then call `record_classification`
        (which upserts), to detect a transition (brief section 11)."""
        return self.db.execute(
            "SELECT * FROM classification_log WHERE source_key=? AND slug=?",
            (source_key, slug),
        ).fetchone()

    def record_classification(
        self, source_key: str, slug: str, url: str, classification: str, evidence: dict,
    ) -> None:
        self.db.execute(
            "INSERT INTO classification_log(source_key, slug, url, classification, evidence_json) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(source_key, slug) DO UPDATE SET "
            "url=excluded.url, classification=excluded.classification, "
            "evidence_json=excluded.evidence_json, last_seen_at=datetime('now')",
            (source_key, slug, url, classification, json.dumps(evidence, default=str)),
        )
        self.db.commit()

    def classification_log(self, source_key: str, classification: str | None = None) -> list[sqlite3.Row]:
        if classification:
            return self.db.execute(
                "SELECT * FROM classification_log WHERE source_key=? AND classification=? "
                "ORDER BY slug", (source_key, classification),
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM classification_log WHERE source_key=? ORDER BY classification, slug",
            (source_key,),
        ).fetchall()

    # -- run telemetry --------------------------------------------------

    def run_started(
        self, source_key: str, *, provenance: str = "UNKNOWN",
        qualification_scope: str | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO collector_runs(source_key, started_at, provenance, qualification_scope) VALUES (?,?,?,?)",
            (source_key, datetime.now(timezone.utc).isoformat(), provenance, qualification_scope),
        )
        self.db.commit()
        return cur.lastrowid

    def run_finished(
        self, run_id: int, status: str, stats: dict, errors: list[str] | None = None,
        products_observed: int | None = None, previous_products_observed: int | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE collector_runs SET finished_at=?, status=?, stats_json=?, "
            "products_observed=?, previous_products_observed=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), status, json.dumps(stats, default=str),
             products_observed, previous_products_observed, run_id),
        )
        for err in errors or []:
            self.db.execute(
                "INSERT INTO run_errors(run_id, message) VALUES (?,?)", (run_id, err)
            )
        self.db.commit()

    def last_ok_run(self, source_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM collector_runs WHERE source_key=? AND status='ok' "
            "ORDER BY id DESC LIMIT 1",
            (source_key,),
        ).fetchone()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- notifications / delivery outbox (Stage 4) -----------------------
    #
    # The `notifications` table (schema.sql) predates this stage by design
    # (brief section 5/14/15) — nothing here required a migration. `status`
    # keeps this project's existing convention (pending | sent | failed |
    # suppressed) rather than introducing a parallel vocabulary:
    #   pending    -> queued, will be attempted (or retried) on next drain
    #   sent       -> delivered ("delivered" in brief terms)
    #   failed     -> terminal after MAX_ATTEMPTS (providers/discord)
    #   suppressed -> eligible-events policy decided not to notify (retained)
    # A transient failure simply stays `pending` (retry-safe by construction,
    # no separate failed_retryable state needed) until MAX_ATTEMPTS is hit.

    def notification_put(
        self, provider: str, dedup_key: str, payload: dict,
        event_id: int | None, status: str,
    ) -> None:
        """INSERT OR IGNORE on the UNIQUE dedup_key: re-enqueuing the same
        event (a rerun, a restarted process, a retried scheduler tick)
        never creates a second notification row (brief section 6:
        deduplication)."""
        self.db.execute(
            "INSERT OR IGNORE INTO notifications(event_id, provider, dedup_key, "
            "payload_json, status) VALUES (?,?,?,?,?)",
            (event_id, provider, dedup_key, json.dumps(payload, default=str), status),
        )
        self._maybe_commit()

    def notification_by_dedup_key(self, dedup_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM notifications WHERE dedup_key=?", (dedup_key,)
        ).fetchone()

    def pending_notifications(self, provider: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM notifications WHERE provider=? AND status='pending' "
            "ORDER BY id", (provider,),
        ).fetchall()

    def pending_notifications_with_event_time(self, provider: str) -> list[sqlite3.Row]:
        """Pending rows plus the `detected_at` of the event each one is for.

        LEFT JOIN on purpose: a row whose event is missing must still be
        returned, carrying `event_detected_at IS NULL`, so the activation
        policy can hold it explicitly rather than the query silently
        dropping it from view.
        """
        return self.db.execute(
            "SELECT n.*, e.detected_at AS event_detected_at, e.event_type AS event_type, "
            "e.collector AS event_collector "
            "FROM notifications n LEFT JOIN events e ON e.id = n.event_id "
            "WHERE n.provider=? AND n.status='pending' ORDER BY n.id",
            (provider,),
        ).fetchall()

    def defer_notification(self, notification_id: int, not_before_iso: str) -> None:
        """Set a durable retry floor without touching status or attempts.

        Used for HTTP 429: the row stays `pending` and un-penalised, but no
        drain (this process or any later one) will pick it up until the
        rate-limit window has passed.
        """
        self.db.execute(
            "UPDATE notifications SET not_before=? WHERE id=?",
            (not_before_iso, notification_id),
        )
        self._maybe_commit()

    def clear_notification_defer(self, notification_id: int) -> None:
        self.db.execute("UPDATE notifications SET not_before=NULL WHERE id=?", (notification_id,))
        self._maybe_commit()

    # -- delivery policy (durable activation) ---------------------------

    def policy_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM delivery_policy WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def policy_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO delivery_policy(key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )
        self._maybe_commit()

    def policy_all(self) -> dict[str, str]:
        return {
            r["key"]: r["value"]
            for r in self.db.execute("SELECT key, value FROM delivery_policy").fetchall()
        }

    def delivery_preview(self, provider: str = "discord") -> dict:
        """Read-only delivery inventory. Mutates nothing, sends nothing.

        Returns the raw material an operator needs before enabling delivery
        for the first time: how much is queued, how old it is, how much has
        already been attempted, and which rows are structurally odd (no
        event row, or no usable event timestamp).
        """
        counts = {
            r["status"]: r["c"]
            for r in self.db.execute(
                "SELECT status, COUNT(*) c FROM notifications WHERE provider=? GROUP BY status",
                (provider,),
            ).fetchall()
        }
        by_type = [
            dict(r)
            for r in self.db.execute(
                "SELECT COALESCE(e.event_type,'(no event row)') AS event_type, "
                "COALESCE(e.collector,'(no event row)') AS source, n.status, COUNT(*) AS count "
                "FROM notifications n LEFT JOIN events e ON e.id = n.event_id "
                "WHERE n.provider=? GROUP BY event_type, source, n.status "
                "ORDER BY count DESC, event_type",
                (provider,),
            ).fetchall()
        ]
        span = self.db.execute(
            "SELECT MIN(e.detected_at) AS oldest, MAX(e.detected_at) AS newest "
            "FROM notifications n JOIN events e ON e.id = n.event_id "
            "WHERE n.provider=? AND n.status='pending'",
            (provider,),
        ).fetchone()
        attempted = self.db.execute(
            "SELECT SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END) AS attempted, "
            "SUM(CASE WHEN attempts = 0 THEN 1 ELSE 0 END) AS never_attempted "
            "FROM notifications WHERE provider=? AND status='pending'",
            (provider,),
        ).fetchone()
        missing = self.db.execute(
            "SELECT "
            "SUM(CASE WHEN n.event_id IS NULL THEN 1 ELSE 0 END) AS null_event_id, "
            "SUM(CASE WHEN n.event_id IS NOT NULL AND e.id IS NULL THEN 1 ELSE 0 END) AS orphaned_event, "
            "SUM(CASE WHEN e.id IS NOT NULL AND (e.detected_at IS NULL OR e.detected_at='') THEN 1 ELSE 0 END) "
            "  AS missing_detected_at "
            "FROM notifications n LEFT JOIN events e ON e.id = n.event_id "
            "WHERE n.provider=? AND n.status='pending'",
            (provider,),
        ).fetchone()
        return {
            "provider": provider,
            "counts_by_status": counts,
            "pending_by_source_and_type": by_type,
            "pending_event_time_span": {"oldest": span["oldest"], "newest": span["newest"]},
            "pending_attempts": {
                "already_attempted": attempted["attempted"] or 0,
                "never_attempted": attempted["never_attempted"] or 0,
            },
            "pending_provenance_gaps": {
                "null_event_id": missing["null_event_id"] or 0,
                "orphaned_event_id": missing["orphaned_event"] or 0,
                "missing_detected_at": missing["missing_detected_at"] or 0,
            },
        }

    def mark_notification(self, notification_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE notifications SET status=?, attempts=attempts+1, last_error=?, "
            "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END WHERE id=?",
            (status, error, status, notification_id),
        )
        self.db.commit()

    def notification_counts(self, provider: str | None = None) -> dict[str, int]:
        clause, params = ("WHERE provider=?", (provider,)) if provider else ("", ())
        return {
            r["status"]: r["c"] for r in self.db.execute(
                f"SELECT status, COUNT(*) c FROM notifications {clause} GROUP BY status", params,
            ).fetchall()
        }

    def notifications_by_status(self, status: str, provider: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        clauses, params = ["status=?"], [status]
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        params.append(limit)
        return self.db.execute(
            f"SELECT * FROM notifications WHERE {' AND '.join(clauses)} "
            f"ORDER BY id DESC LIMIT ?", params,
        ).fetchall()

    def last_sent_notification(self, provider: str | None = None) -> sqlite3.Row | None:
        clause, params = ("WHERE provider=? AND status='sent'", (provider,)) if provider else ("WHERE status='sent'", ())
        return self.db.execute(
            f"SELECT * FROM notifications {clause} ORDER BY sent_at DESC LIMIT 1", params,
        ).fetchone()

    def requeue_failed_notifications(self, provider: str | None = None) -> int:
        """Operator-triggered retry (brief section 10: 'can failed delivery
        retry safely?'). Moves terminally-`failed` rows back to `pending`
        without resetting `attempts` — the history of prior failures is
        preserved, not erased."""
        clause, params = ("WHERE provider=? AND status='failed'", (provider,)) if provider else ("WHERE status='failed'", ())
        cur = self.db.execute(f"UPDATE notifications SET status='pending' {clause}", params)
        self.db.commit()
        return cur.rowcount
