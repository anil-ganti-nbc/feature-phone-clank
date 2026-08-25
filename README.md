# Feature Phone Clank

> **Phase 0: UNVERIFIED_PRODUCTION — promotion frozen.** The unauthenticated
> dashboard is loopback-only and its collection mutation is disabled until an
> authenticated profile exists.

> Status: Experimental / under construction

Change-detection intelligence for official feature-phone sources (FEATURE-01). Crawls
HMD's official site (feature-phones and smartphones catalogue listings plus
`sitemap-dtc.xml`), deterministically classifies each product URL, extracts structured
spec fields from the page's own embedded data, and persists an append-only observation
history to SQLite — diffing each run against the last to emit deterministic change
events.

## Usage

```
feature-phone-clank version
feature-phone-clank identity
feature-phone-clank health
feature-phone-clank run                # collects, then attempts delivery of pending notifications
feature-phone-clank status
feature-phone-clank events
feature-phone-clank report             # health + events + notifications + provenance, one JSON doc
feature-phone-clank deliver            # (re)attempt delivery of pending notifications standalone
feature-phone-clank notifications      # outbox counts, or --status pending|sent|failed|suppressed
feature-phone-clank test-notify        # send a marked FEATURE-01 TEST notification
feature-phone-clank backup <output>    # verified SQLite-safe recovery point (run-lock cooperative)
feature-phone-clank continuity         # ADR-0006 continuity registry (epoch evidence)
feature-phone-clank run-experimental   # soak-run itel/lava/punkt against the isolated experimental DB
```

`--db` (default `data/feature_phone_clank.db`) and `--scope` (default
`config/scope.yaml`) are available globally.

## Notifications (Stage 4)

`collect -> classify -> persist observation -> diff -> persist event` is
unchanged and remains independent of delivery. Every newly-persisted event
enqueues a notification into the durable `notifications` outbox
(`core/notifications.py` decides eligibility; `providers/discord` renders
and sends). A Discord outage never blocks collection, never re-runs a
collector, and never duplicates an event or a notification — dedup is by
`Event.dedup_key()`, enforced by a DB `UNIQUE` constraint, not a timestamp
check.

**Eligibility policy** (`core/notifications.py`):

| Notify by default | Suppressed by default (retained, never pushed) |
|---|---|
| `NEW_PRODUCT` | `SPECS_BECAME_UNAVAILABLE` |
| `FIELD_CHANGED` | `CLASSIFICATION_CHANGED` |
| `PRODUCT_REMOVED` | `REGIONAL_VARIANT` (not currently produced) |
| `SPECS_BECAME_AVAILABLE` | `AVAILABILITY_CHANGED` (not currently produced) |
| `IDENTITY_ANOMALY` | `SOURCE_DEGRADED` (not currently produced) |

**Outbox status values** (`notifications.status`, matches the schema's
pre-existing convention rather than introducing a parallel one): `pending`
(queued or transiently failed — retried automatically), `sent` (delivered),
`failed` (terminal after `MAX_ATTEMPTS=5`, see `providers/discord`), or
`suppressed` (policy decided not to notify; still audit-visible).

**Credentials**: set `FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL` in the
environment. Never hard-coded, never committed, never logged — unset means
notifications still enqueue but delivery no-ops (`remaining` count in
`deliver`'s output tells you how many are waiting).

**Owner field test**: `feature-phone-clank test-notify --webhook <test URL>`
sends an unmistakably marked `FEATURE-01 TEST` embed through the real
delivery code path without creating any `events`/`products` row. Sending to
the configured production webhook (no `--webhook` override) requires
`--confirm-production`.

**Retry**: `feature-phone-clank deliver --requeue-failed` moves terminally
`failed` rows back to `pending` (e.g. after fixing the webhook URL) without
losing their `attempts`/`last_error` history, then attempts delivery.

## Scheduling

Two independent unattended schedules currently run this project, each against
its own isolated database — they are not the same soak, and their evidence is
combined, not merged:

- **Windows** (original): Task Scheduler runs `feature-phone-clank run` four
  times daily via `scripts/install-schedule.ps1` (see `scripts/README.md`)
  against `data/feature_phone_clank.db`. This remains active independently of
  the cloud deployment below.
- **Hetzner** (added 2026-08-09, this project's cloud migration): cron runs
  the same command inside Docker, four times daily (`15 1,7,13,17 * * *`
  UTC — the same intended cadence, converted from Windows' local-time
  06:30/12:30/18:30/22:30 IST triggers), against an isolated named Docker
  volume (`feature_phone_clank_staging_data`), completely separate from the
  Windows database.

Both use the same file-lock mechanism (`data/feature-phone-clank.lock`,
`core/run_lock.py`) to prevent overlapping runs — verified under a real
deliberate overlap on Hetzner (a second invocation while one was active
correctly returned `{"status": "locked", ...}` and exited cleanly rather than
running a duplicate writer).

## Cloud deployment (Hetzner)

```
image:              feature-phone-clank:<short-sha>  (currently c749df3)
deployed revision:   c749df33c11b1d8283a3fe48026c6bac6ca4da7e (full SHA)
persistent state:    named Docker volume feature_phone_clank_staging_data
                     (fresh cloud baseline, NOT copied from the Windows database)
release_channel:      experimental (unchanged — no promotion implied by containerization)
production scope:     unchanged — hmd-nokia only (config/scope.yaml)
```

Git-revision provenance is built in: `org.opencontainers.image.revision`
(OCI label) and the `identity`/`version` commands' `source_revision` field
both report the exact deployed commit SHA, verified equal to the merged
GitHub commit. Local/non-Docker runs report `"unknown"` rather than a
fabricated value. Same pattern as OEM Radar and Chinese Tech Wire.

Real-network validation on Hetzner: multiple genuine scheduled and manual
cycles, all `status: ok`, 44 products discovered, 0 errors, 0 new
`identity_anomalies`. `hmd-touch-4g`'s specs-page HTTP 404 (a known,
pre-existing upstream quirk — the source falls back to the base page rather
than failing) reproduced identically on Hetzner as it does on Windows,
confirming this is a genuine site behavior, not a Windows/Linux transport
difference.

Windows-specific scheduling files (`.cmd`, `.ps1`, `.vbs` under `scripts/`)
remain as optional Windows convenience tooling only — nothing in the
container runtime path depends on them.

**Rollback**: the Hetzner host reads which image tag to run from a plain
`.deployed-id` file (not baked into the cron line or compose file), so
switching revisions is a one-line file edit, not a rebuild. This is the
first cloud deployment for this clank — no previous image exists yet to roll
back to; the next revision change should retain `c749df3` before replacing
it. Persistent state (the named volume) is independent of which image tag
runs against it.

**NAS portability**: nothing above is Hetzner-specific — the named volume,
the Git-revision build-arg mechanism, and the cron/lock design all transfer
directly to a Synology NAS or any other Linux/Docker host. No Hetzner IP,
API, or provider metadata is referenced anywhere in this project.

## Tests

```
pytest
```

111 tests / 1 skipped as of the last check (90/1 baseline + 21 new for Stage
4 notifications/delivery/acceptance-drill), using only fixtures under
`tests/fixtures/hmd/` and a fake Discord sender — no live network access, and
no real webhook is ever contacted, in the automated suite.
