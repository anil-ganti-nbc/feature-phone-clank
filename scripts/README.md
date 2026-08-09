# FEATURE-01 production schedule

One-shot CLI invoked by Windows Task Scheduler — no daemon, matching the
convention used across the other Clanks.

- **Task name:** `FeaturePhoneClank HMD Soak`
- **Schedule:** daily at 06:30, 12:30, 18:30, 22:30 (local machine time —
  this host is set to India Standard Time, so these are IST)
- **Command actually run:** `run-silent.vbs` (hidden window) →
  `run-production.cmd` → `.venv\Scripts\python.exe -m feature_phone_clank.cli run`
  (run lock always on — the script never passes `--no-lock`)
- **Log:** `data\scheduled-runs.log` (appended every run, start/finish
  timestamps + exit code; collector stdout/stderr in between)
- **Overlap protection:** two layers — Task Scheduler's own
  `MultipleInstances IgnoreNew` setting, and this project's own
  `data\feature-phone-clank.lock` file (`core/run_lock.py`). Either alone
  would be enough; both together is cheap insurance.
- **Runs as:** the current Windows user, interactive logon, Limited run
  level — no stored password, no elevation. Only runs while this user is
  logged in (this machine doesn't run headless/server-logon).

## Install

Double-click `install-schedule.cmd` (or run it from a terminal). Re-running
it is safe — `Register-ScheduledTask -Force` replaces the existing
definition rather than erroring.

## Check status

Double-click `status-schedule.cmd`, or:

```
powershell -File scripts\status-schedule.ps1
```

Shows task state, last run result, and next scheduled run time.

## Uninstall

Double-click `uninstall-schedule.cmd`. FEATURE-01 still works from the
command line afterward — only the automatic schedule is removed.

## Run one now, manually (to test)

```
schtasks /run /tn "FeaturePhoneClank HMD Soak"
```

or, bypassing the scheduler entirely:

```
scripts\run-production.cmd
```

Both append to the same `data\scheduled-runs.log`.
