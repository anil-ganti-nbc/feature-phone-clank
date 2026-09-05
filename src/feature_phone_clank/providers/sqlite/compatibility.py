"""Persistent-state compatibility inspection for the Feature Phone store.

M14 / STD-DEPLOY-COM-002. The invariant this module implements is NOT "the
project has migrations" — it is that normal work must not begin against
persistent state until the running software has established that the state
is compatible. Compatibility must fail closed when state is unknown,
corrupt, partially migrated, or newer than this software can safely
understand. Migration and compatibility are separate concepts: inspection
is read-only and precedes any mutation; only an explicit compatibility
decision admits a database.

The version authority is the durable `schema_migrations` marker (the same
authority `SqliteStore` has always used). What changed in M14 is that the
marker is now *inspected and adjudicated* before anything mutates, instead
of construction itself being the migration:

- FRESH is earned, never assumed: a database with no `schema_migrations`
  authority but with any other user table is UNKNOWN (pre-marker or
  damaged state), not fresh. Only a database with zero user tables is
  fresh. An unknown database is preserved for diagnosis — never deleted,
  never stamped as current.
- Newer state fails closed: a database whose marker reports a version
  greater than this software's expected version is INCOMPATIBLE_NEWER.
  Additive schema changes are NOT assumed backward-compatible; this
  project's skew contract is FORWARD_ONLY_EXPLICIT (state may only move
  forward through this software's own canonical migrations; no downgrade
  path exists or is implied).
- Table existence is not compatibility proof: a marker claiming the
  expected version must be corroborated by the presence of every table the
  current schema defines, else the state is PARTIAL.
- Corruption fails closed: a file that is not a database, or fails
  quick_check, is CORRUPT.

The seven states below are the contract; inspection never mutates the
database it examines.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum

# The expected persistent-state contract of THIS software version. Single
# source of truth; `providers.sqlite` re-exports this as SCHEMA_VERSION so
# the schema, the migrations, and the compatibility gate cannot drift apart.
EXPECTED_SCHEMA_VERSION = 6

# Every table the current schema (schema.sql + applied migrations at this
# version) must have left behind. Used to corroborate the marker: a marker
# claiming the expected version with any of these missing is PARTIAL.
EXPECTED_TABLES = frozenset({
    "schema_migrations",
    "sources",
    "products",
    "observations",
    "events",
    "collector_runs",
    "run_errors",
    "notifications",
    "delivery_policy",
    "classification_log",
    "qualification_state",
    "qualification_epochs",
    "qualification_events",
})


class StateCompatibility(str, Enum):
    """Compatibility verdicts. UNKNOWN != COMPATIBLE; FRESH != UNKNOWN;
    DB_OPENED != COMPATIBLE."""

    FRESH = "FRESH"
    COMPATIBLE = "COMPATIBLE"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    INCOMPATIBLE_NEWER = "INCOMPATIBLE_NEWER"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    CORRUPT = "CORRUPT"


# States that can never be admitted to normal work, and that mark health
# as degraded when observed read-only. (MIGRATION_REQUIRED is not in this
# set: it is valid older state that the canonical migration admits; FRESH
# bootstraps.)
UNADMITTABLE_STATES = frozenset({
    StateCompatibility.INCOMPATIBLE_NEWER,
    StateCompatibility.UNKNOWN,
    StateCompatibility.PARTIAL,
    StateCompatibility.CORRUPT,
})


@dataclass(frozen=True)
class CompatibilityReport:
    """Read-only verdict on one persistent store, with the evidence that
    produced it. `as_evidence()` is the machine-readable refusal record."""

    state: StateCompatibility
    expected_version: int
    observed_version: int | None
    reason: str
    evidence: dict = field(default_factory=dict)

    def as_evidence(self) -> dict:
        return {
            "compatibility_state": self.state.value,
            "expected_schema_version": self.expected_version,
            "observed_schema_version": self.observed_version,
            "reason": self.reason,
            **self.evidence,
        }

    def __str__(self) -> str:  # human-facing one-liner for logs/messages
        return (
            f"{self.state.value}: {self.reason} "
            f"(expected schema v{self.expected_version}, "
            f"observed {'none' if self.observed_version is None else f'v{self.observed_version}'})"
        )


class StateCompatibilityError(RuntimeError):
    """Raised when a store is refused because its persistent state is not
    (or not yet) compatible with this software. `.report` carries the full
    read-only evidence; nothing was mutated by the refusal."""

    def __init__(self, report: CompatibilityReport) -> None:
        super().__init__(
            "persistent-state compatibility refused: "
            f"{report} — normal work was not admitted; the database was "
            f"left untouched for diagnosis"
        )
        self.report = report


def _report(
    state: StateCompatibility,
    expected_version: int,
    observed_version: int | None,
    reason: str,
    **evidence,
) -> CompatibilityReport:
    return CompatibilityReport(
        state=state,
        expected_version=expected_version,
        observed_version=observed_version,
        reason=reason,
        evidence=evidence,
    )


def _marker_version_values(con: sqlite3.Connection) -> list | None:
    """Raw `version` column values, or None when the marker table does not
    have the expected shape."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(schema_migrations)")}
    if "version" not in columns:
        return None
    return [row[0] for row in con.execute("SELECT version FROM schema_migrations").fetchall()]


def _coerce_versions(raw: list) -> list[int] | None:
    """Parse marker rows into positive ints; None if any row is not a
    usable version (corrupt/contradictory authority)."""
    values: list[int] = []
    for value in raw:
        if isinstance(value, int):
            values.append(value)
        elif isinstance(value, str):
            try:
                values.append(int(value))
            except ValueError:
                return None
        else:
            return None
    return values


def inspect_compatibility(
    con: sqlite3.Connection, *, expected_version: int = EXPECTED_SCHEMA_VERSION
) -> CompatibilityReport:
    """Adjudicate one open SQLite connection's persistent state against the
    expected contract. Strictly read-only: PRAGMA quick_check, sqlite_master
    reads, and table_info reads never mutate the database."""
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            return _report(
                StateCompatibility.CORRUPT, expected_version, None,
                f"quick_check reported {quick!r}", quick_check=str(quick),
            )
        tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.DatabaseError as exc:
        return _report(
            StateCompatibility.CORRUPT, expected_version, None,
            f"not a usable SQLite database: {exc}", sqlite_error=str(exc),
        )

    if not tables:
        return _report(
            StateCompatibility.FRESH, expected_version, None,
            "no persistent state yet (zero user tables); canonical bootstrap "
            "may create it",
            user_tables=[],
        )

    if "schema_migrations" not in tables:
        return _report(
            StateCompatibility.UNKNOWN, expected_version, None,
            f"existing database has {len(tables)} table(s) but no "
            f"schema_migrations authority; it is not fresh and must not be "
            f"bootstrapped or stamped",
            user_tables=sorted(tables),
        )

    raw = _marker_version_values(con)
    if raw is None:
        return _report(
            StateCompatibility.UNKNOWN, expected_version, None,
            "schema_migrations table exists but lacks the expected "
            "'version' column; the version authority is unreadable",
            user_tables=sorted(tables),
        )
    versions = _coerce_versions(raw)
    if versions is None:
        return _report(
            StateCompatibility.UNKNOWN, expected_version, None,
            f"schema_migrations contains non-integer version data "
            f"({raw!r}); the version authority is corrupt",
            user_tables=sorted(tables),
        )
    if not versions:
        return _report(
            StateCompatibility.UNKNOWN, expected_version, 0,
            "schema_migrations exists but has never recorded any version; "
            "state is neither fresh nor versioned",
            user_tables=sorted(tables),
        )

    observed = max(versions)
    if observed > expected_version:
        return _report(
            StateCompatibility.INCOMPATIBLE_NEWER, expected_version, observed,
            f"persistent state is newer (v{observed}) than this software "
            f"understands (v{expected_version}); the skew contract is "
            f"FORWARD_ONLY_EXPLICIT and older software must not open it",
            user_tables=sorted(tables),
        )
    if observed < expected_version:
        return _report(
            StateCompatibility.MIGRATION_REQUIRED, expected_version, observed,
            f"older valid state (v{observed}) must migrate through the "
            f"canonical mechanism to v{expected_version} before normal work",
            user_tables=sorted(tables),
        )

    missing = sorted(EXPECTED_TABLES - tables)
    if missing:
        return _report(
            StateCompatibility.PARTIAL, expected_version, observed,
            f"marker records v{observed} but {len(missing)} expected table(s) "
            f"are missing ({', '.join(missing)}); migration is incomplete or "
            f"the state is contradictory",
            missing_tables=missing,
            user_tables=sorted(tables),
        )

    return _report(
        StateCompatibility.COMPATIBLE, expected_version, observed,
        f"state matches the expected v{expected_version} contract "
        f"(all {len(EXPECTED_TABLES)} expected tables present)",
        user_tables=sorted(tables),
    )
