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
  state. No Discord/email delivery is implemented; CLI status/events/report
  are the operator surface.

## Health and evidence

A previously non-empty catalogue returning zero is `blocked_zero_result`, not
a removal event. Collector failure is recorded separately. Classification log
evidence is retained even for blocked/failed runs. Partial/incomplete specs
are explicit state, not fabricated completeness. Supported specification
changes are diffed; availability and broad regional-change semantics are
partial/not implemented for the single `en_int` source.

## Current evidence

GitHub `main`: `f7eda737c90d0f373f19c6754151b386e882a87e`.
Hetzner staging deployment: `c749df33c11b1d8283a3fe48026c6bac6ca4da7e`,
intentionally behind main because the delta is README-only; no deployment is
implied by this document. The canonical suite at GitHub head passed **87
tests, 1 skipped** on Python 3.12. Natural cron runs at
01:15 and 07:15 UTC on 2026-08-16 reported 44 discoveries, zero new/removal
events and zero identity anomalies. `hmd-touch-4g`'s known 404 fallback was
observed safely.

## V1 completion gate

Feature Phone Clank v1 is complete pending owner field test when the HMD/Nokia
scope remains explicit; classification and identity fixtures pass; healthy
confirmed absence prevents false removal; incomplete products remain present;
evidence and health are inspectable; lock and unattended scheduling remain
proven; and persistent state survives routine runs. Owner UI/CLI validation on
the main desktop remains **OWNER FIELD TEST — PENDING**.

## Backlog

- **P0:** none newly identified; continue observing healthy absence and
  incomplete-product behaviour.
- **P1:** deploy and soak the accepted GitHub revision only after a separately
  scoped deployment decision; verify its provenance against the Docker image.
- **P2:** availability/regional-change semantics, dashboard UX, and explicit
  delivery policy.
- **P3:** additional manufacturers, Product Intelligence, Motherclank and
  Unified Clank work.
- **Owner decision:** whether post-v1 scope should expand beyond HMD/Nokia.

## Stage A

Documentation and evidence only. No runtime collector, database, deployment,
source scope, or secret change is justified by the current audit.
