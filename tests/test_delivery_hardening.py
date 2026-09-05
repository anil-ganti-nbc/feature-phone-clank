"""Delivery hardening regressions (activation, serialization, redaction).

Every test here uses a disposable database and a fake/local transport. No
test resolves, contacts, or requires a real webhook, and the fake secret
below is the canary for the redaction assertions — if it ever appears in a
returned error, a persisted `last_error` or a log record, that is the exact
leak this suite exists to catch.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
import requests

from feature_phone_clank.core.delivery_policy import (
    ACTIVATION_CUTOFF_KEY,
    HELD_BEFORE_CUTOFF,
    HELD_POLICY_UNREADABLE,
    HELD_UNKNOWN_EVENT_TIME,
    SEND,
    decide,
    load_policy,
)
from feature_phone_clank.core.pipeline import ClassificationTransition, process_run
from feature_phone_clank.providers.discord import (
    DiscordNotifier,
    _error_category,
    _post_webhook,
    _status_category,
    delivery_lock_path,
)
from feature_phone_clank.providers.sqlite import SqliteStore

from helpers import make_discovery

# A fake that is unmistakably secret-shaped, including a token segment.
FAKE_WEBHOOK = "https://discord.test/api/webhooks/123456789/SUPERSECRETTOKENvalue-do-not-leak"
FAKE_TOKEN = "SUPERSECRETTOKENvalue-do-not-leak"


# ----------------------------------------------------------------- fixtures


def _store(tmp_path, name="fph.db"):
    return SqliteStore(str(tmp_path / name))


def _seed_event(store, *, detected_at: datetime | None = None, slug="phone-1", source_key="test-source"):
    """Create one genuinely notify-eligible event through the real pipeline.

    Uses the production path (baseline, then a real field change) rather
    than hand-inserting rows, so these regressions exercise the same
    persistence and dedup behaviour a collection run does. `detected_at`
    only rewrites the stored timestamp afterwards, which is what the
    activation-cutoff cases need and cannot get from a live clock.
    """
    source_id = store.ensure_source(source_key, "TestCo", "catalogue", "en_int", "https://example.test", {})
    captured: list[tuple[object, int]] = []
    process_run(store, source_key, source_id,
                [make_discovery(slug, source_key=source_key, fields={"usb-connection": "Micro-USB"})],
                [], is_baseline=True, notify=lambda e, i: captured.append((e, i)))
    process_run(store, source_key, source_id,
                [make_discovery(slug, source_key=source_key, fields={"usb-connection": "USB-C"})],
                [], is_baseline=False, notify=lambda e, i: captured.append((e, i)))
    assert len(captured) == 1, captured
    event, event_id = captured[0]
    if detected_at is not None:
        store.db.execute("UPDATE events SET detected_at=? WHERE id=?",
                         (detected_at.isoformat(), event_id))
        store.db.commit()
    return event, event_id


class FakeSender:
    """Records every call. Returns whatever shape the test asks for."""

    def __init__(self, ok=True, error=None, retry_after=None, three_tuple=False, status_seq=None):
        self.ok, self.error, self.retry_after = ok, error, retry_after
        self.three_tuple, self.status_seq = three_tuple, list(status_seq or [])
        self.calls: list[dict] = []

    def __call__(self, url, payload):
        self.calls.append({"url": url, "payload": payload})
        if self.status_seq:
            ok, err, retry = self.status_seq.pop(0)
            return (ok, err, retry)
        if self.three_tuple:
            return (self.ok, self.error, self.retry_after)
        return (self.ok, self.error)


# --------------------------------------------- 1: overlapping process safety


OVERLAP_CHILD = textwrap.dedent(
    """
    import json, sys, time
    from feature_phone_clank.providers.sqlite import SqliteStore
    from feature_phone_clank.providers.discord import DiscordNotifier

    db_path, marker = sys.argv[1], sys.argv[2]

    class SlowSender:
        def __call__(self, url, payload):
            # Hold the grant long enough that the parent genuinely overlaps.
            with open(marker, "a", encoding="utf-8") as fh:
                fh.write("SEND\\n")
            time.sleep(2.0)
            return (True, None)

    store = SqliteStore(db_path)
    try:
        notifier = DiscordNotifier(store, webhook_url="https://example.test/hook", sender=SlowSender())
        print(json.dumps(notifier.drain()))
    finally:
        store.close()
    """
)


def test_two_overlapping_processes_send_one_queued_row_exactly_once(tmp_path):
    """Regression 1: genuinely concurrent drains, one fake outbound request."""
    db_path = tmp_path / "overlap.db"
    marker = tmp_path / "sends.log"
    store = _store(tmp_path, "overlap.db")
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    store.db.commit()
    store.close()

    env = {**os.environ, "PYTHONPATH": str(tmp_path.parent)}
    child = subprocess.Popen(
        [sys.executable, "-c", OVERLAP_CHILD, str(db_path), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        # Wait until the child is actually inside its send, holding the grant.
        deadline = datetime.now() + timedelta(seconds=20)
        while datetime.now() < deadline and not marker.exists():
            pass
        assert marker.exists(), "child never reached its send"

        parent_store = SqliteStore(str(db_path))
        try:
            parent_sender = FakeSender(ok=True)
            parent = DiscordNotifier(parent_store, webhook_url=FAKE_WEBHOOK, sender=parent_sender)
            result = parent.drain()
        finally:
            parent_store.close()
    finally:
        out, err = child.communicate(timeout=60)

    assert child.returncode == 0, err
    # The overlapping sender refused explicitly, without sending.
    assert result["status"] == "delivery_busy"
    assert result["sent"] == 0
    assert parent_sender.calls == []
    # Exactly one real outbound request happened, from the lock holder.
    assert marker.read_text(encoding="utf-8").count("SEND") == 1

    verify = SqliteStore(str(db_path))
    try:
        rows = verify.notifications_by_status("sent", "discord")
        assert len(rows) == 1
        assert rows[0]["attempts"] == 1  # not double-incremented
    finally:
        verify.close()


def test_delivery_busy_does_not_increment_attempts_or_mutate_rows(tmp_path):
    """The refusal path must be genuinely inert."""
    from feature_phone_clank.core.run_lock import RunLock

    store = _store(tmp_path)
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    before = dict(store.notifications_by_status("pending", "discord")[0])

    holder = RunLock.acquire(delivery_lock_path(store.db_path))
    try:
        sender = FakeSender(ok=True)
        result = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=sender).drain()
    finally:
        holder.release()

    assert result["status"] == "delivery_busy"
    assert sender.calls == []
    after = dict(store.notifications_by_status("pending", "discord")[0])
    assert after == before
    store.close()


# ------------------------------------------ 2/3: atomicity and idempotence


def _detach_event(store, event_id):
    """Remove a persisted event (and its outbox row) while keeping the
    product, so the very same Event object can be re-recorded. Lets a test
    drive `_record_and_notify` against real pipeline output twice."""
    store.db.execute("DELETE FROM notifications WHERE event_id=?", (event_id,))
    store.db.execute("DELETE FROM events WHERE id=?", (event_id,))
    store.db.commit()


def test_enqueue_failure_rolls_back_event_and_replay_succeeds_once(tmp_path):
    """Regression 2: no committed event may survive a failed enqueue."""
    from feature_phone_clank.core import pipeline

    store = _store(tmp_path)
    event, event_id = _seed_event(store)
    _detach_event(store, event_id)
    assert store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0

    def exploding_notify(evt, evt_id):
        raise RuntimeError("enqueue failed")

    stats = {"events_created": 0}
    with pytest.raises(RuntimeError):
        pipeline._record_and_notify(store, event, exploding_notify, stats)

    # Neither half survived, and the failure was not counted as a creation.
    assert store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    assert store.db.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 0
    assert stats["events_created"] == 0

    # Replay repairs it — impossible when the event committed alone, because
    # dedup would have suppressed the second attempt's notify callback.
    recorded: list[int] = []
    pipeline._record_and_notify(store, event, lambda e, i: recorded.append(i), stats)
    assert stats["events_created"] == 1
    assert len(recorded) == 1
    assert store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    store.close()


def test_repeated_ingestion_does_not_duplicate_events_or_outbox_rows(tmp_path):
    """Regression 3: dedup still holds through the transaction refactor."""
    from feature_phone_clank.core import pipeline

    store = _store(tmp_path)
    event, event_id = _seed_event(store)
    _detach_event(store, event_id)
    notifier = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=FakeSender())

    stats = {"events_created": 0}
    for _ in range(4):
        pipeline._record_and_notify(store, event, notifier.enqueue, stats)

    assert store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1
    assert store.db.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 1
    store.close()


# ------------------------------------------------------- 4: secret redaction


@pytest.mark.parametrize(
    "exc,expected",
    [
        (requests.exceptions.ConnectionError(f"failed to reach {FAKE_WEBHOOK}"), "connection_error"),
        (requests.exceptions.ConnectTimeout(f"timed out to {FAKE_WEBHOOK}"), "connect_timeout"),
        (requests.exceptions.ReadTimeout(f"read timed out {FAKE_WEBHOOK}"), "read_timeout"),
        (requests.exceptions.MissingSchema(f"invalid url {FAKE_WEBHOOK}"), "invalid_webhook_url"),
        (requests.exceptions.InvalidURL(f"bad url {FAKE_WEBHOOK}"), "invalid_webhook_url"),
        (requests.exceptions.SSLError(f"tls fail {FAKE_WEBHOOK}"), "tls_error"),
        (requests.exceptions.RequestException(f"other {FAKE_WEBHOOK}"), "transport_error"),
    ],
)
def test_transport_errors_are_bounded_categories_without_secrets(exc, expected):
    category = _error_category(exc)
    assert category == expected
    assert FAKE_TOKEN not in category and "discord.test" not in category


def test_post_webhook_never_returns_the_url_on_transport_failure(monkeypatch):
    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool: failed establishing connection to {FAKE_WEBHOOK}"
        )

    monkeypatch.setattr(requests, "post", boom)
    ok, err, retry_after = _post_webhook(FAKE_WEBHOOK, {"embeds": []})
    assert ok is False and retry_after is None
    assert err == "connection_error"
    assert FAKE_TOKEN not in err and FAKE_WEBHOOK not in err


def test_http_error_bodies_are_not_persisted(monkeypatch):
    class Resp:
        status_code = 400
        headers: dict = {}
        # A hostile/echoing body: must never be retained.
        text = f"Bad Request for url {FAKE_WEBHOOK} token={FAKE_TOKEN}"

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    ok, err, _ = _post_webhook(FAKE_WEBHOOK, {"embeds": []})
    assert ok is False
    assert err == "http_400_client_error"
    assert FAKE_TOKEN not in err


def test_status_categories_are_bounded():
    assert _status_category(429) == "http_429_rate_limited"
    assert _status_category(401) == "http_unauthorized"
    assert _status_category(404) == "http_404_webhook_not_found"
    assert _status_category(500) == "http_500_server_error"


def test_no_secret_reaches_persisted_error_or_logs(tmp_path, caplog, monkeypatch):
    """Regression 4: returned error, persisted last_error and log records."""
    store = _store(tmp_path)
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    notifier = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=_post_webhook)
    notifier.enqueue(event, event_id)

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError(f"cannot connect to {FAKE_WEBHOOK}")

    monkeypatch.setattr(requests, "post", boom)
    with caplog.at_level(logging.DEBUG):
        result = notifier.drain()

    assert result["sent"] == 0
    row = store.notifications_by_status("pending", "discord")[0]
    assert row["last_error"] == "connection_error"

    haystack = json.dumps(result) + json.dumps(dict(row), default=str) + caplog.text
    assert FAKE_TOKEN not in haystack
    assert FAKE_WEBHOOK not in haystack
    store.close()


def test_test_notify_errors_are_redacted_too(tmp_path, monkeypatch):
    """Same redaction on the test-notify path, not only normal delivery."""
    store = _store(tmp_path)

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError(f"cannot connect to {FAKE_WEBHOOK}")

    monkeypatch.setattr(requests, "post", boom)
    notifier = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=_post_webhook)
    result = notifier.enqueue_test("hardening check")
    assert result["sent"] is False
    assert result["error"] == "connection_error"
    row = store.notification_by_dedup_key(result["dedup_key"])
    assert FAKE_TOKEN not in (row["last_error"] or "")
    store.close()


# --------------------------------------------------- 5: activation policy


def test_policy_decides_old_boundary_new_and_unknown_rows():
    """Regression 5, at the decision level."""
    cutoff = "2026-09-01T00:00:00Z"
    policy = load_policy(cutoff)
    assert decide("2026-08-31T23:59:59Z", policy) == HELD_BEFORE_CUTOFF
    assert decide("2026-09-01T00:00:00Z", policy) == SEND        # boundary is inclusive
    assert decide("2026-09-02T10:00:00Z", policy) == SEND
    assert decide(None, policy) == HELD_UNKNOWN_EVENT_TIME
    assert decide("not-a-timestamp", policy) == HELD_UNKNOWN_EVENT_TIME
    # An unreadable stored policy holds everything, including new rows.
    unreadable = load_policy("whenever-ish")
    assert unreadable.unreadable is True
    assert decide("2099-01-01T00:00:00Z", unreadable) == HELD_POLICY_UNREADABLE
    # No policy installed: historical behaviour retained.
    assert decide("2020-01-01T00:00:00Z", load_policy(None)) == SEND


def test_drain_holds_pre_cutoff_rows_and_sends_post_cutoff_rows(tmp_path):
    """Regression 5, end to end through drain()."""
    store = _store(tmp_path)
    old_event, old_id = _seed_event(
        store, detected_at=datetime(2026, 8, 25, 8, 29, 24, tzinfo=timezone.utc),
        slug="old",
    )
    new_event, new_id = _seed_event(
        store, detected_at=datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc),
        slug="new",
    )
    notifier = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=FakeSender(ok=True))
    notifier.enqueue(old_event, old_id)
    notifier.enqueue(new_event, new_id)
    store.policy_set(ACTIVATION_CUTOFF_KEY, "2026-09-01T00:00:00Z")

    sender = FakeSender(ok=True)
    result = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK, sender=sender).drain()

    assert result["sent"] == 1
    assert result["held"] == 1
    assert result["held_by_reason"] == {HELD_BEFORE_CUTOFF: 1}
    assert len(sender.calls) == 1
    # The held row is preserved exactly: still pending, never attempted.
    held = [r for r in store.notifications_by_status("pending", "discord")]
    assert len(held) == 1
    assert held[0]["attempts"] == 0
    assert held[0]["last_error"] is None
    store.close()


def test_explicit_include_held_replays_history(tmp_path):
    """Historical replay is possible, but only as its own explicit action."""
    store = _store(tmp_path)
    event, event_id = _seed_event(
        store, detected_at=datetime(2026, 8, 25, 8, 29, 24, tzinfo=timezone.utc),
    )
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    store.policy_set(ACTIVATION_CUTOFF_KEY, "2026-09-01T00:00:00Z")

    default_sender = FakeSender(ok=True)
    assert DiscordNotifier(store, FAKE_WEBHOOK, default_sender).drain()["held"] == 1
    assert default_sender.calls == []

    replay_sender = FakeSender(ok=True)
    replayed = DiscordNotifier(store, FAKE_WEBHOOK, replay_sender).drain(include_held=True)
    assert replayed["sent"] == 1
    assert len(replay_sender.calls) == 1
    store.close()


def test_unreadable_stored_policy_fails_closed_in_drain(tmp_path):
    store = _store(tmp_path)
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    store.policy_set(ACTIVATION_CUTOFF_KEY, "sometime last tuesday")

    sender = FakeSender(ok=True)
    result = DiscordNotifier(store, FAKE_WEBHOOK, sender).drain()
    assert result["sent"] == 0 and result["held"] == 1
    assert result["held_by_reason"] == {HELD_POLICY_UNREADABLE: 1}
    assert sender.calls == []
    store.close()


def test_row_without_event_is_held_when_a_cutoff_is_in_force(tmp_path):
    """Unknown provenance fails closed rather than being flushed."""
    store = _store(tmp_path)
    store.notification_put("discord", "orphan:1", {"embeds": []}, event_id=None, status="pending")
    store.policy_set(ACTIVATION_CUTOFF_KEY, "2026-09-01T00:00:00Z")

    sender = FakeSender(ok=True)
    result = DiscordNotifier(store, FAKE_WEBHOOK, sender).drain()
    assert result["held"] == 1
    assert result["held_by_reason"] == {HELD_UNKNOWN_EVENT_TIME: 1}
    assert sender.calls == []
    store.close()


# ------------------------------------------------- 6: preview is read-only


def test_preview_is_read_only_and_preserves_held_history(tmp_path):
    """Regression 6."""
    store = _store(tmp_path)
    old_event, old_id = _seed_event(
        store, detected_at=datetime(2026, 8, 25, 8, 29, 24, tzinfo=timezone.utc),
        slug="old",
    )
    new_event, new_id = _seed_event(
        store, detected_at=datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc),
        slug="new",
    )
    notifier = DiscordNotifier(store, webhook_url=FAKE_WEBHOOK)
    notifier.enqueue(old_event, old_id)
    notifier.enqueue(new_event, new_id)

    def snapshot():
        return [dict(r) for r in store.db.execute(
            "SELECT id, status, attempts, last_error, sent_at FROM notifications ORDER BY id"
        )]

    before = snapshot()
    preview = store.delivery_preview("discord")
    assert preview["counts_by_status"]["pending"] == 2
    assert preview["pending_attempts"]["never_attempted"] == 2
    assert preview["pending_event_time_span"]["oldest"].startswith("2026-08-25")
    assert preview["pending_event_time_span"]["newest"].startswith("2026-09-03")
    assert preview["pending_provenance_gaps"]["null_event_id"] == 0
    assert snapshot() == before  # nothing mutated

    from feature_phone_clank.cli import _delivery_preview

    full = _delivery_preview(store, "2026-09-01T00:00:00Z")
    assert full["proposed_cutoff_effect"]["would_send"] == 1
    assert full["proposed_cutoff_effect"]["would_hold"] == 1
    assert snapshot() == before  # still nothing mutated
    # A preview must never resolve or echo a secret.
    assert FAKE_TOKEN not in json.dumps(full, default=str)
    store.close()


# ------------------------------------- 7: experimental / baseline silence


def test_suppressed_events_are_never_sent(tmp_path):
    """Regression 7 (baseline/suppressed path stays silent)."""
    store = _store(tmp_path)
    source_key = "test-source"
    source_id = store.ensure_source(source_key, "TestCo", "catalogue", "en_int", "https://example.test", {})
    captured: list[tuple[object, int]] = []
    process_run(store, source_key, source_id,
                [make_discovery("phone-1", source_key=source_key)],
                [], is_baseline=True, notify=lambda e, i: captured.append((e, i)))
    # A demotion out of feature_phone classification: a real, retained event
    # that policy suppresses rather than pushes.
    process_run(store, source_key, source_id,
                [make_discovery("phone-1", source_key=source_key)],
                [ClassificationTransition("phone-1", prior="feature_phone", new="ambiguous")],
                is_baseline=False, notify=lambda e, i: captured.append((e, i)))
    assert len(captured) == 1
    event, event_id = captured[0]
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)

    sender = FakeSender(ok=True)
    result = DiscordNotifier(store, FAKE_WEBHOOK, sender).drain()
    assert sender.calls == []
    assert result["sent"] == 0
    assert store.notification_counts("discord") == {"suppressed": 1}
    store.close()


def test_collection_without_a_notifier_stays_silent(tmp_path):
    """A run wired with no notifier must persist events and send nothing."""
    from feature_phone_clank.core import pipeline

    store = _store(tmp_path)
    event, event_id = _seed_event(store, slug="silent")
    _detach_event(store, event_id)
    stats = {"events_created": 0}
    pipeline._record_and_notify(store, event, None, stats)
    assert store.db.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 0
    store.close()


# ------------------------------------------------------ 8: rate limiting


def test_429_defers_delivery_and_does_not_storm_the_queue(tmp_path):
    """Regression 8: one 429 stops the drain, defers durably, burns no attempt."""
    store = _store(tmp_path)
    for i in range(5):
        event, event_id = _seed_event(
            store, detected_at=datetime.now(timezone.utc),
            slug=f"phone-{i}", source_key=f"test-source-{i}",
        )
        DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)

    sender = FakeSender(status_seq=[(False, "http_429_rate_limited", 30)])
    result = DiscordNotifier(store, FAKE_WEBHOOK, sender).drain()

    # Exactly one request: the rest of the queue was not hammered.
    assert len(sender.calls) == 1
    assert result["status"] == "rate_limited"
    assert result["deferred"] == 1
    assert result["sent"] == 0 and result["failed"] == 0

    rows = {r["id"]: r for r in store.notifications_by_status("pending", "discord")}
    assert len(rows) == 5  # nothing failed out
    deferred = [r for r in rows.values() if r["not_before"]]
    assert len(deferred) == 1
    assert deferred[0]["attempts"] == 0  # a rate limit is not the row's fault
    assert deferred[0]["last_error"] is None

    # A drain during the window skips the deferred row rather than retrying it.
    second = FakeSender(ok=True)
    again = DiscordNotifier(store, FAKE_WEBHOOK, second).drain()
    assert again["deferred"] >= 1
    assert all(c["payload"] != json.loads(deferred[0]["payload_json"]) for c in second.calls)
    store.close()


def test_retry_after_is_parsed_and_clamped():
    from feature_phone_clank.providers.discord import (
        MAX_RETRY_AFTER_SECONDS,
        _parse_retry_after,
    )

    assert _parse_retry_after("30") == 30
    assert _parse_retry_after(" 12.5 ") == 12.5
    assert _parse_retry_after("99999") == MAX_RETRY_AFTER_SECONDS  # clamped
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None  # date form -> default
    assert _parse_retry_after("-5") is None
    assert _parse_retry_after(None) is None


def test_429_response_populates_retry_after(monkeypatch):
    class Resp:
        status_code = 429
        headers = {"Retry-After": "42"}
        text = "rate limited"

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    ok, err, retry_after = _post_webhook(FAKE_WEBHOOK, {"embeds": []})
    assert ok is False
    assert err == "http_429_rate_limited"
    assert retry_after == 42


def test_expired_defer_becomes_eligible_again(tmp_path):
    store = _store(tmp_path)
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    row = store.notifications_by_status("pending", "discord")[0]
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    store.defer_notification(row["id"], past)

    sender = FakeSender(ok=True)
    result = DiscordNotifier(store, FAKE_WEBHOOK, sender).drain()
    assert result["sent"] == 1
    assert len(sender.calls) == 1
    store.close()


# --------------------------------------------------------------- migration


def test_v6_migration_is_additive_and_preserves_delivery_history(tmp_path):
    """Existing sent/failed history and attempt counts survive the upgrade."""
    from feature_phone_clank.providers.sqlite.compatibility import EXPECTED_SCHEMA_VERSION

    path = tmp_path / "legacy.db"
    store = SqliteStore(str(path))
    event, event_id = _seed_event(store, detected_at=datetime.now(timezone.utc))
    DiscordNotifier(store, webhook_url=FAKE_WEBHOOK).enqueue(event, event_id)
    row = store.notifications_by_status("pending", "discord")[0]
    store.mark_notification(row["id"], "failed", "connection_error")
    store.close()

    # Simulate a v5 database: drop the v6 additions and unstamp the marker.
    import sqlite3

    con = sqlite3.connect(path)
    with con:
        con.execute("DROP TABLE delivery_policy")
        con.execute("ALTER TABLE notifications DROP COLUMN not_before")
        con.execute("DELETE FROM schema_migrations WHERE version=6")
    con.close()

    migrated = SqliteStore(str(path))
    try:
        assert migrated.schema_version() == EXPECTED_SCHEMA_VERSION
        after = migrated.notifications_by_status("failed", "discord")
        assert len(after) == 1
        assert after[0]["attempts"] == 1              # attempt history preserved
        assert after[0]["last_error"] == "connection_error"
        assert migrated.policy_get(ACTIVATION_CUTOFF_KEY) is None  # no cutoff invented
    finally:
        migrated.close()
