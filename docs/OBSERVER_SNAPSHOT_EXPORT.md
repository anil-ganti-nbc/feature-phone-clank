# Feature Phone-owned observer export — COPS-000089

Candidate only until exact Linux/NAS proof passes. COPS-000081 stays PAUSED.
This does not change collectors, delivery, schema, tasks 10/11 or Motherclank
task 14. Motherclank's separate e93195e diagnostics patch is not included.

## Command and source admission

```
feature-phone-clank --db /app/data/feature_phone_clank.db observer-snapshot \
  --output /export --timeout-seconds 60
```

The child-only container gets canonical state at `/app/data:rw` solely for
SQLite sidecar coordination and the existing collection lock. Its connection
is `mode=ro`, `query_only`, read-authorizer allowlisted, existing-compatible
schema only. It never constructs SqliteStore, bootstraps, migrates, seeds
continuity, checkpoints, converts the source journal, collects or sends.
The exporter and ordinary recovery `backup_to()` share
`online_backup_to_partial()`. Only the destination is normalized to DELETE.

Canonical collection lock: `/app/data/feature-phone-clank.lock`, OS-backed
nonblocking grant. No `--no-lock` or alternative lock option. Exit 2 means
collection contention, not success. SQLite online backup remains consistent
with concurrent delivery/store writers; no all-writer quiescence is required.
Backup progress and SQL progress handlers bound work; non-OK backup status is
not retried. The NAS wrapper must also impose an outer process timeout.

Exit codes: 0 private export verified (NOT published), 2 lock busy, 3 schema
refused, 4 timeout, 1 other bounded failure. JSON failure stages distinguish
source admission, lock, backup, integrity, FK, schema, metadata, publication,
timeout and other. Only numeric SQLite code/allowlisted name cross the boundary;
never raw exceptions, rows, SQL, URLs or webhook values.

## Wire contract v1.0

Private set: `feature_phone_clank.db` + `metadata.json`. Neither is an admitted
publication. `status=VERIFIED_PRIVATE`. All fields are strictly allowlisted.
This child export format is distinct from ADR-0016's governed Motherclank
snapshot manifest v1.0 and Diagnostic's observer-surface/adapter versions.
Translation and consumer integration belong to the later COPS-000081 Session.

Metadata includes child/instance/lane IDs, canonical source path, canonical
`deployed_revision` (the separately inspected collector image), distinct
`exporter_revision` (exact candidate), nullable database-evidenced source
revision with unavailable reason, schema 7, native `child_as_of`, clock basis,
UTC completion, fixed relative artifact path/name, bytes/SHA256, integrity/FKs,
SQLite runtime and export/publisher versions. Native clock is the latest
collector-run row's finished/started UTC field, not a claim of last healthy
collection and never publication time. Missing/non-UTC/invalid native time is
null with an explicit bounded reason. The schema does not independently store
source revision; do not fabricate one from an image label.

Source proof includes zero connection `total_changes`, zero copy changes,
unchanged schema cookie, mechanical read-only access and sidecar-presence
before/after. WAL/SHM changes are CHILD SQLite coordination, not domain writes.
This is not a claim that the canonical main-file hash stays fixed during other
legitimate activity, nor that all writers are quiescent.

## Publisher boundary

Host root: `/volume2/clank/feature-phone-clank/observer-export/`, mounted as
fixed `/publication` **only in the owner-invoked finalizer**, never alongside
canonical state. Root-owned layout:

| Path | Owner/group | Mode | Access |
| --- | --- | --- | --- |
| root | 0:10001 | 0750 | no UID10001 write |
| staging, failed | 0:0 | 0700 | root-private parent |
| staging/unique-attempt | 10001:10001 | 0700 | only child unique RW bind |
| accepted | 0:10001 | 0550 | observer readable, not writable |
| accepted/attempt | 0:10001 | 0550 | complete sealed set |
| accepted/attempt/files | 0:10001 | 0440 | root-owned immutable-by-contract |
| pins.json | 0:10001 | 0440 | owner-managed evidence pins |

IDs: `export-YYYYMMDDTHHMMSSZ-16lowercasehex`. Fixed files only. No links,
hardlinks, traversal, unexpected files or supplied source/destination paths.
Publisher CLI takes only attempt ID + exact exporter revision. It executes
no child commands/SQL. Source module SHA256 is pinned in the image label and
rechecked by the owner wrapper. The producer must have exited, with its
container removed, before finalization; ownership cannot revoke an existing
open writable descriptor. Root-private ancestry plus terminated producer is
an explicit precondition, not a fiction about shared UID10001 separation.

Publisher revokes staging ownership before validating metadata/hash/header,
fsyncs all files, adds hash-bound `publication.json`, seals file modes, and
uses kernel `renameat2(RENAME_NOREPLACE)` on the same filesystem. It refuses
unsupported kernels/filesystems. No check-then-replace fallback. The renamed
attempt stays root-private until parent fsync succeeds. Failures after rename
are quarantined under `failed` and never admitted by readers. No overwrite.
The final reader-access grant is the last filesystem operation, after durable
file/inode/directory/parent sync. No fallible sync follows exposure of SUCCESS;
a crash before the access mode persists may leave a complete private set,
which fails closed. A separate nonblocking root publisher lock serializes
capacity accounting and parent-directory mode changes.

The observer receives **only** `accepted:ro`, no canonical/staging bind,
read-only root filesystem, UID10001, all capabilities dropped, no privilege
escalation, no network/webhook. Shared UID10001 is not an independent writer/
reader identity. Root ownership, verified ACL/DAC and kernel RO mounts form
the actual boundary. Synology ACLs and denied-write probes must be evidenced
on NAS; Windows unit tests are not proof of that boundary.

`verify_publication()` verifies root ownership/modes, exact layout, both
hash bindings, standalone integrity/FKs/schema and native-clock binding.
Old native time remains HISTORICAL regardless of a fresh export timestamp;
missing clock is UNKNOWN. This consumer helper is NOT integrated into live
Motherclank in COPS-000089.

## Retention and capacity — no automatic pruning

Policy v1.0: maximum 32 accepted sets / 1 GiB total; unpinned accepted sets
older than 7 days require owner action before another publication. Incident,
admission and rollback references are recorded in root-owned `pins.json` and
retained regardless of age, still counted for capacity. Separate private/
failed area: maximum 16 attempts / 1 GiB; minimum free space 128 MiB. Limits
include the incoming set and fail closed. No automatic prune command exists.
Motherclank never deletes exports. Future owner-authorized archival/pruning
must be pin-aware, tested and separately accepted; do not silently overwrite
pins or previous evidence.

## Acceptance/handoff

Windows and Linux fixtures cover A–Y in `tests/test_observer_snapshot.py`;
ordinary backup regressions remain in `tests/test_survivability.py`. Root
Linux sealing tests intentionally skip on Windows. Exact NAS candidate must
pass them, then a non-root export against live state and fixture WAL-present/
absent cases, with non-mutation/lock/permissions/kernel-RO proofs. No READY
claim, consumer Mission resume or production integration before all gates.

Handoff must record exact reviewed source/image, contract, lock, retention,
non-mutation and NAS proof, and an exact sealed read path. Until then:
`FEATURE_PHONE_OBSERVER_EXPORT_READY=NO`.
