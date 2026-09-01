"""Cross-platform run lock backed by an OS-held advisory grant.

The lock file's JSON is diagnostic metadata only. Exclusivity comes from the
kernel lock held by the open descriptor for the entire run; PID liveness and
the contents of the marker can never authorize acquisition or reclamation.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


_WINDOWS_LOCK_OFFSET = 1 << 20
log = logging.getLogger("feature_phone_clank.run_lock")


class LockError(Exception):
    """Raised when the kernel cannot grant the run lock."""


def _os_lock(fd: int) -> None:
    """Take a non-blocking exclusive lock on the descriptor."""
    if sys.platform == "win32":
        # Keep the diagnostic JSON readable at offset zero while reserving a
        # separate byte range for the Windows kernel grant.
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"path": str(path), "owner": "unreadable"}
    return value if isinstance(value, dict) else {"path": str(path), "owner": "unreadable"}


@dataclass
class RunLock:
    path: Path
    pid: int
    acquired_at: float
    _held: bool = False
    _fd: int | None = field(default=None, repr=False)

    @classmethod
    def acquire(cls, path: str | Path, *, stale_after_hours: float = 12.0) -> "RunLock":
        """Acquire the kernel grant, retaining the historical API argument.

        `stale_after_hours` is intentionally ignored: there is no stale
        timeout or PID-based reclaim under an OS-held lock. The kernel drops
        the grant when the owning descriptor closes, including on crashes.
        """
        del stale_after_hours
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _os_lock(fd)
        except OSError as exc:
            metadata = _read_metadata(path)
            os.close(fd)
            raise LockError(
                f"another feature-phone-clank run holds the kernel lock "
                f"(metadata={json.dumps(metadata, sort_keys=True)}; lock={path})"
            ) from exc

        started_at = time.time()
        payload = {
            "pid": os.getpid(),
            "started_at": started_at,
            "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "lock_authority": "os_advisory_lock",
        }
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        except OSError:
            # Metadata is diagnostic-only. Never discard a genuine grant
            # because the marker cannot be refreshed.
            log.warning("could not refresh diagnostic lock metadata: %s", path)

        lock = cls(path=path, pid=payload["pid"], acquired_at=started_at, _held=True, _fd=fd)
        log.info("acquired run lock %s (pid=%s)", path, lock.pid)
        return lock

    def release(self) -> None:
        if not self._held or self._fd is None:
            return
        try:
            _os_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
            self._held = False
            log.info("released run lock %s", self.path)

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

