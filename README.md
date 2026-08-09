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

Windows Task Scheduler runs `feature-phone-clank run` four times daily via
`scripts/install-schedule.ps1` (see `scripts/README.md`). A file lock
(`data/feature-phone-clank.lock`) prevents overlapping runs.

## Tests

```
pytest
```

84 tests as of the last check, using only fixtures under `tests/fixtures/hmd/` — no
live network access in the automated suite.
