from __future__ import annotations

import json
import os

import pytest

from feature_phone_clank.core.run_lock import LockError, RunLock


def test_acquire_and_release_keeps_readable_diagnostic_marker(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock = RunLock.acquire(lock_path)
    assert lock_path.exists()
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == os.getpid()
    assert metadata["lock_authority"] == "os_advisory_lock"
    lock.release()
    assert lock_path.exists()
    assert not lock._held


def test_second_acquire_blocked_while_first_grant_is_live(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock = RunLock.acquire(lock_path)
    try:
        with pytest.raises(LockError, match="kernel lock"):
            RunLock.acquire(lock_path)
    finally:
        lock.release()


def test_metadata_from_dead_or_reused_pid_cannot_block_acquisition(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": 1}), encoding="utf-8")
    lock = RunLock.acquire(lock_path)
    try:
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        lock.release()


def test_stale_metadata_cannot_authorize_reclaim_of_active_grant(tmp_path):
    lock_path = tmp_path / "run.lock"
    first = RunLock.acquire(lock_path)
    try:
        # Diagnostic fields may be stale or reused while the real grant remains.
        lock_path.write_text(json.dumps({"pid": 999999, "started_at": 1}), encoding="utf-8")
        with pytest.raises(LockError):
            RunLock.acquire(lock_path)
    finally:
        first.release()
    recovered = RunLock.acquire(lock_path)
    recovered.release()


def test_release_permits_later_acquisition(tmp_path):
    lock_path = tmp_path / "run.lock"
    first = RunLock.acquire(lock_path)
    first.release()
    second = RunLock.acquire(lock_path)
    second.release()


def test_context_manager_releases_on_exit_even_on_failure(tmp_path):
    lock_path = tmp_path / "run.lock"
    with pytest.raises(RuntimeError):
        with RunLock.acquire(lock_path):
            raise RuntimeError("synthetic failure")
    recovered = RunLock.acquire(lock_path)
    recovered.release()

