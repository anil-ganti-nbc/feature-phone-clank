"""Stage 4: deterministic event -> notification eligibility policy.

Owns exactly one decision: given a persisted `Event`, should a notification
be queued for delivery, or queued-but-suppressed (kept for history/audit,
never sent)? Nothing here talks to Discord or any other provider, and
nothing here decides *whether an event happened* (that's `core/pipeline.py`)
— this module only runs after an event already exists.

No scoring, no weights: one dict lookup, keyed on `ChangeType`. If a new
`ChangeType` is ever added and forgotten here, `decide()` defaults it to
suppressed rather than silently notifying on nothing more than "unhandled ->
must be interesting".
"""

from __future__ import annotations

from .models import ChangeType, Event

# Section 9 policy. HIGH/notify-worthy: the operator wants to know the
# moment these happen. Suppressed-by-default: real, retained, audit-visible
# events (never dropped from `events`/`recent_events`) that are usually
# noise for a push notification — an operator can still find them via
# `feature-phone-clank events`.
NOTIFY_ON_DEFAULT: frozenset[ChangeType] = frozenset({
    ChangeType.NEW_PRODUCT,
    ChangeType.FIELD_CHANGED,          # pipeline.py never creates this event
    ChangeType.PRODUCT_REMOVED,        # type without at least one real changed field
    ChangeType.SPECS_BECAME_AVAILABLE,
    ChangeType.IDENTITY_ANOMALY,
})

SUPPRESS_BY_DEFAULT: frozenset[ChangeType] = frozenset({
    ChangeType.SPECS_BECAME_UNAVAILABLE,   # low severity by design (diff.py) — retained, not pushed
    ChangeType.CLASSIFICATION_CHANGED,     # cosmetic/internal demotion signal
    ChangeType.REGIONAL_VARIANT,           # not produced by the current pipeline; suppressed if it ever is
    ChangeType.AVAILABILITY_CHANGED,       # not produced by the current pipeline; suppressed if it ever is
    ChangeType.SOURCE_DEGRADED,            # collector/source health, not a product event
})


def should_notify(event: Event) -> bool:
    """True only for event types explicitly allow-listed above. Anything
    unrecognized (a future ChangeType nobody updated this policy for) is
    suppressed, not notified — the safe failure direction for a push
    channel."""
    return event.event_type in NOTIFY_ON_DEFAULT


def initial_status(event: Event) -> str:
    """'pending' (eligible, queued for delivery) or 'suppressed' (retained
    in the outbox for audit/history, delivery never attempted)."""
    return "pending" if should_notify(event) else "suppressed"
