"""Stage 4 controlled end-to-end acceptance drill (brief section 13).

Runs entirely against a scratch SQLite DB (the `store` fixture's
`tmp_path`-backed database) and a fake Discord sender — the production
database is never opened by anything in this file. Each scenario is one
function so a failure names exactly which contract broke.
"""

from __future__ import annotations

from feature_phone_clank.core.pipeline import ClassificationTransition, process_run
from feature_phone_clank.providers.discord import DiscordNotifier, MAX_ATTEMPTS

from helpers import make_discovery

SOURCE_KEY = "test-source"


def _source(store):
    return store.ensure_source(SOURCE_KEY, "TestCo", "catalogue", "en_int", "https://example.test", {})


class FakeSender:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls = 0

    def __call__(self, webhook_url, payload):
        self.calls += 1
        return (True, None) if self.ok else (False, "simulated failure")


def test_drill_new_product(store):
    source_id = _source(store)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=FakeSender(ok=True))
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY)],
                [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY),
                 make_discovery("phone-b", source_key=SOURCE_KEY)],
                [], is_baseline=False, notify=notifier.enqueue)
    result = notifier.drain()
    assert result["sent"] == 1
    assert store.notification_counts("discord") == {"sent": 1}


def test_drill_field_change(store):
    source_id = _source(store)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=FakeSender(ok=True))
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY, fields={"usb-connection": "Micro-USB"})],
                [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY, fields={"usb-connection": "USB-C"})],
                [], is_baseline=False, notify=notifier.enqueue)
    result = notifier.drain()
    assert result["sent"] == 1


def test_drill_specs_become_available(store):
    source_id = _source(store)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=FakeSender(ok=True))
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY, spec_completeness="incomplete")],
                [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY, spec_completeness="complete",
                                 fields={"usb-connection": "USB-C"})],
                [], is_baseline=False, notify=notifier.enqueue)
    result = notifier.drain()
    assert result["sent"] == 1
    counts = store.notification_counts("discord")
    assert counts == {"sent": 1}  # single event, single notification


def test_drill_confirmed_removal(store):
    source_id = _source(store)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=FakeSender(ok=True))
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY)],
                [], is_baseline=True, notify=notifier.enqueue)
    # Three consecutive healthy absences confirm removal (REMOVAL_CONFIRMATION_THRESHOLD).
    for _ in range(3):
        process_run(store, SOURCE_KEY, source_id, [], [], is_baseline=False, notify=notifier.enqueue)
    result = notifier.drain()
    assert result["sent"] == 1
    row = store.recent_events()[0]
    assert row["event_type"] == "product_removed"


def test_drill_delivery_failure_then_retry(store):
    source_id = _source(store)
    sender = FakeSender(ok=False)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=sender)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY)],
                [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id,
                [make_discovery("phone-a", source_key=SOURCE_KEY),
                 make_discovery("phone-b", source_key=SOURCE_KEY)],
                [], is_baseline=False, notify=notifier.enqueue)

    result = notifier.drain()
    assert result["sent"] == 0
    assert store.notification_counts("discord") == {"pending": 1}  # remains pending/retryable

    sender.ok = True
    retry_result = notifier.drain()
    assert retry_result["sent"] == 1
    assert store.notification_counts("discord") == {"sent": 1}  # exactly one eventual delivery


def test_drill_duplicate_protection_on_rerun(store):
    source_id = _source(store)
    notifier = DiscordNotifier(store, "https://example.test/webhook", sender=FakeSender(ok=True))
    d = make_discovery("phone-a", source_key=SOURCE_KEY)
    process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=True, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=False, notify=notifier.enqueue)
    process_run(store, SOURCE_KEY, source_id, [d], [], is_baseline=False, notify=notifier.enqueue)  # rerun, unchanged
    assert store.notification_counts("discord") == {}  # no event, no notification
    assert len(store.recent_events()) == 0
