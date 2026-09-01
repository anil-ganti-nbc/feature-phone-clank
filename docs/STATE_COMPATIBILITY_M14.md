# M14 — Persistent-State Compatibility Barrier (STD-DEPLOY-COM-002)

Status: implemented on top of canonical baseline `4b7dce2` (main).
Controlling standard: `standards-clank/standards/deployment/STD-DEPLOY-COM-002.json`
(RATIFIED v1). Controlling mission: FEATURE PHONE M14.

## The invariant

Normal work must not begin against persistent state until the running
software has established that the state is compatible. Compatibility is
adjudicated read-only, **before** anything mutates, and fails closed when
state is unknown, corrupt, partial, or newer than this software understands.
Migration and compatibility are separate concepts: migration is something a
compatible *decision* may cause; it is never a side effect of opening the
database.

## Defect being remediated (at baseline `4b7dce2`)

`SqliteStore.__init__` called `migrate()` unconditionally:

- **Auto-migration as admission.** Constructing a store silently upgraded
  any existing database in place. There was no compatibility decision and
  no way to refuse.
- **Freshness was assumed, not earned.** "Fresh" meant only "no
  `schema_migrations` table". A database with real data tables but a
  missing/corrupt marker was classified fresh, bootstrapped over, and
  stamped 1..N — unknown state laundered into "current".
- **Newer state was silently accepted.** A v6+ database opened by v5 code
  ran zero migrations and proceeded to normal work.
- **Table existence acted as compatibility proof.** The idempotent
  `executescript` on every open meant "tables exist" and "state is
  compatible" were indistinguishable.
- **Failure semantics were raw sqlite errors**, not compatibility evidence.

## The mechanism (Feature Phone-native, smallest honest shape)

New module `src/feature_phone_clank/providers/sqlite/compatibility.py`:

- `EXPECTED_SCHEMA_VERSION = 5` — the single source of truth for this
  software's persistent-state contract; `providers.sqlite` re-exports it as
  `SCHEMA_VERSION` so schema, migrations, and gate cannot drift apart.
- `StateCompatibility` — `FRESH`, `COMPATIBLE`, `MIGRATION_REQUIRED`,
  `INCOMPATIBLE_NEWER`, `UNKNOWN`, `PARTIAL`, `CORRUPT`.
  Semantics pinned by test: `UNKNOWN != COMPATIBLE`, `FRESH != UNKNOWN`,
  `DB_OPENED != COMPATIBLE`.
- `inspect_compatibility(con)` — **strictly read-only** (quick_check,
  sqlite_master, table_info). Adjudication rules, in order:
  1. quick_check fails / not a SQLite file → `CORRUPT`.
  2. Zero user tables → `FRESH` (canonical bootstrap may create it).
  3. Tables but no `schema_migrations` → `UNKNOWN` (not fresh; never
     bootstrapped, never stamped).
  4. Marker present but unreadable/contradictory (missing `version`
     column, non-integer rows, never stamped) → `UNKNOWN`.
  5. `max(version) > expected` → `INCOMPATIBLE_NEWER` (the skew contract is
     **FORWARD_ONLY_EXPLICIT**: state only moves forward through this
     software's own canonical migrations; no downgrade path exists, is
     implied, or is invented).
  6. `max(version) < expected` → `MIGRATION_REQUIRED` (valid older state;
     admitted only through the canonical migration).
  7. Marker at expected version but any of the 12 expected tables missing
     → `PARTIAL` (table existence is not compatibility proof).
  8. Otherwise → `COMPATIBLE`.

`SqliteStore.__init__` is the barrier, in two phases:

1. **Read-only inspection on a `mode=ro` handle** (existing files only). A
   refusal raises `StateCompatibilityError(report)` before any read-write
   handle exists — refused files are left **byte-identical** (proved by
   test, including against the WAL pragma).
2. **Admission on the read-write handle**, only for `FRESH` (canonical
   `schema.sql` bootstrap + version stamp), `MIGRATION_REQUIRED` (canonical
   `_MIGRATIONS` in one `BEGIN IMMEDIATE` transaction, then the idempotent
   current-schema finalize), or `COMPATIBLE` (zero writes). Bootstrap and
   migration are both **re-verified by a fresh inspection before the store
   is handed to the caller**; anything else raises with evidence including
   `admission_failure`. A failed migration rolls back and cannot advance
   the version marker; retry crosses inspection again.

`QcArchiveStore` (`feature_phone_clank_qc.db`) gets its own narrower gate:
it has exactly one schema shape and no version history, so its contract is
"exact known shape", checked read-only against `table_info`. Fresh files
bootstrap canonically; archives written before M14 match the expected
column set and are grandfathered compatible; a different shape, foreign
tables, or corruption refuses with evidence. Inspection happens before the
WAL pragma so a non-database file is refused cleanly.

## Refusal evidence (the reason is the record)

- **CLI**: every store-touching command opens via
  `_open_state_compatible_store`; refusal prints
  `{"status": "state_incompatible", "gate": "persistent_state_compatibility",
  ...full evidence}` and exits **3** (0 ok / 1 operational failure /
  2 lock held / 3 state refused).
- **Dashboard**: `GET /` returns **503** with a refusal page carrying the
  evidence table; `POST /api/qc/review/<id>` returns **503** JSON with the
  same evidence.
- **LocalCollectionController**: worker threads record
  `state="blocked"`, message `persistent-state compatibility refused: …`,
  and the full evidence dict as the run `result`.
- **Health** (`runtime_bridge`): the read-only probe appends
  `persistent_state: <STATE> (<reason>)` to `status_reasons` and reports
  `degraded` for unadmittable states. (`FRESH`/`MIGRATION_REQUIRED` are not
  degraded — the store admits them through bootstrap/migration.)

## Persistent-state inventory (M14, this machine)

| Store | Authority | State at takeover | Admission |
|---|---|---|---|
| `data/feature_phone_clank.db` | `schema_migrations` marker, expected v5 | **v4** (versions 1–4, quick_check ok, no qualification tables — untouched by v5-capable code since M7) | `MIGRATION_REQUIRED` → canonical migration 5 → reverified |
| `data/feature_phone_clank_experimental.db` | same contract | absent here | same barrier via `run-experimental` |
| `data/feature_phone_clank_qc.db` | exact single shape (19-column `qc_reviews`) | matches expected shape, quick_check ok | grandfathers `COMPATIBLE` |
| `data/continuity/continuity-events.jsonl` | none (schema-less append-only JSONL, hash-verified) | absent here | trigger-unmet: no evolving schema/compatibility boundary |

No production database was modified during this mission (the inventory and
all mission testing used `mode=ro` handles or copies; the production file's
v4 state is preserved exactly as found).

## Entry-point classification (§8/§13)

| Path | Classification |
|---|---|
| `cli.cmd_run` (scheduler + manual production path) | NORMAL_GUARDED — store construction is the first DB touch; refuses exit 3 |
| `cli.cmd_run_experimental` (manual/experimental) | NORMAL_GUARDED — same barrier on the experimental db |
| `cli.cmd_status` / `cmd_events` / `cmd_notifications` / `cmd_report` / `cmd_deliver` / `cmd_test_notify` | NORMAL_GUARDED — read-path commands refuse rather than silently upgrade |
| `cli.cmd_backup` | NORMAL_GUARDED — refuses on incompatible state (copy the file at the filesystem level for diagnosis) |
| `dashboard.render` / `GET /` | NORMAL_GUARDED — 503 refusal page |
| `dashboard` QC POST | NORMAL_GUARDED — 503 evidence JSON |
| `LocalCollectionController._run_one` / `_run_all_production` | NORMAL_GUARDED — blocked state + evidence |
| `runtime_bridge._probe_sqlite` / `get_health` | READ_ONLY_INSPECTION — never mutates; reports compatibility |
| `providers.sqlite.connect_readonly` | READ_ONLY_INSPECTION — never migrates, never locks (pre-existing contract, unchanged) |
| `backup_to` internal connects | BOOTSTRAP_ONLY (writes to a fresh `.partial` snapshot, never the source) |
| `core.runner.run_experimental` docstring `SqliteStore(":memory:")` | TEST/THROWAWAY — `:memory:` is always fresh, bootstrapped through the same gate |
| `live_probe_wave2.py` `SqliteStore(":memory:")` | TEST/THROWAWAY |
| `scripts/*`, `native/*/launcher.py` | external wrappers around the CLI/controller — guarded transitively |
| tests/ | TEST_ONLY (disposable tmp databases) |

No unexplained `NORMAL_BYPASS` exists.

## Orthogonality (§9)

Compatibility is a third, independent authority:

- **Lock ≠ compatibility.** A valid `RunLock` kernel grant admits the
  *process*, never the *state*: an incompatible database refuses even while
  the run lock is held (test: `test_run_lock_ownership_cannot_admit_incompatible_state`).
  Ordering in `cmd_run` (lock → store construction) is safe because the
  barrier still raises inside the lock; the reverse ordering is equally
  safe because inspection is read-only.
- **Qualification ≠ compatibility.** Qualification evidence lives *inside*
  the store; it is unreachable until the store is admitted. A qualified
  source cannot open incompatible state (test:
  `test_controller_run_blocked_by_barrier_with_evidence`), and the M7
  qualification lifecycle works unchanged on admitted migrated state
  (test: `test_qualification_evidence_flow_works_after_admission`).

## Validation (this host, Windows, Python 3.14)

- Pre-change full suite at `4b7dce2`: **218 passed, 1 skipped, 4 failed**.
  The 4 failures are the documented baseline family: shared
  `clank_runtime` `HealthPayload` Pydantic contract drift (missing
  `clank_id`, extra-forbidden fields) in 3 tests, and the Windows
  Python 3.14 subprocess-handle flake (`OSError: [WinError 6]`) in the
  CLI-subprocess health test.
- Post-change: **252 passed, 1 skipped, 4 failed** — the identical four
  tests, attributed to the identical pre-existing causes. 218 + 34 new
  M14 tests = 252.
- Focused: `test_state_compatibility_m14.py` 34/34;
  `test_qualification_m7.py` (OPS-COM-003) and `test_run_lock.py`
  (OPS-COM-004) green; scope-isolation and dashboard-operation suites green.

## Skew contract

`FORWARD_ONLY_EXPLICIT`: this software opens state at or before its own
expected version (older only via canonical migration) and refuses newer
state rather than guessing at additive-compatibility. No downgrade
guarantees exist or are claimed.
