# Feature Phone Clank

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
feature-phone-clank run
feature-phone-clank status
feature-phone-clank events
feature-phone-clank report
```

`--db` (default `data/feature_phone_clank.db`) and `--scope` (default
`config/scope.yaml`) are available globally.

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

84 tests as of the last check, using only fixtures under `tests/fixtures/hmd/` — no
live network access in the automated suite.
