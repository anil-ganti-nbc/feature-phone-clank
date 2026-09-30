"""Child-owned observer export. No SqliteStore construction, collectors or sender.

This produces VERIFIED_PRIVATE staging, never an admitted publication. The
separate fixed-root publisher must seal it after the producer has exited.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .core.run_lock import LockError, RunLock
from .providers.sqlite import BackupIntegrityError, online_backup_to_partial
from .providers.sqlite.compatibility import (
    EXPECTED_SCHEMA_VERSION, StateCompatibility, inspect_compatibility,
)

FORMAT_VERSION = "1.0"
PUBLISHER_VERSION = "1.0"
CLANK_ID = "feature-phone-clank"
INSTANCE_ID = "feature-phone-clank-nas-cops-000072"
LANE_ID = "experimental"
CANONICAL_SOURCE = "/volume2/clank/feature-phone-clank/state/feature_phone_clank.db"
DB_NAME = "feature_phone_clank.db"
META_NAME = "metadata.json"
SEAL_NAME = "publication.json"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
STAGES = frozenset({
    "SOURCE_ADMISSION_FAILED", "LOCK_BUSY", "BACKUP_FAILED", "INTEGRITY_FAILED",
    "FK_FAILED", "SCHEMA_INCOMPATIBLE", "METADATA_FAILED", "PUBLICATION_FAILED",
    "TIMEOUT", "OTHER",
})


class ExportFailure(Exception):
    """Only bounded stages and SQLite codes cross the diagnostic boundary."""
    def __init__(self, stage: str, code: str, cause: Exception | None = None):
        assert stage in STAGES
        self.stage, self.code = stage, code
        self.sqlite_code = getattr(cause, "sqlite_errorcode", None)
        self.sqlite_name = getattr(cause, "sqlite_errorname", None)
        super().__init__(stage)

    def envelope(self) -> dict:
        result = {"status": "FAILED", "stage": self.stage, "code": self.code}
        if isinstance(self.sqlite_code, int):
            result["sqlite_errorcode"] = self.sqlite_code
        if isinstance(self.sqlite_name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]+", self.sqlite_name):
            result["sqlite_errorname"] = self.sqlite_name
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(path: Path, deadline: float | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise ExportFailure("TIMEOUT", "DEADLINE_EXCEEDED")
            h.update(block)
    return h.hexdigest()


def fsync_file(path: Path) -> None:
    with path.open("r+b" if os.name == "nt" else "rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    # Durable publication is Linux-only. Windows tests verify the producer and
    # no-clobber rename separately; they cannot certify directory durability.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_exclusive(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def sidecars(path: Path) -> dict:
    return {suffix: Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")}


def read_only(path: Path, deadline: float | None = None) -> sqlite3.Connection:
    """Existing file only. No immutable/nolock, bootstrap or write fallback."""
    con = sqlite3.connect("file:" + quote(path.absolute().as_posix(), safe="/:") + "?mode=ro",
                          uri=True, timeout=0)
    con.execute("PRAGMA query_only=ON")
    if deadline is not None:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    # Allow only the read operations used by compatibility/metadata. In
    # addition to mode=ro and query_only, domain/schema writes, ATTACH and
    # source checkpoint/journal conversion are mechanically denied.
    def authorize(action, a, b, database, trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if a.lower() in {"quick_check", "integrity_check", "foreign_key_check", "table_info"}:
                return sqlite3.SQLITE_OK
            if a.lower() in {"journal_mode", "schema_version", "query_only"} and b is None:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    con.set_authorizer(authorize)
    return con


def native_clock(con: sqlite3.Connection) -> tuple[str | None, str, str | None]:
    row = con.execute("SELECT COALESCE(finished_at,started_at) FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row or row[0] is None:
        return None, "UNAVAILABLE", "NO_NATIVE_RUN_CLOCK"
    try:
        value = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            return None, "UNAVAILABLE", "NATIVE_CLOCK_NOT_EXPLICIT_UTC"
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "NATIVE_RUN_ROW_UTC", None
    except (ValueError, TypeError, AttributeError):
        return None, "UNAVAILABLE", "NATIVE_CLOCK_INVALID"


def export_snapshot(source: Path, output: Path, *, timeout_seconds: float = 60) -> dict:
    """Run once, nonblocking collection lock, bounded SQLite work, no retries.

    Canonical lock is derived from the DB's directory, exactly
    /app/data/feature-phone-clank.lock for the NAS command. No lock bypass or
    arbitrary alternative lock is exposed. Source RO coordinates sidecars in
    the child's normal RW directory; that is not a grant to Motherclank.
    """
    deadline = time.monotonic() + timeout_seconds
    stage = "SOURCE_ADMISSION_FAILED"
    lock = None
    con = None
    try:
        if not 0 < timeout_seconds <= 300:
            raise ExportFailure("TIMEOUT", "INVALID_TIME_BUDGET")
        source, output = source.absolute(), output.absolute()
        if any(p.is_symlink() for p in [source, *source.parents]) or not source.is_file():
            raise ExportFailure(stage, "SOURCE_MISSING_OR_UNSAFE")
        # Prevent a private output mount from aliasing canonical state.
        if source.parent.resolve() == output.resolve():
            raise ExportFailure("PUBLICATION_FAILED", "STAGING_ALIASES_SOURCE_DIRECTORY")
        revision = os.environ.get("FEATURE_PHONE_CLANK_SOURCE_REVISION", "")
        if not SHA40.fullmatch(revision):
            raise ExportFailure("METADATA_FAILED", "EXPORTER_REVISION_UNAVAILABLE")
        deployed = os.environ.get("FEATURE_PHONE_CLANK_CANONICAL_DEPLOYED_REVISION", "")
        if not SHA40.fullmatch(deployed):
            raise ExportFailure("METADATA_FAILED", "CANONICAL_DEPLOYED_REVISION_UNAVAILABLE")
        stage = "PUBLICATION_FAILED"
        if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
            raise ExportFailure(stage, "STAGING_NOT_PRIVATE_EMPTY_DIRECTORY")
        if os.name == "posix":
            permissions = stat.S_IMODE(output.stat().st_mode)
            if permissions != 0o700 or output.stat().st_uid != os.getuid():
                raise ExportFailure(stage, "STAGING_OWNERSHIP_OR_MODE_INVALID")
        # Reuse the exact OS-backed collection lock, not the delivery lock.
        stage = "LOCK_BUSY"
        lock = RunLock.acquire(source.parent / "feature-phone-clank.lock")
        stage = "SOURCE_ADMISSION_FAILED"
        before = sidecars(source)
        con = read_only(source, deadline)
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        report = inspect_compatibility(con)
        if time.monotonic() >= deadline:
            raise ExportFailure("TIMEOUT", "DEADLINE_EXCEEDED")
        if report.state is not StateCompatibility.COMPATIBLE:
            stage = "SOURCE_ADMISSION_FAILED" if report.state is StateCompatibility.CORRUPT else "SCHEMA_INCOMPATIBLE"
            raise ExportFailure(stage, report.state.value)
        source_schema = con.execute("PRAGMA schema_version").fetchone()[0]
        partial, artifact = output / (DB_NAME + ".partial"), output / DB_NAME
        stage = "BACKUP_FAILED"
        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise ExportFailure("TIMEOUT", "DEADLINE_EXCEEDED")
            if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
                # No automatic retry of SQLITE_BUSY/LOCKED or a failed attempt.
                exc = ExportFailure("BACKUP_FAILED", "SQLITE_BACKUP_STATUS")
                exc.sqlite_code = status
                raise exc
        online_backup_to_partial(con, partial, progress=progress,
                                 normalize_destination=True, exclusive=True,
                                 verification_progress=lambda: int(time.monotonic() >= deadline))
        # The file rename is inside private staging, NOT publication. The
        # root finalizer publishes DB + metadata together as one directory.
        os.rename(partial, artifact)
        stage = "INTEGRITY_FAILED"
        copy = read_only(artifact, deadline)
        try:
            if list(copy.execute("PRAGMA integrity_check")) != [("ok",)]:
                raise ExportFailure(stage, "COPY_INTEGRITY_FAILED")
            stage = "FK_FAILED"
            if copy.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ExportFailure(stage, "COPY_FOREIGN_KEYS_FAILED")
            stage = "SCHEMA_INCOMPATIBLE"
            if inspect_compatibility(copy).state is not StateCompatibility.COMPATIBLE:
                raise ExportFailure(stage, "COPY_SCHEMA_INCOMPATIBLE")
            stage = "METADATA_FAILED"
            child_as_of, clock, unavailable = native_clock(copy)
            schema = copy.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            copy_changes = copy.total_changes
        finally:
            copy.close()
        if con.total_changes != 0 or con.execute("PRAGMA schema_version").fetchone()[0] != source_schema:
            raise ExportFailure("SOURCE_ADMISSION_FAILED", "SOURCE_NON_MUTATION_GATE_FAILED")
        source_changes = con.total_changes
        con.close()
        con = None
        after = sidecars(source)
        if any(sidecars(artifact).values()):
            raise ExportFailure("INTEGRITY_FAILED", "COPY_REQUIRES_SIDECARS")
        with artifact.open("rb") as stream:
            if stream.read(20)[18:20] != b"\x01\x01":
                raise ExportFailure("INTEGRITY_FAILED", "COPY_NOT_STANDALONE_DELETE_JOURNAL")
        metadata = {
            "status": "VERIFIED_PRIVATE", "export_format_version": FORMAT_VERSION,
            "publisher_version": PUBLISHER_VERSION,
            "clank_id": CLANK_ID, "instance_id": INSTANCE_ID, "lane_id": LANE_ID,
            "canonical_source_path": (CANONICAL_SOURCE if source.as_posix() == "/app/data/" + DB_NAME
                                      else source.as_posix()),
            "deployed_revision": deployed, "exporter_revision": revision, "source_revision": None,
            "source_revision_unavailable_reason": "NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE",
            "schema_version": schema, "child_as_of": child_as_of,
            "child_as_of_clock": clock, "child_as_of_unavailable_reason": unavailable,
            "export_completed_at": utc_now(), "artifact_filename": DB_NAME,
            "artifact_path": DB_NAME, "size_bytes": artifact.stat().st_size,
            "sha256": digest(artifact, deadline), "integrity_check": "ok",
            "foreign_key_violations": 0, "sqlite_runtime_version": sqlite3.sqlite_version,
            "source_access": {
                "mode": "ro", "query_only": True, "authorizer": "READ_ALLOWLIST_V1",
                "total_changes": source_changes, "copy_total_changes": copy_changes,
                "schema_cookie_unchanged": True,
                "sidecars_before": before, "sidecars_after": after,
                "sidecar_authority": "CHILD_SQLITE_COORDINATION_NOT_DOMAIN_WRITES",
            },
        }
        validate_metadata(metadata)
        fsync_file(artifact)
        write_json_exclusive(output / META_NAME, metadata)
        if os.name == "posix":
            fsync_directory(output)
        if time.monotonic() >= deadline:
            raise ExportFailure("TIMEOUT", "DEADLINE_EXCEEDED")
        return {"status": "VERIFIED_PRIVATE", "export_format_version": FORMAT_VERSION,
                "sha256": metadata["sha256"], "size_bytes": metadata["size_bytes"]}
    except ExportFailure:
        raise
    except LockError as exc:
        raise ExportFailure("LOCK_BUSY", "COLLECTION_LOCK_NOT_GRANTED", exc) from None
    except BackupIntegrityError as exc:
        raise ExportFailure("INTEGRITY_FAILED", "COPY_INTEGRITY_FAILED", exc) from None
    except Exception as exc:
        if time.monotonic() >= deadline:
            raise ExportFailure("TIMEOUT", "DEADLINE_EXCEEDED", exc) from None
        raise ExportFailure(stage if stage in STAGES else "OTHER", "OPERATION_FAILED", exc) from None
    finally:
        if con is not None:
            con.close()
        if lock is not None:
            lock.release()


METADATA_KEYS = frozenset({
    "status", "export_format_version", "publisher_version", "clank_id", "instance_id",
    "lane_id", "canonical_source_path", "deployed_revision", "exporter_revision", "source_revision",
    "source_revision_unavailable_reason", "schema_version", "child_as_of", "child_as_of_clock",
    "child_as_of_unavailable_reason", "export_completed_at", "artifact_filename", "artifact_path",
    "size_bytes", "sha256", "integrity_check", "foreign_key_violations", "sqlite_runtime_version",
    "source_access",
})


def validate_metadata(m: dict) -> None:
    """Strict bounded wire contract; reject extra payload/credential fields."""
    def require(condition):
        if not condition:
            raise ExportFailure("METADATA_FAILED", "METADATA_CONTRACT_INVALID")
    require(isinstance(m, dict) and set(m) == METADATA_KEYS)
    for key, expected in {
        "status": "VERIFIED_PRIVATE", "export_format_version": FORMAT_VERSION,
        "publisher_version": PUBLISHER_VERSION, "clank_id": CLANK_ID, "instance_id": INSTANCE_ID,
        "lane_id": LANE_ID,
        "artifact_filename": DB_NAME, "artifact_path": DB_NAME,
        "schema_version": EXPECTED_SCHEMA_VERSION, "integrity_check": "ok", "foreign_key_violations": 0,
        "source_revision": None, "source_revision_unavailable_reason": "NOT_INDEPENDENTLY_EVIDENCED_IN_DATABASE",
    }.items():
        require(m[key] == expected and type(m[key]) is type(expected))
    require(isinstance(m["deployed_revision"], str) and bool(SHA40.fullmatch(m["deployed_revision"])))
    require(isinstance(m["exporter_revision"], str) and bool(SHA40.fullmatch(m["exporter_revision"])))
    require(isinstance(m["canonical_source_path"], str) and len(m["canonical_source_path"]) <= 512
            and Path(m["canonical_source_path"]).is_absolute())
    require(isinstance(m["sha256"], str) and bool(SHA64.fullmatch(m["sha256"])))
    require(type(m["size_bytes"]) is int and 0 < m["size_bytes"] <= 1024**3)
    require(isinstance(m["sqlite_runtime_version"], str) and bool(re.fullmatch(r"\d+\.\d+\.\d+", m["sqlite_runtime_version"])))
    for key in ("export_completed_at", "child_as_of"):
        value = m[key]
        if key == "child_as_of" and value is None:
            continue
        require(isinstance(value, str) and len(value) <= 40 and value.endswith("Z"))
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            require(False)
    if m["child_as_of"] is None:
        require(m["child_as_of_clock"] == "UNAVAILABLE" and m["child_as_of_unavailable_reason"] in {
            "NO_NATIVE_RUN_CLOCK", "NATIVE_CLOCK_NOT_EXPLICIT_UTC", "NATIVE_CLOCK_INVALID"})
    else:
        require(m["child_as_of_clock"] == "NATIVE_RUN_ROW_UTC" and m["child_as_of_unavailable_reason"] is None)
    p = m["source_access"]
    require(isinstance(p, dict) and set(p) == {
        "mode", "query_only", "authorizer", "total_changes", "copy_total_changes",
        "schema_cookie_unchanged", "sidecars_before", "sidecars_after", "sidecar_authority"})
    for key, expected in {"mode": "ro", "query_only": True, "authorizer": "READ_ALLOWLIST_V1",
                          "total_changes": 0, "copy_total_changes": 0, "schema_cookie_unchanged": True,
                          "sidecar_authority": "CHILD_SQLITE_COORDINATION_NOT_DOMAIN_WRITES"}.items():
        require(p[key] == expected and type(p[key]) is type(expected))
    for key in ("sidecars_before", "sidecars_after"):
        require(isinstance(p[key], dict) and set(p[key]) == {"-wal", "-shm", "-journal"})
        require(all(type(v) is bool for v in p[key].values()))


def command(args) -> int:
    try:
        result = export_snapshot(Path(args.db), Path(args.output), timeout_seconds=args.timeout_seconds)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ExportFailure as exc:
        print(json.dumps(exc.envelope(), sort_keys=True))
        return {"LOCK_BUSY": 2, "SCHEMA_INCOMPATIBLE": 3, "TIMEOUT": 4}.get(exc.stage, 1)
    except Exception as exc:
        print(json.dumps(ExportFailure("OTHER", "COMMAND_FAILED", exc).envelope(), sort_keys=True))
        return 1
