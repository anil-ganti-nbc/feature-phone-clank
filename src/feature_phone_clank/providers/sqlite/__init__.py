"""SQLite provider: schema lifecycle + product/observation/event
persistence + run telemetry.

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
from datetime import datetime, timezone
from pathlib import Path

from ...core.models import Discovery, Event

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

SCHEMA_VERSION = 4
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
}


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open read-only; never migrates, never locks. Safe to call against a
    live database (health checks, status queries)."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


class SqliteStore:
    def __init__(self, db_path: str = "data/feature_phone_clank.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # some filesystems (network mounts) can't do WAL; default journal is fine
        self.db.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # -- lifecycle ------------------------------------------------------

    def migrate(self) -> None:
        """Idempotent: safe to call on every startup, fresh DB or existing.

        For an existing database, incremental `_MIGRATIONS` (plain ALTER
        TABLE statements) must run BEFORE `executescript(_SCHEMA)` — not
        after. `schema.sql` is written as the current version's full,
        final shape (e.g. `CREATE UNIQUE INDEX ... ON events(dedup_key)`),
        so running it against an older table that doesn't have that column
        yet fails outright. Only a genuinely fresh database can safely take
        `_SCHEMA` first, because it creates every table at the current
        version directly."""
        fresh = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is None
        if fresh:
            self.db.executescript(_SCHEMA)  # CREATE TABLE IF NOT EXISTS throughout
            for v in range(1, SCHEMA_VERSION + 1):
                self.db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (v,)
                )
        else:
            current = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) v FROM schema_migrations"
            ).fetchone()["v"]
            for v in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS.get(v, []):
                    self.db.execute(stmt)
                self.db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (v,))
            # Now safe: every column schema.sql's CREATE TABLE/INDEX
            # statements reference has just been added by the ALTER TABLEs
            # above. Idempotent (IF NOT EXISTS throughout) — also picks up
            # any brand-new table a future version adds without its own
            # migration entry.
            self.db.executescript(_SCHEMA)
        self.db.commit()

    def schema_version(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(MAX(version), 0) v FROM schema_migrations"
        ).fetchone()["v"]

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
        construction, not by a pre-check)."""
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
        self.db.commit()
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

    def run_started(self, source_key: str) -> int:
        cur = self.db.execute(
            "INSERT INTO collector_runs(source_key, started_at) VALUES (?,?)",
            (source_key, datetime.now(timezone.utc).isoformat()),
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
