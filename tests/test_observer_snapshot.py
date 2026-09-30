"""A–Y observer contract. Hermetic fixtures only, real CLI process exits.

Linux root publication tests are intentionally skipped on Windows: local
tests cannot certify Synology ACLs, Linux ownership or durable rename.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from feature_phone_clank import observer_snapshot as export
from feature_phone_clank import observer_publication as publication
from feature_phone_clank.core.run_lock import RunLock
from feature_phone_clank.providers import sqlite as provider

REV = "3e6e19a2d5b7cb5004aab12145d099521a200c25"
ATTEMPT = "export-20260930T100000Z-0123456789abcdef"
ROOT_LINUX = os.name == "posix" and os.geteuid() == 0
linux_root = pytest.mark.skipif(not ROOT_LINUX, reason="requires real Linux root publisher boundary")


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_SOURCE_REVISION", REV)
    monkeypatch.setenv("FEATURE_PHONE_CLANK_CANONICAL_DEPLOYED_REVISION", REV)
    path = tmp_path / "state" / export.DB_NAME
    store = provider.SqliteStore(str(path))
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.db.execute("INSERT INTO collector_runs(source_key,started_at,finished_at,status) VALUES ('fixture',?,?, 'ok')", (now, now))
    store.db.execute("INSERT INTO notifications(provider,dedup_key,payload_json,status) VALUES ('discord','test','{}','pending')")
    store.db.commit()
    store.close()
    return path


def stage(tmp_path, name="stage"):
    p = tmp_path / name
    p.mkdir(mode=0o700)
    p.chmod(0o700)
    return p


def cli(source, output, *options):
    env = os.environ.copy()
    env["FEATURE_PHONE_CLANK_SOURCE_REVISION"] = REV
    env["FEATURE_PHONE_CLANK_CANONICAL_DEPLOYED_REVISION"] = REV
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    env["FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL"] = "SECRET_SENTINEL_DO_NOT_LEAK"
    return subprocess.run([sys.executable, "-m", "feature_phone_clank.cli", "--db", str(source),
                           "observer-snapshot", "--output", str(output), *options],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=15)


def census(path):
    con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    try:
        return (list(con.execute("SELECT sql FROM sqlite_master ORDER BY name")),
                list(con.execute("SELECT * FROM collector_runs")),
                list(con.execute("SELECT * FROM notifications")), con.total_changes)
    finally:
        con.close()


def test_A_current_and_H_I_wal_absent(source, tmp_path):
    # Do not open a preliminary RO census: that itself can create child
    # coordination sidecars. This idle fixture has no concurrent writer.
    before = export.digest(source)
    assert not Path(str(source) + "-wal").exists()
    out = stage(tmp_path)
    process = cli(source, out)
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout)["status"] == "VERIFIED_PRIVATE"
    metadata = json.loads((out / export.META_NAME).read_text())
    assert metadata["schema_version"] == 7
    assert metadata["source_access"]["total_changes"] == 0
    assert metadata["source_access"]["sidecars_before"]["-wal"] is False
    assert metadata["child_as_of"] != metadata["export_completed_at"]
    assert metadata["child_as_of_clock"] == "NATIVE_RUN_ROW_UTC"
    assert export.digest(out / export.DB_NAME) == metadata["sha256"]
    assert (out / export.DB_NAME).read_bytes()[18:20] == b"\x01\x01"
    assert not any(export.sidecars(out / export.DB_NAME).values())
    assert export.digest(source) == before
    assert census(source) == census(out / export.DB_NAME)
    assert not (out / export.SEAL_NAME).exists()
    assert "SECRET_SENTINEL" not in process.stdout + process.stderr + (out / export.META_NAME).read_text()


@pytest.mark.parametrize("case", ["old", "newer", "unknown", "empty", "partial"])
def test_B_C_incompatible_never_migrates(source, tmp_path, case):
    con = sqlite3.connect(source)
    if case == "old":
        con.execute("DELETE FROM schema_migrations WHERE version>6")
    elif case == "newer":
        con.execute("INSERT INTO schema_migrations(version) VALUES (999)")
    elif case == "unknown":
        con.execute("DROP TABLE schema_migrations")
    elif case == "empty":
        con.close()
        source = tmp_path / "empty.db"
        con = sqlite3.connect(source)
    else:
        con.execute("DROP TABLE observation_occurrences")
    con.commit()
    con.close()
    before = export.digest(source)
    out = stage(tmp_path)
    process = cli(source, out)
    assert process.returncode == 3
    assert json.loads(process.stdout)["stage"] == "SCHEMA_INCOMPATIBLE"
    assert not any(out.iterdir())
    assert export.digest(source) == before


def test_D_absent_never_bootstraps(tmp_path):
    missing = tmp_path / "missing" / export.DB_NAME
    process = cli(missing, stage(tmp_path))
    assert process.returncode == 1
    assert json.loads(process.stdout)["stage"] == "SOURCE_ADMISSION_FAILED"
    assert not missing.parent.exists()


def test_E_lock_busy_real_process(source, tmp_path):
    with RunLock.acquire(source.parent / "feature-phone-clank.lock"):
        start = time.monotonic()
        process = cli(source, stage(tmp_path))
        assert process.returncode == 2
        assert json.loads(process.stdout)["stage"] == "LOCK_BUSY"
        assert time.monotonic() - start < 10


def test_F_crash_releases_same_collection_lock(source, tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-c",
        "from feature_phone_clank.core.run_lock import RunLock; import sys,time; "
        "l=RunLock.acquire(sys.argv[1]); print('LOCKED',flush=True); time.sleep(60)",
        str(source.parent / "feature-phone-clank.lock")], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        assert cli(source, stage(tmp_path, "busy")).returncode == 2
        child.kill()
        child.wait(timeout=10)
        assert cli(source, stage(tmp_path, "after-crash")).returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_G_H_concurrent_valid_delivery_writer(source, tmp_path, monkeypatch):
    # A distinct real SQLite process writes notification state during the
    # backup progress callback. It does NOT take the collection lock, just
    # like the deployed delivery path. The copy must be one consistent view.
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE notifications SET attempts=1,status='sent'")
    writer.commit()
    assert Path(str(source) + "-wal").exists()
    original = export.online_backup_to_partial
    observed = []
    def backup(con, partial, **kwargs):
        callback = kwargs["progress"]
        fired = False
        def progress(status, remaining, total):
            nonlocal fired
            if not fired:
                fired = True
                p = subprocess.run([sys.executable, "-c",
                    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=2); "
                    "c.execute(\"UPDATE notifications SET attempts=2,status='sent'\"); c.commit(); c.close()",
                    str(source)], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
                observed.append(p.returncode)
            callback(status, remaining, total)
        kwargs["progress"] = progress
        return original(con, partial, **kwargs)
    monkeypatch.setattr(export, "online_backup_to_partial", backup)
    out = stage(tmp_path)
    try:
        export.export_snapshot(source, out)
        assert observed == [0]
        m = json.loads((out / export.META_NAME).read_text())
        assert m["source_access"]["sidecars_before"]["-wal"] is True
        assert m["source_access"]["total_changes"] == 0
        with sqlite3.connect(out / export.DB_NAME) as copy:
            attempts, status = copy.execute("SELECT attempts,status FROM notifications").fetchone()
            assert (attempts, status) in [(1, "sent"), (2, "sent")]
            assert list(copy.execute("PRAGMA integrity_check")) == [("ok",)]
            assert not list(copy.execute("PRAGMA foreign_key_check"))
        assert writer.execute("SELECT attempts FROM notifications").fetchone()[0] == 2
    finally:
        writer.close()


def test_J_corrupt_source_safe_failure(source, tmp_path):
    source.write_bytes(b"NOT_SQLITE_SECRET_PAYLOAD")
    p = cli(source, stage(tmp_path))
    assert p.returncode == 1
    assert json.loads(p.stdout)["stage"] == "SOURCE_ADMISSION_FAILED"
    assert "SECRET_PAYLOAD" not in p.stdout + p.stderr


def test_J_copy_integrity_failure(source, tmp_path, monkeypatch):
    def bad(*args, **kwargs):
        raise provider.BackupIntegrityError("SECRET_SQL_OR_URL")
    monkeypatch.setattr(export, "online_backup_to_partial", bad)
    with pytest.raises(export.ExportFailure) as caught:
        export.export_snapshot(source, stage(tmp_path))
    assert caught.value.stage == "INTEGRITY_FAILED"
    assert "SECRET" not in json.dumps(caught.value.envelope())


def test_K_fk_failure_has_nonzero_process_exit(source, tmp_path):
    con = sqlite3.connect(source)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO run_errors(run_id,message) VALUES (99999,'SECRET_PAYLOAD')")
    con.commit()
    con.close()
    p = cli(source, stage(tmp_path))
    assert p.returncode == 1
    assert json.loads(p.stdout)["stage"] == "FK_FAILED"
    assert "SECRET_PAYLOAD" not in p.stdout + p.stderr


def test_L_M_N_private_no_clobber_or_public_success(source, tmp_path):
    out = stage(tmp_path)
    (out / (export.DB_NAME + ".partial")).write_bytes(b"evidence")
    p = cli(source, out)
    assert p.returncode == 1
    assert json.loads(p.stdout)["stage"] == "PUBLICATION_FAILED"
    assert (out / (export.DB_NAME + ".partial")).read_bytes() == b"evidence"
    assert not (out / export.META_NAME).exists()
    assert not (out / export.SEAL_NAME).exists()


def test_R_missing_native_clock_is_not_export_time(source, tmp_path):
    con = sqlite3.connect(source)
    con.execute("DELETE FROM collector_runs")
    con.commit()
    con.close()
    out = stage(tmp_path)
    assert cli(source, out).returncode == 0
    m = json.loads((out / export.META_NAME).read_text())
    assert m["child_as_of"] is None
    assert m["child_as_of_clock"] == "UNAVAILABLE"
    assert m["child_as_of_unavailable_reason"] == "NO_NATIVE_RUN_CLOCK"
    assert m["export_completed_at"]


def test_S_T_U_no_store_collector_sender_continuity_or_migration(source, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a mutating store/collector/sender/continuity path was invoked")
    monkeypatch.setattr(provider.SqliteStore, "__init__", forbidden)
    from feature_phone_clank import cli as cli_module
    for name in ["cmd_run", "cmd_run_experimental", "cmd_deliver", "cmd_test_notify", "cmd_backup", "cmd_continuity"]:
        monkeypatch.setattr(cli_module, name, forbidden)
    before_modules = set(sys.modules)
    assert cli_module.main(["--db", str(source), "observer-snapshot", "--output", str(stage(tmp_path))]) == 0
    imported = set(sys.modules) - before_modules
    assert not any("collectors" in name or "messenger" in name for name in imported)


def test_U_V_W_X_source_mechanical_authorizer(source):
    before = census(source)
    con = export.read_only(source)
    try:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        for sql in ["DELETE FROM collector_runs", "CREATE TABLE hacked(x)", "PRAGMA wal_checkpoint(TRUNCATE)",
                    "PRAGMA journal_mode=DELETE", "ATTACH ':memory:' AS x", "PRAGMA query_only=OFF"]:
            with pytest.raises(sqlite3.DatabaseError):
                con.execute(sql)
        assert con.total_changes == 0
    finally:
        con.close()
    assert census(source) == before


def test_X_no_live_immutable_or_rw_connection(source, tmp_path, monkeypatch):
    original = sqlite3.connect
    opened = []
    def connect(path, *args, **kwargs):
        opened.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    export.export_snapshot(source, stage(tmp_path))
    source_opens = [p for p in opened if source.as_posix() in p]
    assert source_opens and all(p.endswith("?mode=ro") for p in source_opens)
    assert all("immutable=" not in p and "nolock=" not in p for p in opened)


def test_Y_timeout_and_no_lock_bypass_real_exit(source, tmp_path):
    p = cli(source, stage(tmp_path), "--timeout-seconds", "0.000000001")
    assert p.returncode == 4
    assert json.loads(p.stdout)["stage"] == "TIMEOUT"
    p = cli(source, stage(tmp_path, "bypass"), "--no-lock")
    assert p.returncode != 0


def test_failure_envelope_never_raw_exception_sql_or_paths():
    exc = sqlite3.OperationalError("SELECT SECRET_WEBHOOK FROM /private/path")
    exc.sqlite_errorcode = 14
    exc.sqlite_errorname = "SQLITE_CANTOPEN"
    safe = export.ExportFailure("BACKUP_FAILED", "OPERATION_FAILED", exc).envelope()
    assert safe["sqlite_errorcode"] == 14
    assert safe["sqlite_errorname"] == "SQLITE_CANTOPEN"
    assert "SECRET" not in json.dumps(safe) and "private" not in json.dumps(safe)


def test_canonical_and_exporter_revisions_are_not_conflated(source, tmp_path, monkeypatch):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_SOURCE_REVISION", "a" * 40)
    out = stage(tmp_path)
    export.export_snapshot(source, out)
    m = json.loads((out / export.META_NAME).read_text())
    assert m["deployed_revision"] == REV
    assert m["exporter_revision"] == "a" * 40
    assert m["source_revision"] is None


def test_metadata_has_no_extra_or_unbounded_fields(source, tmp_path):
    out = stage(tmp_path)
    export.export_snapshot(source, out)
    m = json.loads((out / export.META_NAME).read_text())
    m["webhook"] = "SECRET"
    with pytest.raises(export.ExportFailure):
        export.validate_metadata(m)


def test_source_and_staging_cannot_alias(source):
    with pytest.raises(export.ExportFailure) as caught:
        export.export_snapshot(source, source.parent)
    assert caught.value.code == "STAGING_ALIASES_SOURCE_DIRECTORY"


@pytest.fixture
def publisher_root(source, tmp_path, monkeypatch):
    if not ROOT_LINUX:
        pytest.skip("requires Linux ownership and renameat2")
    # Fixture authority is explicitly its fixture path, never live NAS state.
    monkeypatch.setattr(export, "CANONICAL_SOURCE", source.as_posix())
    root = tmp_path / "publication"
    root.mkdir(mode=0o750)
    os.chown(root, 0, 10001)
    for name, mode, gid in [("staging", 0o700, 0), ("failed", 0o700, 0), ("accepted", 0o550, 10001)]:
        p = root / name
        p.mkdir(mode=mode)
        os.chown(p, 0, gid)
        p.chmod(mode)
    pins = root / "pins.json"
    pins.write_text(json.dumps({"policy_version": "1.0", "attempt_ids": []}))
    os.chown(pins, 0, 10001)
    pins.chmod(0o440)
    output = stage(root / "staging", ATTEMPT)
    export.export_snapshot(source, output)
    # Hermetic producer runs as QA root; then hands off a stopped UID10001
    # attempt. NAS proof separately executes the actual non-root producer.
    os.chown(output, 10001, 10001)
    for p in output.iterdir():
        os.chown(p, 10001, 10001)
    monkeypatch.setattr(publication, "PUBLICATION_ROOT", root)
    return root


@linux_root
def test_A_P_Q_atomic_publication_seal_read_only_and_historical(publisher_root):
    seal = publication.publish_attempt(ATTEMPT, REV)
    assert seal["status"] == "SUCCESS"
    final = publisher_root / "accepted" / ATTEMPT
    assert final.stat().st_uid == 0 and final.stat().st_gid == 10001
    assert final.stat().st_mode & 0o777 == 0o550
    assert all(p.stat().st_mode & 0o777 == 0o440 and p.stat().st_uid == 0 for p in final.iterdir())
    assert publication.verify_publication(final)["freshness"] == "CURRENT"
    assert publication.verify_publication(final, max_native_age_seconds=0)["freshness"] == "HISTORICAL"
    assert not (publisher_root / "staging" / ATTEMPT).exists()


@linux_root
@pytest.mark.parametrize("attack", ["partial", "swap", "extra", "symlink", "hardlink", "metadata_path", "revision"])
def test_N_O_reject_partial_swapped_or_unsafe_attempt(publisher_root, attack):
    output = publisher_root / "staging" / ATTEMPT
    if attack == "partial":
        (output / export.META_NAME).unlink()
    elif attack == "swap":
        with (output / export.DB_NAME).open("ab") as stream:
            stream.write(b"swapped")
    elif attack == "extra":
        (output / "unexpected").write_text("SECRET")
    elif attack == "symlink":
        (output / export.DB_NAME).unlink()
        (output / export.DB_NAME).symlink_to("/etc/passwd")
    elif attack == "hardlink":
        os.link(output / export.DB_NAME, publisher_root / "duplicate")
    elif attack == "metadata_path":
        p = output / export.META_NAME
        m = json.loads(p.read_text())
        m["artifact_path"] = "../../canonical.db"
        p.write_text(json.dumps(m))
    with pytest.raises(export.ExportFailure):
        publication.publish_attempt(ATTEMPT, "0" * 40 if attack == "revision" else REV)
    assert not (publisher_root / "accepted" / ATTEMPT).exists()


@linux_root
def test_M_atomic_noreplace_even_empty_existing_directory(publisher_root):
    accepted = publisher_root / "accepted"
    accepted.chmod(0o750)
    (accepted / ATTEMPT).mkdir(mode=0o550)
    accepted.chmod(0o550)
    with pytest.raises(export.ExportFailure, match="PUBLICATION_FAILED"):
        publication.publish_attempt(ATTEMPT, REV)
    assert not any((accepted / ATTEMPT).iterdir())


@linux_root
def test_P_corrupt_published_artifact_refused(publisher_root):
    publication.publish_attempt(ATTEMPT, REV)
    final = publisher_root / "accepted" / ATTEMPT
    with (final / export.DB_NAME).open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(export.ExportFailure):
        publication.verify_publication(final)


@linux_root
def test_L_fsync_failure_after_rename_never_readable_success(publisher_root, monkeypatch):
    original = publication.fsync_directory
    fired = False
    def fail_once(path):
        nonlocal fired
        if path == publisher_root / "accepted" and (path / ATTEMPT).exists() and not fired:
            fired = True
            raise OSError("SECRET_PATH")
        return original(path)
    monkeypatch.setattr(publication, "fsync_directory", fail_once)
    with pytest.raises(OSError):
        publication.publish_attempt(ATTEMPT, REV)
    assert fired
    assert not (publisher_root / "accepted" / ATTEMPT).exists()
    assert (publisher_root / "failed" / ATTEMPT).stat().st_mode & 0o777 == 0o700


@linux_root
def test_retention_capacity_fails_closed_without_pruning(publisher_root, monkeypatch):
    before = sorted(p.name for p in (publisher_root / "staging").iterdir())
    monkeypatch.setattr(publication, "MAX_ACCEPTED", 0)
    with pytest.raises(export.ExportFailure) as caught:
        publication.publish_attempt(ATTEMPT, REV)
    assert caught.value.code == "ACCEPTED_CAPACITY_EXHAUSTED"
    assert sorted(p.name for p in (publisher_root / "staging").iterdir()) == before
    assert not any((publisher_root / "accepted").iterdir())


@linux_root
def test_publisher_lock_is_nonblocking(publisher_root):
    with RunLock.acquire(publisher_root / "publisher.lock"):
        with pytest.raises(export.ExportFailure) as caught:
            publication.publish_attempt(ATTEMPT, REV)
    assert caught.value.stage == "LOCK_BUSY"


@linux_root
def test_retention_pins_preserve_old_evidence_without_pruning(publisher_root):
    publication.publish_attempt(ATTEMPT, REV)
    final = publisher_root / "accepted" / ATTEMPT
    seal = json.loads((final / export.SEAL_NAME).read_text())
    seal["published_at"] = "2020-01-01T00:00:00Z"
    (final / export.SEAL_NAME).write_text(json.dumps(seal))
    with pytest.raises(export.ExportFailure) as caught:
        publication._capacity(publisher_root, 1)
    assert caught.value.code == "RETENTION_OWNER_ACTION_REQUIRED"
    (publisher_root / "pins.json").write_text(json.dumps({"policy_version": "1.0", "attempt_ids": [ATTEMPT]}))
    publication._capacity(publisher_root, 1)
    assert final.is_dir()


@linux_root
def test_L_Y_finalizer_publication_failure_real_exit(publisher_root):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    code = '''import sys,json
from pathlib import Path
from feature_phone_clank import observer_publication as p, observer_snapshot as s
p.PUBLICATION_ROOT=Path(sys.argv[1])
s.CANONICAL_SOURCE=json.loads((p.PUBLICATION_ROOT/'staging'/sys.argv[2]/s.META_NAME).read_text())['canonical_source_path']
original=p.fsync_directory
def injected(path):
    if path == p.PUBLICATION_ROOT/'accepted' and (path/sys.argv[2]).exists():
        raise OSError('SECRET_SQL_OR_WEBHOOK')
    original(path)
p.fsync_directory=injected
sys.exit(p.main(sys.argv[2:]))
'''
    process = subprocess.run([sys.executable, "-c", code, str(publisher_root), ATTEMPT, REV],
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert process.returncode == 1
    assert json.loads(process.stdout)["stage"] == "PUBLICATION_FAILED"
    assert "SECRET" not in process.stdout + process.stderr
    assert not (publisher_root / "accepted" / ATTEMPT).exists()


@linux_root
def test_success_has_no_filesystem_operation_after_reader_grant(publisher_root, monkeypatch):
    original = publication.fsync_directory
    def guarded(path):
        final = publisher_root / "accepted" / ATTEMPT
        assert not (final.exists() and final.stat().st_mode & 0o777 == 0o550)
        return original(path)
    monkeypatch.setattr(publication, "fsync_directory", guarded)
    assert publication.publish_attempt(ATTEMPT, REV)["status"] == "SUCCESS"


def test_finalizer_cannot_accept_arbitrary_paths_or_commands():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    p = subprocess.run([sys.executable, "-m", "feature_phone_clank.observer_publication", "../../state", REV],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, env=env)
    assert p.returncode == 1
    assert json.loads(p.stdout)["code"] == "INVALID_ATTEMPT_OR_REVISION"
    assert "../../state" not in p.stdout + p.stderr
