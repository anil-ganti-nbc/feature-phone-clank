from __future__ import annotations

import json
import os
import time

import pytest

from feature_phone_clank.core.run_lock import LockError, RunLock


def test_acquire_and_release(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock = RunLock.acquire(lock_path)
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()


def test_second_acquire_blocked_while_first_is_live(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock = RunLock.acquire(lock_path)
    try:
        with pytest.raises(LockError):
            RunLock.acquire(lock_path)
    finally:
        lock.release()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    lock_path = tmp_path / "run.lock"
    # A PID astronomically unlikely to be alive.
    dead_payload = {"pid": 999999, "started_at": time.time() - 3600}
    lock_path.write_text(json.dumps(dead_payload), encoding="utf-8")

    lock = RunLock.acquire(lock_path)
    try:
        assert lock.pid == os.getpid()
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
    finally:
        lock.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "run.lock"
    with RunLock.acquire(lock_path) as lock:
        assert lock_path.exists()
    assert not lock_path.exists()
