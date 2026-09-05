"""Discord webhook delivery (Stage 4). Outbox semantics: `enqueue()` decides
eligibility (`core/notifications.py`) and persists a fully rendered payload
in the `notifications` table; `drain()` posts whatever is still `pending`
and marks each `sent` or `failed`/left `pending` for the next invocation. A
dead webhook or a killed process loses nothing — the row is already durable
before delivery is attempted (brief section 6: "persistence first").

No production webhook is ever contacted by anything in this module unless a
caller supplies `webhook_url` explicitly (tests never do).

Three delivery-safety properties are enforced here rather than assumed:

1. Serialization. Every path that drains a database takes one grant-backed
   cross-process lock derived from that database's resolved path, held
   across select-send-record. Two overlapping senders cannot both read the
   same pending row and both post it.
2. Redaction. The webhook URL is a secret. No error returned, persisted,
   logged or printed by this module may contain it, so transport failures
   are reduced to bounded categories and HTTP status codes.
3. Activation policy. What may be sent is decided by
   `core/delivery_policy.py`, so switching delivery on for the first time
   cannot flush a historical backlog at a live channel.

What is deliberately *not* claimed: exactly-once delivery. An HTTP success
followed by a crash before the row is marked `sent` leaves an ambiguous
window in which the next drain re-posts it. Serialization removes concurrent
duplication; it does not remove that window, and no outbox that records
after sending can.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from ...core.delivery_policy import (
    ACTIVATION_CUTOFF_KEY,
    HELD_REASONS,
    SEND,
    decide,
    load_policy,
    parse_timestamp,
)
from ...core.models import AlertLevel, ChangeType, Event
from ...core.notifications import initial_status
from ...core.run_lock import LockError, RunLock

log = logging.getLogger("feature_phone_clank.discord")

# After this many failed attempts a notification stops being retried
# automatically and is marked terminally `failed` (brief section 6: retry
# safety without retrying forever). An operator can still requeue it — see
# SqliteStore.requeue_failed_notifications.
MAX_ATTEMPTS = 5

# Ceiling on a server-supplied Retry-After. A hostile or broken header must
# not be able to park the queue indefinitely.
MAX_RETRY_AFTER_SECONDS = 15 * 60
# Used when a 429 arrives with no usable Retry-After header.
DEFAULT_RETRY_AFTER_SECONDS = 60

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


# ---------------------------------------------------------------- redaction


def _error_category(exc: Exception) -> str:
    """Map a transport exception to a bounded, secret-free category.

    `repr(exc)` is never used: a `requests` exception's repr embeds the
    request URL, which for a Discord webhook *is* the credential. That
    string used to be returned to callers, written to `notifications.
    last_error` and written to the log, so a single connection failure
    persisted the token in at least three places.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema)):
        return "invalid_webhook_url"
    if isinstance(exc, requests.exceptions.InvalidURL):
        return "invalid_webhook_url"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "too_many_redirects"
    return "transport_error"


def _status_category(status_code: int) -> str:
    """Bounded category for a non-2xx response. The response *body* is never
    retained: Discord error payloads can echo request context, and an
    arbitrary remote string does not belong in local durable state."""
    if status_code == 429:
        return "http_429_rate_limited"
    if status_code in (401, 403):
        return "http_unauthorized"
    if status_code == 404:
        return "http_404_webhook_not_found"
    if 400 <= status_code < 500:
        return f"http_{status_code}_client_error"
    if 500 <= status_code < 600:
        return f"http_{status_code}_server_error"
    return f"http_{status_code}"


def _parse_retry_after(value: object) -> float | None:
    """Seconds from a Retry-After header, clamped. Only the numeric-seconds
    form is honoured; an HTTP-date form (or junk) falls back to the caller's
    default rather than being mis-parsed into a wrong instant."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _post_webhook(webhook_url: str, payload: dict) -> tuple[bool, str | None, float | None]:
    """Post one payload. Returns (ok, sanitized_error, retry_after_seconds).

    Never returns the URL, the token, or the response body.
    """
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as exc:
        return False, _error_category(exc), None
    if 200 <= resp.status_code < 300:
        return True, None, None
    retry_after = None
    if resp.status_code == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        if retry_after is None:
            retry_after = DEFAULT_RETRY_AFTER_SECONDS
    return False, _status_category(resp.status_code), retry_after


def _normalize_outcome(result) -> tuple[bool, str | None, float | None]:
    """Accept both sender shapes.

    The historical contract is `(ok, error)`; the rate-limit work needs a
    third element. Fakes written against the old shape keep working
    unchanged rather than being rewritten to satisfy the new code.
    """
    if isinstance(result, tuple) and len(result) == 3:
        ok, err, retry_after = result
        return bool(ok), err, retry_after
    ok, err = result
    return bool(ok), err, None


def delivery_lock_path(db_path: str | Path) -> Path:
    """Lock identity derives from the resolved database path, because the
    thing being serialized is *that database's outbox* — not a CLI argument.
    `run --lock-path` names the collection lock and is deliberately separate:
    a standalone `deliver` must exclude a concurrent run's drain even though
    it never takes the collection lock."""
    return Path(str(db_path)).resolve().with_suffix(".delivery.lock")


class DeliveryBusy(Exception):
    """Another process holds this database's delivery grant."""


class DiscordNotifier:
    """Thin wrapper around the store's notification outbox + a sender
    function. `sender` is swappable (tests pass a fake; the local-echo-
    server trick documented for sibling Clanks works unchanged here) so no
    test in this repo ever needs a real Discord webhook."""

    def __init__(
        self,
        store,
        webhook_url: str | None,
        sender: Callable[[str, dict], tuple] = _post_webhook,
    ) -> None:
        self.store = store
        self.webhook_url = webhook_url
        self.sender = sender

    # -- locking ---------------------------------------------------------

    def _lock_path(self) -> Path | None:
        db_path = getattr(self.store, "db_path", None)
        if not db_path or db_path == ":memory:":
            # An in-memory database has no cross-process identity and cannot
            # be shared, so there is nothing to serialize against.
            return None
        return delivery_lock_path(db_path)

    def _acquire_delivery_lock(self) -> RunLock | None:
        path = self._lock_path()
        if path is None:
            return None
        return RunLock.acquire(path)

    # -- enqueue ---------------------------------------------------------

    def enqueue(self, event: Event, event_id: int) -> None:
        """Called once per newly-persisted event (see core/pipeline.py's
        optional `notify` callback, wired in by core/runner.py). Persists a
        notification row unconditionally — even a suppressed event gets a
        row, so an operator can see *why* nothing was sent, not just that
        nothing was.

        Runs inside the caller's transaction: this row and the event it
        describes commit together.
        """
        status = initial_status(event)
        payload = build_embed(event, event_id=event_id)
        self.store.notification_put(
            "discord", event.dedup_key(), payload, event_id=event_id, status=status,
        )

    def enqueue_test(self, note: str = "") -> dict:
        """Section 12/13: a marked test notification, delivered immediately
        (not left for the next `deliver` run) so the owner gets synchronous
        pass/fail feedback. Uses a `test:` dedup-key namespace, never an
        `events` row, and is excluded from delivery-health accounting.

        Takes the same delivery grant as `drain()`: a test send during an
        active drain would otherwise add an uncoordinated request against
        the same rate limit.
        """
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
        try:
            lock = self._acquire_delivery_lock()
        except LockError:
            # Leave the row pending and untouched; a later drain sends it.
            return {"sent": False, "error": "delivery_busy", "dedup_key": dedup_key, "status": "delivery_busy"}
        try:
            ok, err, _retry_after = _normalize_outcome(self.sender(self.webhook_url, payload))
        finally:
            if lock is not None:
                lock.release()
        self.store.mark_notification(row["id"], "sent" if ok else "failed", err)
        return {"sent": ok, "error": err, "dedup_key": dedup_key}

    # -- drain -----------------------------------------------------------

    def _eligible_now(self, row) -> bool:
        """False while a durable retry floor (429 Retry-After) is in force."""
        keys = row.keys() if hasattr(row, "keys") else []
        if "not_before" not in keys:
            return True
        floor = parse_timestamp(row["not_before"])
        if floor is None:
            return True
        return datetime.now(timezone.utc) >= floor

    def drain(self, *, include_held: bool = False) -> dict:
        """Attempt delivery of every eligible `pending` notification. Never
        raises — a Discord outage degrades delivery, not collection (brief
        section 4/17); the caller (cli.py) always gets a summary dict back.

        `include_held=True` is the separate, explicit operator action that
        replays history the activation policy is holding. It is never the
        default and nothing sets it automatically.
        """
        rows = self.store.pending_notifications_with_event_time("discord")
        if not self.webhook_url:
            if rows:
                log.warning(
                    "%d notification(s) pending but no webhook configured "
                    "(set FEATURE_PHONE_CLANK_DISCORD_WEBHOOK_URL)", len(rows),
                )
            return {"sent": 0, "failed": 0, "remaining": len(rows), "held": 0, "deferred": 0}

        try:
            lock = self._acquire_delivery_lock()
        except LockError as exc:
            # Explicit, non-destructive refusal: nothing selected for send,
            # no attempt counter touched, no row mutated.
            log.warning("delivery skipped: another process holds the delivery grant")
            return {
                "status": "delivery_busy",
                "sent": 0,
                "failed": 0,
                "held": 0,
                "deferred": 0,
                "remaining": len(rows),
                "detail": str(exc),
            }

        sent = failed = held = deferred = 0
        held_by_reason: dict[str, int] = {}
        rate_limited = False
        try:
            policy = load_policy(self.store.policy_get(ACTIVATION_CUTOFF_KEY))
            # Re-select under the grant: rows may have changed between the
            # unlocked count above and holding exclusivity.
            rows = self.store.pending_notifications_with_event_time("discord")
            for row in rows:
                if not self._eligible_now(row):
                    deferred += 1
                    continue
                verdict = SEND if include_held else decide(row["event_detected_at"], policy)
                if verdict in HELD_REASONS:
                    held += 1
                    held_by_reason[verdict] = held_by_reason.get(verdict, 0) + 1
                    continue

                payload = json.loads(row["payload_json"])
                ok, err, retry_after = _normalize_outcome(self.sender(self.webhook_url, payload))
                if ok:
                    self.store.mark_notification(row["id"], "sent")
                    sent += 1
                    continue

                if err == "http_429_rate_limited":
                    # Do not burn an attempt and do not keep hammering the
                    # rest of the queue into an active rate limit: record a
                    # durable floor for this row and stop this drain.
                    wait = retry_after if retry_after is not None else DEFAULT_RETRY_AFTER_SECONDS
                    until = datetime.now(timezone.utc) + timedelta(seconds=wait)
                    self.store.defer_notification(row["id"], until.isoformat())
                    deferred += 1
                    rate_limited = True
                    log.warning(
                        "discord rate limited; deferring delivery for %.0fs (%d row(s) not attempted)",
                        wait, max(0, len(rows) - (sent + failed + held + deferred)),
                    )
                    break

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
        finally:
            if lock is not None:
                lock.release()

        remaining = len(self.store.pending_notifications("discord"))
        result = {
            "sent": sent,
            "failed": failed,
            "held": held,
            "deferred": deferred,
            "remaining": remaining,
        }
        if held_by_reason:
            result["held_by_reason"] = held_by_reason
        if rate_limited:
            result["status"] = "rate_limited"
        return result
