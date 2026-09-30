"""Narrow Linux root publisher for one child-owned export root.

CLI accepts an attempt ID ONLY. No canonical DB mount, shell, arbitrary
commands, source/destination overrides, SQL, collector, sender or pruning.
The owner invokes this in a hash-pinned container with only /publication RW.
Producer MUST have exited; staging's root-private parent excludes UID10001.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .observer_snapshot import (
    DB_NAME, META_NAME, SEAL_NAME, FORMAT_VERSION, PUBLISHER_VERSION,
    SHA40,
    ExportFailure, digest, fsync_directory, fsync_file, read_only,
    utc_now, validate_metadata, write_json_exclusive, native_clock,
)
from .providers.sqlite.compatibility import inspect_compatibility, StateCompatibility
from .core.run_lock import RunLock, LockError

PUBLICATION_ROOT = Path("/publication")
OBSERVER_GID = 10001
PRODUCER_UID = 10001
ATTEMPT_ID = re.compile(r"export-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\Z")
# Capacity is a gate, NOT a garbage collector. The owner must manually
# archive under an accepted/pin-aware procedure before these limits fill.
MAX_ACCEPTED = 32
MAX_ACCEPTED_BYTES = 1024**3
MAX_PRIVATE = 16
MAX_PRIVATE_BYTES = 1024**3
MAX_UNPINNED_AGE_DAYS = 7
MIN_FREE_BYTES = 128 * 1024**2


def fail(code: str) -> None:
    raise ExportFailure("PUBLICATION_FAILED", code)


def _regular(path: Path, *, writable: bool) -> os.stat_result:
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        fail("UNSAFE_ARTIFACT_TYPE_OR_LINK_COUNT")
    if not writable and (s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o440 or s.st_gid != OBSERVER_GID):
        fail("ARTIFACT_NOT_SEALED")
    return s


def _directory(path: Path, uid: int, mode: int, gid: int | None = None) -> None:
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != uid or stat.S_IMODE(s.st_mode) != mode:
        fail("DIRECTORY_BOUNDARY_INVALID")
    if gid is not None and s.st_gid != gid:
        fail("DIRECTORY_GROUP_INVALID")


def _layout(root: Path) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        fail("LINUX_ROOT_PUBLISHER_REQUIRED")
    # Reject links throughout the fixed mount path. The mount itself is
    # read-write for publisher only; observers bind only accepted/:ro.
    for path in [root, *root.parents]:
        if path.is_symlink():
            fail("SYMLINKED_PUBLICATION_ROOT")
    _directory(root, 0, 0o750, OBSERVER_GID)
    _directory(root / "staging", 0, 0o700, 0)
    _directory(root / "accepted", 0, 0o550, OBSERVER_GID)
    _directory(root / "failed", 0, 0o700, 0)
    _regular(root / "pins.json", writable=False)
    if not (root.stat().st_dev == (root / "staging").stat().st_dev == (root / "accepted").stat().st_dev):
        fail("CROSS_FILESYSTEM_PUBLICATION")


def _read_json(path: Path) -> dict:
    s = _regular(path, writable=True)
    if s.st_size > 32768:
        fail("METADATA_TOO_LARGE")
    def unique_pairs(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                fail("DUPLICATE_METADATA_KEY")
            result[k] = v
        return result
    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError):
        fail("METADATA_INVALID")
    if not isinstance(result, dict):
        fail("METADATA_INVALID")
    return result


def _capacity(root: Path, candidate_bytes: int) -> None:
    pins = _read_json(root / "pins.json")
    if set(pins) != {"policy_version", "attempt_ids"} or pins["policy_version"] != "1.0":
        fail("PIN_POLICY_INVALID")
    if not isinstance(pins["attempt_ids"], list) or any(not isinstance(p, str) or not ATTEMPT_ID.fullmatch(p) for p in pins["attempt_ids"]):
        fail("PIN_POLICY_INVALID")
    accepted = list((root / "accepted").iterdir())
    private = list((root / "staging").iterdir()) + list((root / "failed").iterdir())
    for entries in (accepted, private):
        for p in entries:
            if p.is_symlink() or not p.is_dir() or not ATTEMPT_ID.fullmatch(p.name):
                fail("RETENTION_LAYOUT_INVALID")
    # Count pinned evidence toward capacity, never prune it automatically.
    # Recursive size includes files in failed attempts; reject unsafe links.
    def size(entries):
        total = 0
        for directory in entries:
            for p in directory.rglob("*"):
                if p.is_symlink() or not p.is_file() or p.stat().st_nlink != 1:
                    fail("RETENTION_ARTIFACT_UNSAFE")
                total += p.stat().st_size
        return total
    if len(accepted) >= MAX_ACCEPTED or size(accepted) + candidate_bytes > MAX_ACCEPTED_BYTES:
        fail("ACCEPTED_CAPACITY_EXHAUSTED")
    if len(private) > MAX_PRIVATE or size(private) > MAX_PRIVATE_BYTES:
        fail("PRIVATE_CAPACITY_EXHAUSTED")
    floor = datetime.now(timezone.utc) - timedelta(days=MAX_UNPINNED_AGE_DAYS)
    for directory in accepted:
        seal = _read_json(directory / SEAL_NAME)
        try:
            published = datetime.fromisoformat(seal["published_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError, AttributeError):
            fail("RETENTION_PUBLICATION_INVALID")
        if directory.name not in pins["attempt_ids"] and published < floor:
            fail("RETENTION_OWNER_ACTION_REQUIRED")
    import shutil
    if shutil.disk_usage(root).free < MIN_FREE_BYTES:
        fail("DISK_CAPACITY_EXHAUSTED")


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomic Linux RENAME_NOREPLACE; no unsafe check-then-replace fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        fail("ATOMIC_NOREPLACE_UNAVAILABLE")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            fail("DESTINATION_EXISTS")
        fail("ATOMIC_RENAME_FAILED")


def publish_attempt(attempt_id: str, expected_revision: str) -> dict:
    # Serialize capacity accounting and accepted-parent permission changes.
    # This is publisher coordination, NOT canonical collection authority.
    if not ATTEMPT_ID.fullmatch(attempt_id) or not SHA40.fullmatch(expected_revision):
        fail("INVALID_ATTEMPT_OR_REVISION")
    _layout(PUBLICATION_ROOT)
    try:
        with RunLock.acquire(PUBLICATION_ROOT / "publisher.lock"):
            return _publish_attempt(attempt_id, expected_revision)
    except LockError as exc:
        raise ExportFailure("LOCK_BUSY", "PUBLISHER_LOCK_NOT_GRANTED", exc) from None


def _publish_attempt(attempt_id: str, expected_revision: str) -> dict:
    """Fixed root + fixed layout only; producer must be stopped before call.

    Take ownership BEFORE reading, so the shared UID cannot swap files
    during validation. On failure retain the root-private attempt in staging;
    no accepted SUCCESS is readable. Never delete or silently retry.
    """
    if not ATTEMPT_ID.fullmatch(attempt_id) or not SHA40.fullmatch(expected_revision):
        fail("INVALID_ATTEMPT_OR_REVISION")
    root = PUBLICATION_ROOT
    _layout(root)
    stage = root / "staging" / attempt_id
    final = root / "accepted" / attempt_id
    if final.exists() or final.is_symlink():
        fail("DESTINATION_EXISTS")
    _directory(stage, PRODUCER_UID, 0o700)
    if stage.stat().st_dev != root.stat().st_dev:
        fail("CROSS_FILESYSTEM_STAGING")
    # Atomic ownership revocation plus root-private ancestry protects paths.
    os.chown(stage, 0, 0)
    os.chmod(stage, 0o700)
    if {p.name for p in stage.iterdir()} != {DB_NAME, META_NAME}:
        fail("ATTEMPT_LAYOUT_INVALID")
    for p in stage.iterdir():
        s = _regular(p, writable=True)
        if s.st_uid != PRODUCER_UID or stat.S_IMODE(s.st_mode) != 0o600:
            fail("PRODUCER_FILE_OWNERSHIP_INVALID")
        os.chown(p, 0, 0)
    _capacity(root, sum(p.stat().st_size for p in stage.iterdir()) + 4096)
    m = _read_json(stage / META_NAME)
    validate_metadata(m)
    from . import observer_snapshot
    if m["canonical_source_path"] != observer_snapshot.CANONICAL_SOURCE:
        fail("CANONICAL_SOURCE_BINDING_MISMATCH")
    if m["exporter_revision"] != expected_revision:
        fail("REVISION_BINDING_MISMATCH")
    db = stage / DB_NAME
    if db.stat().st_size != m["size_bytes"] or digest(db) != m["sha256"]:
        fail("COPY_BINDING_MISMATCH")
    with db.open("rb") as stream:
        if stream.read(20)[18:20] != b"\x01\x01":
            fail("NON_STANDALONE_COPY")
    seal = {"status": "SUCCESS", "publisher_version": PUBLISHER_VERSION,
            "export_format_version": FORMAT_VERSION, "attempt_id": attempt_id,
            "metadata_sha256": digest(stage / META_NAME), "artifact_sha256": m["sha256"],
            "exporter_revision": expected_revision, "deployed_revision": m["deployed_revision"], "published_at": utc_now()}
    write_json_exclusive(stage / SEAL_NAME, seal)
    for p in stage.iterdir():
        os.chown(p, 0, OBSERVER_GID)
        os.chmod(p, 0o440)
        fsync_file(p)
    # Hold the fixed accepted parent writable only while root renames.
    # UID10001 never gains write; stage remains 0700 until durable rename.
    accepted = root / "accepted"
    os.chmod(accepted, 0o750)
    moved = False
    try:
        fsync_directory(stage)
        rename_no_replace(stage, final)
        moved = True
        fsync_directory(accepted)
        fsync_directory(root / "staging")
        os.chown(final, 0, OBSERVER_GID)
        fsync_directory(final)
        os.chmod(accepted, 0o550)
        fsync_directory(accepted)
        # The final access grant is the LAST filesystem operation. There is
        # no fallible fsync/finally operation after exposing SUCCESS. Data,
        # ownership and rename already are durable. A crash before this mode
        # change persists can leave a complete but private set (fail closed),
        # never a partially visible DB/metadata or post-grant failure window.
        os.chmod(final, 0o550)
    except BaseException:
        os.chmod(accepted, 0o750)
        if moved:
            os.chmod(final, 0o700)
            # Retain failed publication separately, never expose SUCCESS.
            rename_no_replace(final, root / "failed" / attempt_id)
            fsync_directory(root / "failed")
            fsync_directory(accepted)
        os.chmod(accepted, 0o550)
        fsync_directory(accepted)
        raise
    return seal


def verify_publication(path: Path, *, max_native_age_seconds: int = 86400) -> dict:
    """Read-only consumer contract. Verify sealed identity before opening copy.

    An old export remains HISTORICAL, never refreshed by publication time.
    This function is not wired into Motherclank production in this Mission.
    """
    _directory(path, 0, 0o550, OBSERVER_GID)
    if not ATTEMPT_ID.fullmatch(path.name) or {p.name for p in path.iterdir()} != {DB_NAME, META_NAME, SEAL_NAME}:
        fail("PUBLISHED_LAYOUT_INVALID")
    for p in path.iterdir():
        _regular(p, writable=False)
    m, seal = _read_json(path / META_NAME), _read_json(path / SEAL_NAME)
    validate_metadata(m)
    from . import observer_snapshot
    if m["canonical_source_path"] != observer_snapshot.CANONICAL_SOURCE:
        fail("CANONICAL_SOURCE_BINDING_MISMATCH")
    if set(seal) != {"status", "publisher_version", "export_format_version", "attempt_id",
                     "metadata_sha256", "artifact_sha256", "exporter_revision", "deployed_revision", "published_at"}:
        fail("SEAL_CONTRACT_INVALID")
    if (seal["status"] != "SUCCESS" or seal["publisher_version"] != PUBLISHER_VERSION
        or seal["export_format_version"] != FORMAT_VERSION or seal["attempt_id"] != path.name
        or seal["metadata_sha256"] != digest(path / META_NAME)
        or seal["artifact_sha256"] != m["sha256"] or seal["deployed_revision"] != m["deployed_revision"]
        or seal["exporter_revision"] != m["exporter_revision"]
        or (path / DB_NAME).stat().st_size != m["size_bytes"] or digest(path / DB_NAME) != m["sha256"]):
        fail("PUBLISHED_BINDING_MISMATCH")
    con = read_only(path / DB_NAME, time.monotonic() + 60)
    try:
        if list(con.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise ExportFailure("INTEGRITY_FAILED", "PUBLISHED_INTEGRITY_FAILED")
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ExportFailure("FK_FAILED", "PUBLISHED_FOREIGN_KEYS_FAILED")
        if inspect_compatibility(con).state is not StateCompatibility.COMPATIBLE:
            raise ExportFailure("SCHEMA_INCOMPATIBLE", "PUBLISHED_SCHEMA_INCOMPATIBLE")
        if native_clock(con) != (m["child_as_of"], m["child_as_of_clock"], m["child_as_of_unavailable_reason"]):
            fail("NATIVE_CLOCK_BINDING_MISMATCH")
        if con.total_changes != 0:
            fail("CONSUMER_NON_MUTATION_GATE_FAILED")
    finally:
        con.close()
    state = "UNKNOWN"
    if m["child_as_of"] is not None:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(m["child_as_of"].replace("Z", "+00:00"))).total_seconds()
        state = "CURRENT" if 0 <= age <= max_native_age_seconds else "HISTORICAL"
    return {"status": "VERIFIED", "freshness": state, "metadata": m}


def main(argv=None) -> int:
    # No argparse traceback or echoing untrusted values; bounded errors only.
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 2:
            fail("EXPECTED_ATTEMPT_ID_AND_REVISION")
        result = publish_attempt(args[0], args[1])
        print(json.dumps(result, sort_keys=True))
        return 0
    except ExportFailure as exc:
        print(json.dumps(exc.envelope(), sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps(ExportFailure("PUBLICATION_FAILED", "PUBLISHER_OPERATION_FAILED", exc).envelope(), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
