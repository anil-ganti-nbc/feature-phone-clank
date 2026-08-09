"""Shared types for the collect -> normalize -> diff -> event pipeline.

Collectors only ever produce `Discovery` objects (dumb, source-shaped).
Everything downstream — identity resolution, diffing, classification,
persistence — speaks these types. A collector must never invent a value:
unknown stays `None`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    CATALOGUE = "catalogue"
    SUPPORT = "support"
    NEWSROOM = "newsroom"
    CERTIFICATION = "certification"


class ChangeType(StrEnum):
    NEW_PRODUCT = "new_product"
    FIELD_CHANGED = "field_changed"
    REGIONAL_VARIANT = "regional_variant"
    AVAILABILITY_CHANGED = "availability_changed"
    PRODUCT_REMOVED = "product_removed"
    SOURCE_DEGRADED = "source_degraded"
    # Stage 3 additions — each earned by a concrete need in the diff
    # pipeline (core/pipeline.py), not spun up speculatively.
    SPECS_BECAME_AVAILABLE = "specs_became_available"
    SPECS_BECAME_UNAVAILABLE = "specs_became_unavailable"
    CLASSIFICATION_CHANGED = "classification_changed"
    IDENTITY_ANOMALY = "identity_anomaly"  # existing URL now reports a different SKU/model number


class AlertLevel(StrEnum):
    """Editorial-review classification (brief section 11). Distinct from
    `Confidence`, which describes evidence quality, not editorial interest."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOISE = "noise"


class Confidence(StrEnum):
    """Confidence in the detected event itself (brief section 13) — never a
    proxy for how good a story it would make."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewOutcome(StrEnum):
    """Reserved for the future editorial feedback loop (brief section 18).
    Not written or read by anything in Stage 1 — kept here only so the event
    model's shape doesn't need to change when that loop is built."""

    HIT = "HIT"
    INTERESTING = "INTERESTING"
    NOISE = "NOISE"
    BUG = "BUG"


class Discovery(BaseModel):
    """One product observation as a collector saw it. Source-shaped, not yet
    identity-resolved or diffed."""

    source_key: str
    product_key: str  # stable identity within the source: manufacturer + model/SKU/URL
    manufacturer: str
    model: str
    model_number: str | None = None
    region: str | None = None
    url: str
    price: float | None = None
    currency: str | None = None
    availability: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)  # normalized comparable specs
    spec_completeness: str = "complete"  # "complete" | "incomplete" — see Discovery docstring
    raw: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def content_hash(self) -> str:
        """Identity-independent fingerprint of everything that should trigger
        a change event. Observation-only fields (`observed_at`, `raw`) are
        excluded so a re-fetch of unchanged content never looks like a
        change. `spec_completeness` IS included: a product going from
        incomplete to complete specs (HMD finally publishing them) is itself
        a meaningful, alertable change, not something to hide inside an
        unchanged observation."""
        basis = {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "model_number": self.model_number,
            "region": self.region,
            "price": self.price,
            "currency": self.currency,
            "availability": self.availability,
            "fields": self.fields,
            "spec_completeness": self.spec_completeness,
        }
        payload = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FieldChange(BaseModel):
    """One changed field within an event. `old_value`/`new_value` are the
    RAW observed values (traceable to the official source), never
    normalized — normalization (core/diff.py) decides *whether* something
    counts as a change, it never rewrites what gets shown as evidence."""

    field: str
    old_value: Any = None
    new_value: Any = None


class Event(BaseModel):
    """A deterministic, persisted change event (Stage 3). Structured data is
    authoritative — a future formatter/LLM may describe an event in prose,
    but the event itself must be fully reconstructable from these fields
    alone, per brief section 12.

    Persisted independently of notification delivery (Stage 4): this model
    has no delivery/channel concept at all.
    """

    source_key: str
    product_key: str
    manufacturer: str
    model: str
    model_number: str | None = None
    region: str | None = None
    url: str

    event_type: ChangeType
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    previous_observation_id: int | None = None
    current_observation_id: int | None = None
    changed_fields: list[FieldChange] = Field(default_factory=list)

    alert_level: AlertLevel
    confidence: Confidence
    meta: dict[str, Any] = Field(default_factory=dict)

    def dedup_key(self) -> str:
        """Deterministic identity for this exact event: which product,
        which kind of change, between which two observations. Re-deriving
        an event from the same observation transition always produces the
        same key — the DB's UNIQUE constraint on this is the actual
        duplicate-protection mechanism (brief section 13), not a timestamp
        check."""
        basis = (
            f"{self.product_key}|{self.event_type.value}|"
            f"{self.previous_observation_id or 0}|{self.current_observation_id or 0}"
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class CollectorRunResult(BaseModel):
    """What `BaseCollector.run()` always returns, success or failure. Never
    raises — a collector exception is captured here, not propagated."""

    source_key: str
    status: str  # "ok" | "failed"
    discovered: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
