"""Durable activation policy for notification delivery.

One decision only: given a queued notification and the operator's configured
activation cutoff, may this row be sent now, or must it be *held*?

"Held" is not "failed". A held row keeps its `pending` status, its payload,
its attempt count and its history untouched; it is simply not eligible for
this drain. That is what makes turning delivery on for the first time safe:
a backlog accumulated while the webhook was unconfigured stays preserved and
visible instead of being flushed at a live channel the moment a URL appears.

Fail-closed is the rule in every ambiguous direction:

- A cutoff that is configured but unparseable holds everything. A policy we
  cannot read is never read as "send it all".
- A row whose event timestamp cannot be established (no event row, null or
  unparseable `detected_at`) is held whenever a cutoff is in force. We cannot
  prove it is newer than the cutoff, so we do not send it.

With no cutoff configured the historical behaviour is retained (every
eligible pending row may send). Choosing and installing the live cutoff is a
deliberate operator act, documented in the activation runbook — this module
only enforces whatever has been chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Key under which the cutoff is persisted in the `delivery_policy` table.
ACTIVATION_CUTOFF_KEY = "discord.activation_cutoff"

# Verdicts. Exactly one is returned per row; every non-SEND value means
# "not attempted, nothing incremented, row untouched".
SEND = "send"
HELD_BEFORE_CUTOFF = "held_before_cutoff"
HELD_UNKNOWN_EVENT_TIME = "held_unknown_event_time"
HELD_POLICY_UNREADABLE = "held_policy_unreadable"

HELD_REASONS = frozenset({
    HELD_BEFORE_CUTOFF,
    HELD_UNKNOWN_EVENT_TIME,
    HELD_POLICY_UNREADABLE,
})


def parse_timestamp(raw: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp to an aware UTC datetime.

    Returns None for anything unparseable, including None/empty. Callers must
    treat None as "unknown", never as "now" or "epoch" — the whole point of
    this module is that unknown times fail closed.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Stored naive timestamps in this store are UTC by convention.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ActivationPolicy:
    """The resolved activation policy currently in force."""

    cutoff: datetime | None = None
    raw: str | None = None
    unreadable: bool = False

    @property
    def configured(self) -> bool:
        return self.raw is not None

    def describe(self) -> dict:
        return {
            "configured": self.configured,
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
            "raw": self.raw,
            "unreadable": self.unreadable,
        }


def load_policy(raw: object) -> ActivationPolicy:
    """Resolve the stored cutoff value into a policy.

    A stored-but-unparseable value produces `unreadable=True`, which holds
    every row. It is deliberately not an exception: delivery degrading to
    "hold everything and say so" is safe, while a crash in the drain path is
    not.
    """
    if raw is None:
        return ActivationPolicy()
    text = raw if isinstance(raw, str) else str(raw)
    parsed = parse_timestamp(text)
    if parsed is None:
        return ActivationPolicy(cutoff=None, raw=text, unreadable=True)
    return ActivationPolicy(cutoff=parsed, raw=text)


def decide(event_detected_at: object, policy: ActivationPolicy) -> str:
    """Return SEND or one of the HELD_* reasons for a single queued row."""
    if policy.unreadable:
        return HELD_POLICY_UNREADABLE
    if policy.cutoff is None:
        # No activation policy installed: historical behaviour, unchanged.
        return SEND
    detected = parse_timestamp(event_detected_at)
    if detected is None:
        return HELD_UNKNOWN_EVENT_TIME
    return SEND if detected >= policy.cutoff else HELD_BEFORE_CUTOFF
