"""QC / archive store: human editorial review decisions on events.

Feature Phone Clank's equivalent of Watch Clank's EventReview contract (see
watch-clank/app/services/qc.py and
watch-clank/ai/handoff/HUMAN_QC_FEEDBACK_CONTRACT.md for the reference
design this ports, adapted to this project's plain-sqlite stack instead of
SQLAlchemy). A QC decision is human feedback about ONE event under one
evidence state -- never a mutation of the event itself, never a permanent
blacklist of the underlying product. EVENT != REVIEW.

Deliberately a wholly separate SQLite file
(data/feature_phone_clank_qc.db, sibling to the production db, resolved via
paths.resolve_data_path) rather than a new table inside
feature_phone_clank.db:

  - no schema migration of the live production database is required to
    ship this at all (the fleet DB-safety rule is "migrate only if truly
    needed, backup first if you do" -- here that need never arises);
  - archived review history survives independently of anything that ever
    happens to the production db file.

Contract (mirrors Watch Clank's):
  - archiving a decision is transactional: the full item snapshot +
    provenance + decision land in one write, or none of it does;
  - idempotent-by-correction, not error-by-duplicate: a second decision for
    the same event_id never creates a second row (UNIQUE(event_id) is the
    actual duplicate-protection mechanism, checked inside a single
    BEGIN IMMEDIATE transaction so two racing submissions for the same
    event can never both "win") -- it corrects the existing row in place
    and appends the prior verdict to review_metadata.correction_history,
    so a correction stays auditable without a second table;
  - nothing here ever deletes or mutates the original event/product/
    observation rows in the production database -- this module only reads
    them (via the caller-supplied snapshot dict) and writes its own file.

Vocabulary matches Watch Clank's DISPOSITIONS minus DUPLICATE: Feature
Phone Clank's event model has no cross-collector duplicate concept yet
(docs/FEATURE_PHONE_SCOPE_EXPANSION.md section 8: "nothing merges across
source_key"), so a DUPLICATE disposition would have nothing to mean here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DECISIONS = frozenset({"USEFUL", "NOT_USEFUL", "FALSE_POSITIVE", "OUT_OF_STOCK"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_reviews (
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
CREATE INDEX IF NOT EXISTS idx_qc_reviews_decided_at ON qc_reviews(decided_at);
CREATE INDEX IF NOT EXISTS idx_qc_reviews_source ON qc_reviews(source_key, decided_at);
"""


class InvalidDecisionError(ValueError):
    """Raised when a decision outside DECISIONS is submitted."""


class QcArchiveStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- queries ----------------------------------------------------------

    def reviewed_event_ids(self) -> set[int]:
        """Event ids already QC'd -- callers use this to remove reviewed
        items from the default/active queue immediately, without deleting
        anything from the production database."""
        return {r["event_id"] for r in self.db.execute("SELECT event_id FROM qc_reviews").fetchall()}

    def review_for_event(self, event_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM qc_reviews WHERE event_id=?", (event_id,)
        ).fetchone()

    def recent_reviews(self, limit: int = 50) -> list[sqlite3.Row]:
        """Newest-first, for the 'Recently QCed' view -- full provenance
        already denormalized onto each row, so this never needs to reach
        back into the production database."""
        return self.db.execute(
            "SELECT * FROM qc_reviews ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def counts_by_decision(self) -> dict[str, int]:
        return {
            r["decision"]: r["c"] for r in self.db.execute(
                "SELECT decision, COUNT(*) c FROM qc_reviews GROUP BY decision"
            ).fetchall()
        }

    # -- writes -------------------------------------------------------------

    def submit_review(
        self,
        *,
        event_id: int,
        source_key: str,
        event_type: str,
        decision: str,
        product_key: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        model_number: str | None = None,
        url: str | None = None,
        changed_fields: list | None = None,
        meta: dict | None = None,
        detected_at: str | None = None,
        run_id: int | None = None,
        run_started_at: str | None = None,
        reason: str | None = None,
    ) -> dict:
        if decision not in DECISIONS:
            raise InvalidDecisionError(f"unknown QC decision: {decision!r}")

        now = datetime.now(timezone.utc).isoformat()
        # BEGIN IMMEDIATE takes the write lock up front -- a second
        # concurrent submit_review() for the same event_id blocks here
        # (sqlite serializes it) rather than racing the SELECT-then-INSERT
        # below, which is what actually makes "no double-QC" a guarantee
        # instead of a best effort.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM qc_reviews WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                metadata = json.loads(existing["review_metadata_json"] or "{}")
                is_corrected = existing["is_corrected"]
                if existing["decision"] != decision:
                    history = list(metadata.get("correction_history") or [])
                    history.append({
                        "previous_decision": existing["decision"],
                        "previous_decided_at": existing["decided_at"],
                        "corrected_at": now,
                    })
                    metadata["correction_history"] = history
                    is_corrected = 1
                self.db.execute(
                    "UPDATE qc_reviews SET decision=?, reason=COALESCE(?, reason), "
                    "is_corrected=?, review_metadata_json=? WHERE event_id=?",
                    (decision, reason, is_corrected, json.dumps(metadata, default=str), event_id),
                )
                self.db.commit()
                return dict(self.db.execute(
                    "SELECT * FROM qc_reviews WHERE event_id=?", (event_id,)
                ).fetchone())

            self.db.execute(
                "INSERT INTO qc_reviews(event_id, source_key, product_key, manufacturer, "
                "model, model_number, url, event_type, changed_fields_json, meta_json, "
                "detected_at, run_id, run_started_at, decision, reason, decided_at, "
                "is_corrected, review_metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'{}')",
                (
                    event_id, source_key, product_key, manufacturer, model, model_number, url,
                    event_type, json.dumps(changed_fields or [], default=str),
                    json.dumps(meta or {}, default=str), detected_at, run_id, run_started_at,
                    decision, reason, now,
                ),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return dict(self.db.execute(
            "SELECT * FROM qc_reviews WHERE event_id=?", (event_id,)
        ).fetchone())
