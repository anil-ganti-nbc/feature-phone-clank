# Feature Phone Clank — Definition of Done

## V1 scope

V1 is **HMD/Nokia feature-phone change detection**, not general feature-phone
market intelligence. The sole production collector is `hmd-nokia`, reading
HMD's official feature-phone, smartphone and sitemap surfaces. Any other
manufacturer requires an explicit later scope decision and starts experimental.

## Current capability

- Discovers official HMD/Nokia catalogue products and quarantines ambiguous
  candidates; smartphones are rejected rather than persisted as products.
- Uses stable `hmd-nokia:<slug>` product keys, structured SKU/spec evidence,
  and a guarded title fallback chain. Degenerate titles (`HMD`, `Nokia`, empty)
  and known Nokia 5310/230 H1 contamination are protected by fixtures/tests.
- Persists products, observations, classification evidence, events and
  consecutive-absence state in SQLite. New, returned, removed, identity-
  correction, and supported specification changes are represented as events.
- Requires three healthy consecutive absences before removal. Failed and
  catastrophic-zero runs do not advance absence counters. An incomplete but
  seen product remains present; `hmd-touch-4g`'s 404 specs page falls back to
  its base page and remains an incomplete observation.
- Uses a stale-aware single-instance lock. Hetzner cron invokes a one-shot
  Docker run four times daily; the isolated Docker volume is its persistent
  state.
- **Stage 4 (this phase): a durable notification outbox and Discord
  delivery.** Every event the pipeline persists is offered to an optional
  `notify` callback (`core/pipeline.py`); `core/runner.py` wires this to
  `providers.discord.DiscordNotifier.enqueue` when running via the CLI.
  Event detection itself has zero Discord/notification imports and produces
  identical `events`/`products`/`observations` state whether or not a
  notifier is attached — verified by `tests/test_notifications.py` and
  `tests/test_acceptance_drill.py`.
- A read-only local field-test dashboard (`dashboard.py`, launched by the
  macOS field-test app under `native/macos/`) remains the only UI; the
  operator surface for Stage 4 is entirely CLI (`report`, `notifications`,
  `deliver`, `test-notify`) per this phase's explicit instruction not to
  build a second UI.

## Health and evidence

A previously non-empty catalogue returning zero is `blocked_zero_result`, not
a removal event. Collector failure is recorded separately. Classification log
evidence is retained even for blocked/failed runs. Partial/incomplete specs
are explicit state, not fabricated completeness. Supported specification
changes are diffed; availability and broad regional-change semantics remain
partial/not implemented for the single `en_int` source (see Backlog — this
phase deliberately did not implement them; the HMD source gives no explicit,
stable evidence for either, so nothing was added — NOT RELIABLE FROM CURRENT
SOURCE / NOT REQUIRED FOR HMD v1).

`feature-phone-clank report`'s `delivery_health` field is a distinct axis
from `source_health`: a Discord outage reports `delivery_health: degraded`
(after a notification has exhausted retries) or `pending` (still retrying),
never `source_health: failed` — `runtime_bridge.get_health` has no
dependency on the `notifications` table at all, by construction.

## Notification / delivery model

- **Schema**: the `notifications` table (schema.sql) predates this phase by
  design and needed no migration — audited first, found sufficient:
  `event_id` (nullable, FK to `events`), `provider`, `dedup_key` (UNIQUE),
  `payload_json`, `status`, `attempts`, `last_error`, `sent_at`.
- **Eligibility policy** (`core/notifications.py`, one dict lookup, no
  scoring): notify on `NEW_PRODUCT`, `FIELD_CHANGED`, `PRODUCT_REMOVED`,
  `SPECS_BECAME_AVAILABLE`, `IDENTITY_ANOMALY`. Suppress (retain, never
  push) `SPECS_BECAME_UNAVAILABLE`, `CLASSIFICATION_CHANGED`, plus the three
  `ChangeType`s the pipeline doesn't currently produce
  (`REGIONAL_VARIANT`/`AVAILABILITY_CHANGED`/`SOURCE_DEGRADED`), suppressed
  defensively rather than left unhandled.
- **Status vocabulary** keeps the schema's existing convention rather than
  introducing a parallel one: `pending` (queued, or transiently failed and
  will retry automatically), `sent` (delivered), `failed` (terminal after
  `MAX_ATTEMPTS=5`), `suppressed` (policy-excluded).
- **Persistence-first**: `DiscordNotifier.enqueue` always writes a row before
  any delivery attempt — a process killed mid-run loses nothing already
  persisted.
- **Retry safety**: a failed attempt increments `attempts`, records
  `last_error`, and stays `pending` until `MAX_ATTEMPTS`; only then does it
  become terminally `failed`. `deliver --requeue-failed` moves `failed` rows
  back to `pending` without resetting `attempts`/history.
- **Deduplication**: `notification_put` is `INSERT OR IGNORE` on the UNIQUE
  `dedup_key` (`Event.dedup_key()`, or a `test:<uuid>` namespace for test
  notifications) — a rerun, restart, or double-invocation cannot create a
  second row for the same event.
- **Independence**: `run` attempts delivery after collection and cannot fail
  the run over a delivery problem (`DiscordNotifier.drain()` never raises).
  `run --no-deliver` skips the attempt entirely, leaving everything queued
  for a standalone `deliver`.
- **Discord message**: compact, evidence-oriented embed (`providers/discord`)
  — product, region, severity, changed fields (old → new), source URL,
  detected timestamp, event ID. No JSON blobs, no LLM-generated prose.

## Operator workflow

```
feature-phone-clank report                 # one JSON doc: health, events, notifications, provenance
feature-phone-clank notifications --status pending
feature-phone-clank notifications --status failed
feature-phone-clank deliver                 # retry pending; --requeue-failed to also retry terminal failures
feature-phone-clank test-notify --webhook <url>   # or --confirm-production for the real configured webhook
```

No command requires opening SQLite directly.

## V1 completion gate

Feature Phone Clank v1 is complete pending owner field test when the HMD/Nokia
scope remains explicit; classification and identity fixtures pass; healthy
confirmed absence prevents false removal; incomplete products remain present;
evidence and health are inspectable; lock and unattended scheduling remain
proven; persistent state survives routine runs; and (as of this phase) the
notification outbox is durable, retry-safe, and delivery cannot damage
collection/event state. Owner UI/CLI validation on the main desktop, and the
owner's own Discord delivery/retry field test, remain **OWNER FIELD TEST —
PENDING**.

## Backlog

- **P0:** none newly identified.
- **P1:** deploy and soak the accepted GitHub revision only after a
  separately scoped deployment decision (this phase changed no Hetzner
  state); verify its provenance against the Docker image.
- **P2:** availability/regional-change semantics remain out of scope — no
  explicit, stable HMD evidence found for either; dashboard UX beyond the
  existing read-only field-test view.
- **P3:** additional manufacturers, Product Intelligence, Motherclank and
  Unified Clank work.
- **Owner decision:** whether post-v1 scope should expand beyond HMD/Nokia;
  whether/when to enable production Discord delivery (currently disabled by
  default — no webhook is configured anywhere in this repo).

## Current evidence

GitHub `main` at the start of this phase:
`757e593bc0f4fe60dbf59f17ea97c57fb2faa7cb`. Hetzner staging deployment
unchanged by this phase: `c749df33c11b1d8283a3fe48026c6bac6ca4da7e` — no
Hetzner runtime change was made or is implied by this document; deployment
of this phase's work is a separate, later decision. Natural cron runs
continued healthy through this phase's audit (last observed 2026-08-17
01:15 UTC, 44 products, zero new/removal events, zero identity anomalies).
The canonical suite at the start of this phase passed 90 tests, 1 skipped;
this phase added 21 new tests (15 notification-behavior + 6 acceptance-drill
scenarios), all against a scratch/tmp-path database — 111 passed, 1 skipped.

## Stage 4 (this phase)

Operator CLI + durable Discord notification outbox. No runtime deployment,
no database reset, no manufacturer expansion, no dashboard, no LLM
summarization. Production Discord delivery remains disabled — no webhook is
configured in any committed file or fixture.
