"""Stage 4: event -> notification eligibility, outbox durability, retry
safety, deduplication, and delivery/collection independence.

No test here ever contacts a real Discord endpoint — `DiscordNotifier`
always takes a fake `sender` callable instead of the real `_post_webhook`.
Notifications reference real, pipeline-persisted events wherever the outbox
schema's FK to `events` matters (i.e. everywhere except the explicit
`test-notify` path, which is event_id=NULL by design).
"""

from __future__ import annotations

from feature_phone_clank.core.models import ChangeType
from feature_phone_clank.core.notifications import initial_status, should_notify
from feature_phone_clank.core.pipeline import ClassificationTransition, process_run
from feature_phone_clank.providers.discord import DiscordNotifier, MAX_ATTEMPTS, build_embed

from helpers import make_discovery

SOURCE_KEY = "test-source"


class FakeSender:
    """Records every payload it was asked to send; `ok` controls the
    canned result for every call."""

    def __init__(self, ok: bool = True, error: str = "boom"):
        self.ok = ok
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, webhook_url: str, payload: dict) -> tuple[bool, str | None]:
        self.calls.append(payload)
        return (True, None) if self.ok else (False, self.error)


class Capture:
    """Stand-in `notify` callback: records (event, event_id) instead of
    building a payload, so a test can inspect exactly what the pipeline
    decided happened before feeding it to a real DiscordNotifier."""

    def __init__(self):
        self.events: list[tuple[object, int]] = []

    def __call__(self, event, event_id):
        self.events.append((event, event_id))


def _ensure_source(store, source_key=SOURCE_KEY):
    return store.ensure_source(source_key, "TestCo", "catalogue", "en_int", "https://example.test", {})


def real_field_changed_event(store):
    """Baseline a product, then change one meaningful field — the simplest
    real, notify-eligible event the pipeline produces."""
    source_id = _ensure_source(store)
    cap = Capture()
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY, fields={"usb-connection": "Micro-USB"})],
                [], is_baseline=True, notify=cap)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY, fields={"usb-connection": "USB-C"})],
                [], is_baseline=False, notify=cap)
    assert len(cap.events) == 1
    return cap.events[0]  # (Event, event_id)


def real_classification_changed_event(store):
    """A real, notify-suppressed-by-default event: an already-catalogued
    product demoted out of feature_phone classification."""
    source_id = _ensure_source(store)
    cap = Capture()
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY)],
                [], is_baseline=True, notify=cap)
    transitions = [ClassificationTransition("phone-1", prior="feature_phone", new="ambiguous")]
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY)],
                transitions, is_baseline=False, notify=cap)
    assert len(cap.events) == 1
    assert cap.events[0][0].event_type == ChangeType.CLASSIFICATION_CHANGED
    return cap.events[0]


# -- eligibility policy ----------------------------------------------------

def test_notify_eligible_event_types():
    from feature_phone_clank.core.models import AlertLevel, ChangeType as CT, Confidence, Event

    def ev(t):
        return Event(source_key=SOURCE_KEY, product_key=f"{SOURCE_KEY}:x", manufacturer="TestCo",
                     model="X", url="https://example.test/x", event_type=t,
                     alert_level=AlertLevel.HIGH, confidence=Confidence.HIGH)

    for t in (CT.NEW_PRODUCT, CT.FIELD_CHANGED, CT.PRODUCT_REMOVED,
              CT.SPECS_BECAME_AVAILABLE, CT.IDENTITY_ANOMALY):
        assert should_notify(ev(t)) is True
        assert initial_status(ev(t)) == "pending"

    for t in (CT.SPECS_BECAME_UNAVAILABLE, CT.CLASSIFICATION_CHANGED,
              CT.REGIONAL_VARIANT, CT.AVAILABILITY_CHANGED, CT.SOURCE_DEGRADED):
        assert should_notify(ev(t)) is False
        assert initial_status(ev(t)) == "suppressed"


# -- 1: event queues exactly one notification ------------------------------

def test_new_product_event_enqueues_exactly_one_notification(store):
    source_id = _ensure_source(store)
    notifier = DiscordNotifier(store, webhook_url=None)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY)],
                [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-1", source_key=SOURCE_KEY),
                 make_discovery("phone-2", source_key=SOURCE_KEY)],
                [], is_baseline=False, notify=notifier.enqueue)
    assert store.notification_counts("discord") == {"pending": 1}


# -- 2: suppressed event queues none for delivery ---------------------------

def test_suppressed_event_never_delivered(store):
    event, event_id = real_classification_changed_event(store)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=FakeSender(ok=True))
    notifier.enqueue(event, event_id)
    assert store.notification_counts("discord") == {"suppressed": 1}
    result = notifier.drain()
    assert result == {"sent": 0, "failed": 0, "remaining": 0}


# -- 3: failed delivery persists error / stays retryable ---------------------

def test_failed_delivery_persists_error_and_stays_pending(store):
    event, event_id = real_field_changed_event(store)
    sender = FakeSender(ok=False, error="HTTP 500: boom")
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    result = notifier.drain()
    assert result["failed"] == 0 and result["sent"] == 0
    row = store.notifications_by_status("pending", "discord")[0]
    assert row["last_error"] == "HTTP 500: boom"
    assert row["attempts"] == 1


# -- 4: retry succeeds --------------------------------------------------------

def test_retry_after_failure_eventually_delivers_exactly_once(store):
    event, event_id = real_field_changed_event(store)
    sender = FakeSender(ok=False)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    notifier.drain()
    assert store.notification_counts("discord") == {"pending": 1}

    sender.ok = True
    result = notifier.drain()
    assert result["sent"] == 1
    assert store.notification_counts("discord") == {"sent": 1}
    assert len(sender.calls) == 2  # one failed attempt, one successful


# -- 5 / 9: delivered event is not re-sent; failure isolated from event ------

def test_delivered_notification_not_resent(store):
    event, event_id = real_field_changed_event(store)
    sender = FakeSender(ok=True)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    notifier.drain()
    assert len(sender.calls) == 1
    notifier.drain()  # nothing pending now
    assert len(sender.calls) == 1


# -- 6: concurrent/repeated delivery cannot duplicate -------------------------

def test_repeated_enqueue_same_event_does_not_duplicate(store):
    event, event_id = real_field_changed_event(store)
    notifier = DiscordNotifier(store, webhook_url=None)
    notifier.enqueue(event, event_id)
    notifier.enqueue(event, event_id)  # e.g. process restarted mid-run, re-derives same event
    assert store.notification_counts("discord") == {"pending": 1}


def test_max_attempts_becomes_terminal_failed(store):
    event, event_id = real_field_changed_event(store)
    sender = FakeSender(ok=False)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    for _ in range(MAX_ATTEMPTS):
        notifier.drain()
    assert store.notification_counts("discord") == {"failed": 1}
    calls_before = len(sender.calls)
    notifier.drain()  # terminally failed rows are never retried automatically
    assert len(sender.calls) == calls_before


def test_requeue_failed_allows_retry(store):
    event, event_id = real_field_changed_event(store)
    sender = FakeSender(ok=False)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    for _ in range(MAX_ATTEMPTS):
        notifier.drain()
    assert store.notification_counts("discord") == {"failed": 1}

    assert store.requeue_failed_notifications("discord") == 1
    sender.ok = True
    result = notifier.drain()
    assert result["sent"] == 1


# -- 7: missing webhook/config fails safely -----------------------------------

def test_missing_webhook_does_not_crash_and_leaves_pending(store):
    event, event_id = real_field_changed_event(store)
    notifier = DiscordNotifier(store, webhook_url=None)
    notifier.enqueue(event, event_id)
    result = notifier.drain()
    assert result == {"sent": 0, "failed": 0, "remaining": 1}
    assert store.notifications_by_status("pending", "discord")[0]["attempts"] == 0


# -- 8: notification failure does not mutate the event ------------------------

def test_notification_failure_does_not_mutate_event(store):
    event, event_id = real_field_changed_event(store)
    events_before = [dict(r) for r in store.recent_events()]

    sender = FakeSender(ok=False)
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=sender)
    notifier.enqueue(event, event_id)
    notifier.drain()

    events_after = [dict(r) for r in store.recent_events()]
    assert events_before == events_after


# -- notification failure does not degrade collector/source health -----------

def test_delivery_failure_does_not_affect_health(store, tmp_path):
    from feature_phone_clank import runtime_bridge

    event, event_id = real_field_changed_event(store)
    db_path = tmp_path / "test.db"  # matches conftest's store fixture db path
    health_before = runtime_bridge.as_jsonable(runtime_bridge.get_health(db_path))

    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=FakeSender(ok=False))
    notifier.enqueue(event, event_id)
    for _ in range(MAX_ATTEMPTS):
        notifier.drain()

    health_after = runtime_bridge.as_jsonable(runtime_bridge.get_health(db_path))
    assert health_before["operational_state"] == health_after["operational_state"]


# -- 10: test notification does not create a fake product event --------------

def test_test_notification_creates_no_event_or_product(store):
    notifier = DiscordNotifier(store, webhook_url="https://example.test/webhook", sender=FakeSender(ok=True))
    result = notifier.enqueue_test(note="field test")
    assert result["sent"] is True
    assert store.recent_events() == []
    assert store.active_product_count(SOURCE_KEY) == 0
    row = store.notification_by_dedup_key(result["dedup_key"])
    assert row["event_id"] is None
    assert row["dedup_key"].startswith("test:")


# -- duplicate protection end-to-end via the pipeline -------------------------

def test_pipeline_rerun_unchanged_state_no_duplicate_notification(store):
    source_id = _ensure_source(store)
    notifier = DiscordNotifier(store, webhook_url=None)
    d = make_discovery("phone-1", source_key=SOURCE_KEY)
    process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=True, notify=notifier.enqueue)
    stats1 = process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=False, notify=notifier.enqueue)
    stats2 = process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=False, notify=notifier.enqueue)
    assert stats1["events_created"] == 0  # unchanged observation
    assert stats2["events_created"] == 0
    assert store.notification_counts("discord") == {}


# -- embed rendering: compact, no JSON blobs, evidence-oriented --------------

def test_build_embed_is_compact_and_marks_fields(store):
    event, event_id = real_field_changed_event(store)
    payload = build_embed(event, event_id=event_id)
    embed = payload["embeds"][0]
    assert "FIELD CHANGE" in embed["title"]
    assert "usb-connection" in embed["description"]
    assert "Micro-USB" in embed["description"] and "USB-C" in embed["description"]
    assert f"Event ID: {event_id}" in embed["footer"]["text"]
    assert len(json_dump_size(payload)) < 2000  # compact, not a JSON blob dump


def json_dump_size(payload: dict) -> str:
    import json
    return json.dumps(payload)
