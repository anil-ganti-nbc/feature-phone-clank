"""Cross-platform single-instance lock for one-shot `run` invocations.

Ported verbatim from OEM Radar's `core/run_lock.py` (brief section 15 /
user constraint 1: "reuse OEM Radar's established locking pattern rather
than inventing a new one"). Only the docstring and log-channel name were
adjusted; the algorithm is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("feature_phone_clank.run_lock")


class LockError(Exception):
    """Raised when the lock cannot be acquired."""


def _pid_alive(pid: int) -> bool | None:
    """Return True if process exists, False if not, None if unknown."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # ERROR_INVALID_PARAMETER (87) usually means PID does not exist
            err = ctypes.windll.kernel32.GetLastError()
            if err in (87, 0):
                return False
            return None
        except Exception:
            return None
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not owned by us
    except Exception:
        return None


@dataclass
class RunLock:
    path: Path
    pid: int
    acquired_at: float
    _held: bool = False

    @classmethod
    def acquire(cls, path: str | Path, *, stale_after_hours: float = 12.0) -> "RunLock":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # Atomic exclusive create
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return cls._handle_existing(path, payload, stale_after_hours)
        try:
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        lock = cls(path=path, pid=payload["pid"], acquired_at=payload["started_at"], _held=True)
        log.info("acquired run lock %s (pid=%s)", path, lock.pid)
        return lock

    @classmethod
    def _handle_existing(cls, path: Path, payload: dict, stale_after_hours: float) -> "RunLock":
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            old_pid = int(existing.get("pid") or 0)
            old_started = float(existing.get("started_at") or 0)
        except Exception as exc:
            raise LockError(
                f"lock file {path} exists but is unreadable ({exc}); "
                f"refusing to start. Remove it only if no crawl is running."
            ) from exc

        alive = _pid_alive(old_pid)
        age_h = (time.time() - old_started) / 3600.0 if old_started else None
        if alive is True:
            raise LockError(
                f"another feature-phone-clank run is active (pid={old_pid}, lock={path}). "
                f"Wait for it to finish or stop that process."
            )
        if alive is None:
            raise LockError(
                f"lock file {path} exists (pid={old_pid}) and process liveness "
                f"could not be determined; refusing to steal the lock."
            )
        # alive is False -> stale
        if age_h is not None and age_h < 0.01:
            # extremely fresh + dead is odd; still reclaim after proven dead
            pass
        log.warning(
            "removing stale lock %s (old pid=%s not alive, age_h=%s)",
            path, old_pid, f"{age_h:.2f}" if age_h is not None else "?",
        )
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise LockError(f"could not remove stale lock {path}: {exc}") from exc
        # retry exclusive create
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise LockError(
                f"race reclaiming lock {path}; another process started. Retry later."
            ) from exc
        try:
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        lock = cls(path=path, pid=payload["pid"], acquired_at=payload["started_at"], _held=True)
        log.info("reclaimed stale lock %s (pid=%s)", path, lock.pid)
        return lock

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if int(data.get("pid") or 0) == self.pid:
                    self.path.unlink(missing_ok=True)
                    log.info("released run lock %s", self.path)
                else:
                    log.warning(
                        "lock %s owned by pid=%s, not releasing (our pid=%s)",
                        self.path, data.get("pid"), self.pid,
                    )
        except Exception as exc:
            log.warning("failed to release lock %s: %s", self.path, exc)
        finally:
            self._held = False

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
