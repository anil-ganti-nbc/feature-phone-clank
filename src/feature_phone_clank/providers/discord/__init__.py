"""Discord webhook delivery (Stage 4). Outbox semantics: `enqueue()` decides
eligibility (`core/notifications.py`) and persists a fully rendered payload
in the `notifications` table; `drain()` posts whatever is still `pending`
and marks each `sent` or `failed`/left `pending` for the next invocation. A
dead webhook or a killed process loses nothing — the row is already durable
before delivery is attempted (brief section 6: "persistence first").

No production webhook is ever contacted by anything in this module unless a
caller supplies `webhook_url` explicitly (tests never do).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

import requests

from ...core.models import AlertLevel, ChangeType, Event
from ...core.notifications import initial_status

log = logging.getLogger("feature_phone_clank.discord")

# After this many failed attempts a notification stops being retried
# automatically and is marked terminally `failed` (brief section 6: retry
# safety without retrying forever). An operator can still requeue it — see
# SqliteStore.requeue_failed_notifications.
MAX_ATTEMPTS = 5

_COLORS: dict[AlertLevel, int] = {
    AlertLevel.HIGH: 0xE74C3C,
    AlertLevel.MEDIUM: 0xE67E22,
    AlertLevel.LOW: 0x3498DB,
    AlertLevel.NOISE: 0x95A5A6,
}

_TITLES: dict[ChangeType, str] = {
    ChangeType.NEW_PRODUCT: "NEW PRODUCT",
    ChangeType.FIELD_CHANGED: "FIELD CHANGE",
    ChangeType.PRODUCT_REMOVED: "PRODUCT REMOVED",
    ChangeType.SPECS_BECAME_AVAILABLE: "SPECS NOW AVAILABLE",
    ChangeType.SPECS_BECAME_UNAVAILABLE: "SPECS UNAVAILABLE",
    ChangeType.CLASSIFICATION_CHANGED: "CLASSIFICATION CHANGED",
    ChangeType.IDENTITY_ANOMALY: "IDENTITY ANOMALY",
    ChangeType.REGIONAL_VARIANT: "REGIONAL VARIANT",
    ChangeType.AVAILABILITY_CHANGED: "AVAILABILITY CHANGED",
    ChangeType.SOURCE_DEGRADED: "SOURCE DEGRADED",
}


def build_embed(event: Event, event_id: int | None = None) -> dict:
    """Compact, evidence-oriented embed (brief section 8). No JSON blobs, no
    speculative prose — every line is a field already on `event`."""
    title = f"FEATURE-01 — {_TITLES.get(event.event_type, event.event_type.value.upper())}"
    lines = [f"**Product:** {event.manufacturer} {event.model}"]
    if event.region:
        lines.append(f"**Region:** {event.region}")
    lines.append(f"**Severity:** {event.alert_level.value.upper()}")

    if event.changed_fields:
        changed = "\n".join(
            f"• {fc.field}: {fc.old_value if fc.old_value is not None else '—'} "
            f"→ {fc.new_value if fc.new_value is not None else '—'}"
            for fc in event.changed_fields[:20]
        )
        lines.append(f"**Changed:**\n{changed}")

    reason = event.meta.get("reason")
    if reason:
        lines.append(f"**Why:** {reason}")

    footer_parts = ["FEATURE-01"]
    if event_id is not None:
        footer_parts.append(f"Event ID: {event_id}")

    embed = {
        "title": title[:256],
        "description": "\n".join(lines)[:4096],
        "color": _COLORS.get(event.alert_level, 0x95A5A6),
        "url": event.url or None,
        "timestamp": event.detected_at.isoformat(),
        "footer": {"text": " · ".join(footer_parts)[:2048]},
    }
    return {"embeds": [embed]}


def build_test_embed(note: str = "") -> dict:
    """Section 12: an explicit, unmistakable test payload. Never references a
    real product/event — no `events` or `products` row is read to build
    this."""
    embed = {
        "title": "FEATURE-01 TEST",
        "description": (
            "This is a test notification confirming Discord delivery wiring. "
            "It does not represent a real product change." + (f"\n\n{note}" if note else "")
        ),
        "color": 0x95A5A6,
        "footer": {"text": "FEATURE-01 · owner field-test notification"},
    }
    return {"embeds": [embed]}


def _post_webhook(webhook_url: str, payload: dict) -> tuple[bool, str | None]:
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.RequestException as exc:
        return False, repr(exc)


class DiscordNotifier:
    """Thin wrapper around the store's notification outbox + a sender
    function. `sender` is swappable (tests pass a fake; the local-echo-
    server trick documented for sibling Clanks works unchanged here) so no
    test in this repo ever needs a real Discord webhook."""

    def __init__(
        self,
        store,
        webhook_url: str | None,
        sender: Callable[[str, dict], tuple[bool, str | None]] = _post_webhook,
    ) -> None:
        self.store = store
        self.webhook_url = webhook_url
        self.sender = sender

    def enqueue(self, event: Event, event_id: int) -> None:
        """Called once per newly-persisted event (see core/pipeline.py's
        optional `notify` callback, wired in by core/runner.py). Persists a
        notification row unconditionally — even a suppressed event gets a
        row, so an operator can see *why* nothing was sent, not just that
        nothing was."""
        status = initial_status(event)
        payload = build_embed(event, event_id=event_id)
        self.store.notification_put(
            "discord", event.dedup_key(), payload, event_id=event_id, status=status,
        )

    def enqueue_test(self, note: str = "") -> dict:
        """Section 12/13: a marked test notification, delivered immediately
        (not left for the next `deliver` run) so the owner gets synchronous
        pass/fail feedback. Uses a `test:` dedup-key namespace, never an
        `events` row, and is excluded from delivery-health accounting."""
        import uuid

        dedup_key = f"test:{uuid.uuid4().hex}"
        payload = build_test_embed(note)
        self.store.notification_put(
            "discord", dedup_key, payload, event_id=None, status="pending",
        )
        row = self.store.notification_by_dedup_key(dedup_key)
        if not self.webhook_url:
            self.store.mark_notification(row["id"], "failed", "no webhook configured")
            return {"sent": False, "error": "no webhook configured", "dedup_key": dedup_key}
        ok, err = self.sender(self.webhook_url, payload)
        self.store.mark_notification(row["id"], "sent" if ok else "failed", err)
        return {"sent": ok, "error": err, "dedup_key": dedup_key}

    def drain(self) -> dict:
        """Attempt delivery of every `pending` notification. Never raises —
        a Discord outage degrades delivery, not collection (brief section
        4/17); the caller (cli.py) always gets a summary dict back."""
        sent = 0
        failed = 0
        rows = self.store.pending_notifications("discord")
        if not self.webhook_url:
            if rows:
                log.warning(
                    "%d notification(s) pending but no webhook configured "
                    "(set FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL)", len(rows),
                )
            return {"sent": 0, "failed": 0, "remaining": len(rows)}

        for row in rows:
            payload = json.loads(row["payload_json"])
            ok, err = self.sender(self.webhook_url, payload)
            if ok:
                self.store.mark_notification(row["id"], "sent")
                sent += 1
                continue
            attempts_next = row["attempts"] + 1
            if attempts_next >= MAX_ATTEMPTS:
                self.store.mark_notification(row["id"], "failed", err)
                failed += 1
                log.warning("discord delivery permanently failed after %d attempts: %s",
                            attempts_next, err)
            else:
                self.store.mark_notification(row["id"], "pending", err)
                log.warning("discord delivery attempt %d/%d failed, will retry: %s",
                            attempts_next, MAX_ATTEMPTS, err)
        remaining = len(self.store.pending_notifications("discord"))
        return {"sent": sent, "failed": failed, "remaining": remaining}
