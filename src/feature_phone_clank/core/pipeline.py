"""Stage 3: collector -> Discovery -> identity resolution -> observation
persistence -> deterministic diff -> event classification -> persisted
event. This module owns the *decisions*; `providers/sqlite` owns the SQL.

Nothing here talks to Discord or any other delivery channel — an event
exists the moment this module decides it happened, independent of whether
anything ever notifies about it (brief section 1).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from .diff import MEANINGFUL_FIELDS, diff_meaningful_fields, severity_for_changes
from .models import AlertLevel, ChangeType, Confidence, Discovery, Event, FieldChange

# Optional extension point (Stage 4): called with (event, event_id) right
# after an event is newly persisted. Event detection itself never depends
# on this running, succeeding, or being provided at all — `process_run`
# still returns identical `stats` with or without one (brief section 4:
# "Event detection must remain independent from Discord"). Whatever this
# callback does (e.g. DiscordNotifier.enqueue) is wired in by core/runner.py,
# never imported here.
NotifyFn = Callable[[Event, int], None]

log = logging.getLogger("feature_phone_clank.pipeline")

# Healthy runs in a row a previously-active product must be absent for
# before we conclude it was actually removed (brief section 9: "sites
# glitch"). Only 'ok' runs advance this counter — see runner.py, which
# never calls this pipeline on a blocked/failed run in the first place.
REMOVAL_CONFIRMATION_THRESHOLD = 3


class ClassificationTransition:
    __slots__ = ("slug", "prior", "new")

    def __init__(self, slug: str, prior: str | None, new: str) -> None:
        self.slug = slug
        self.prior = prior
        self.new = new


def compute_classification_transitions(store, source_key: str, collector) -> list[ClassificationTransition]:
    """Must be called BEFORE the classification_log upsert (runner.py does
    this) — reads the prior classification for every candidate the
    collector examined this run, so a transition can be detected instead of
    the prior value being silently overwritten first."""
    transitions = []
    for entry in getattr(collector, "classification_log", []):
        prior_row = store.get_classification(source_key, entry["slug"])
        prior = prior_row["classification"] if prior_row else None
        new = entry["classification"]
        if prior != new:
            transitions.append(ClassificationTransition(entry["slug"], prior, new))
    return transitions


def _build_event(
    *, d: Discovery | None, product_row, event_type: ChangeType,
    previous_observation_id: int | None, current_observation_id: int | None,
    changed_fields: list[FieldChange], alert_level: AlertLevel, confidence: Confidence,
    meta: dict | None = None,
) -> Event:
    """`d` is the fresh Discovery when available (new/changed product);
    `product_row` is the DB row to fall back to for identity fields when
    there's no fresh Discovery (e.g. PRODUCT_REMOVED, where nothing was
    observed this run)."""
    if d is not None:
        source_key, product_key = d.source_key, d.product_key
        manufacturer, model, model_number = d.manufacturer, d.model, d.model_number
        region, url = d.region, d.url
    else:
        product_key = product_row["product_key"]
        source_key = product_key.split(":", 1)[0]
        manufacturer, model, model_number = product_row["manufacturer"], product_row["model"], product_row["model_number"]
        region, url = product_row["region"], product_row["url"]
    return Event(
        source_key=source_key, product_key=product_key, manufacturer=manufacturer,
        model=model, model_number=model_number, region=region, url=url,
        event_type=event_type, previous_observation_id=previous_observation_id,
        current_observation_id=current_observation_id, changed_fields=changed_fields,
        alert_level=alert_level, confidence=confidence, meta=meta or {},
    )


def _record_and_notify(store, event: Event, notify: NotifyFn | None, stats: dict) -> None:
    """Persist `event` (idempotent by dedup_key) and, only for a genuinely
    NEW row, invoke `notify`. A re-derived duplicate of an already-persisted
    event must never re-trigger notification — `store.record_event`
    returning None (dedup hit) is exactly how that's already guaranteed."""
    event_id = store.record_event(event)
    if event_id is None:
        return
    stats["events_created"] += 1
    if notify is not None:
        notify(event, event_id)


def process_run(
    store, source_key: str, source_id: int, discoveries: list[Discovery],
    classification_transitions: list[ClassificationTransition], is_baseline: bool,
    notify: NotifyFn | None = None,
) -> dict:
    """The Stage 3 replacement for the old `store.ingest()` call in
    runner.py. Only ever invoked for a run whose overall status is 'ok'
    (never for blocked_zero_result/failed) — that gating happens in
    runner.py, one level up, exactly where the catastrophic-zero guard
    already lived.
    """
    stats = {
        "discovered": len(discoveries), "new_products": 0, "updated_products": 0,
        "unchanged_observations": 0, "events_created": 0, "identity_anomalies": 0,
        "removed_products": 0,
    }
    seen_product_ids: set[int] = set()
    seen_keys: set[str] = set()

    promoted_slugs = {
        t.slug: t.prior for t in classification_transitions
        if t.new == "feature_phone" and t.prior not in (None, "feature_phone")
    }

    for d in discoveries:
        if d.product_key in seen_keys:
            continue  # a collector must not emit the same identity twice per run
        seen_keys.add(d.product_key)
        # product_key is conventionally "<source_key>:<slug>" (HMD's
        # collector always does this), but Discovery itself doesn't enforce
        # a colon — fall back to the whole key so a collector that doesn't
        # follow the convention still works, just without classification-
        # promotion metadata attached to its NEW_PRODUCT events.
        key_parts = d.product_key.split(":", 1)
        slug = key_parts[1] if len(key_parts) > 1 else key_parts[0]

        existing = store.get_product(d.product_key)

        if existing is None:
            product_id = store.create_product(source_id, d)
            obs_id, _ = store.record_observation_get_id(product_id, d)
            stats["new_products"] += 1
            seen_product_ids.add(product_id)
            if not is_baseline:
                meta = {}
                if slug in promoted_slugs:
                    meta["promoted_from_classification"] = promoted_slugs[slug]
                event = _build_event(
                    d=d, product_row=None, event_type=ChangeType.NEW_PRODUCT,
                    previous_observation_id=None, current_observation_id=obs_id,
                    changed_fields=[], alert_level=AlertLevel.HIGH, confidence=Confidence.HIGH,
                    meta=meta,
                )
                _record_and_notify(store, event, notify, stats)
            continue

        product_id = existing["id"]
        seen_product_ids.add(product_id)

        # Identity anomaly: same canonical URL, different non-empty SKU.
        # Never silently overwritten — products.model_number simply isn't
        # touched again after creation (see create_product); this only
        # raises the anomaly for review. `existing["model_number"]` is
        # always the ORIGINAL creation-time value, so a product whose SKU
        # changed once and then stayed at the new value would otherwise
        # keep comparing unequal on every subsequent run. Gating event
        # creation on `is_new_obs` (the same guard every other branch in
        # this function already uses) is what actually makes this a
        # one-time signal: a rerun that observes the identical
        # already-recorded state is not new information, so it must not
        # mint a second event/notification for the same real-world change
        # (notification-eligibility review, Stage 4).
        if existing["model_number"] and d.model_number and existing["model_number"] != d.model_number:
            prior_latest = store.latest_observation(product_id)
            store.touch_product(product_id, d.url)
            obs_id, is_new_obs = store.record_observation_get_id(product_id, d)
            stats["updated_products"] += 1
            stats["identity_anomalies"] += 1
            if not is_baseline and is_new_obs:
                event = _build_event(
                    d=d, product_row=None, event_type=ChangeType.IDENTITY_ANOMALY,
                    previous_observation_id=prior_latest["id"] if prior_latest else None,
                    current_observation_id=obs_id,
                    changed_fields=[FieldChange(
                        field="model_number", old_value=existing["model_number"],
                        new_value=d.model_number,
                    )],
                    alert_level=AlertLevel.HIGH, confidence=Confidence.HIGH,
                    meta={"reason": "canonical URL now reports a different SKU/model number"},
                )
                _record_and_notify(store, event, notify, stats)
            continue

        prev_obs = store.latest_observation(product_id)
        store.touch_product(product_id, d.url)
        stats["updated_products"] += 1

        obs_id, is_new_obs = store.record_observation_get_id(product_id, d)
        if not is_new_obs or prev_obs is None:
            stats["unchanged_observations"] += 1
            continue

        prev_fields = json.loads(prev_obs["fields_json"])
        prev_completeness = prev_obs["spec_completeness"]
        new_completeness = d.spec_completeness
        event = None

        if prev_completeness == "complete" and new_completeness == "incomplete":
            event = _build_event(
                d=d, product_row=None, event_type=ChangeType.SPECS_BECAME_UNAVAILABLE,
                previous_observation_id=prev_obs["id"], current_observation_id=obs_id,
                changed_fields=[], alert_level=AlertLevel.LOW, confidence=Confidence.MEDIUM,
                meta={"reason": "spec_completeness complete -> incomplete"},
            )
        elif prev_completeness == "incomplete" and new_completeness == "complete":
            newly_populated = [
                FieldChange(field=k, old_value=None, new_value=v)
                for k, v in sorted(d.fields.items()) if k in MEANINGFUL_FIELDS
            ]
            event = _build_event(
                d=d, product_row=None, event_type=ChangeType.SPECS_BECAME_AVAILABLE,
                previous_observation_id=prev_obs["id"], current_observation_id=obs_id,
                changed_fields=newly_populated, alert_level=AlertLevel.MEDIUM,
                confidence=Confidence.HIGH,
            )
        elif prev_completeness == "complete" and new_completeness == "complete":
            changes = diff_meaningful_fields(prev_fields, d.fields)
            if changes:
                event = _build_event(
                    d=d, product_row=None, event_type=ChangeType.FIELD_CHANGED,
                    previous_observation_id=prev_obs["id"], current_observation_id=obs_id,
                    changed_fields=changes, alert_level=severity_for_changes(changes),
                    confidence=Confidence.HIGH,
                )
        # both incomplete: nothing usable to diff, no event.

        if event is not None and not is_baseline:
            _record_and_notify(store, event, notify, stats)

    # Classification demotions on EXISTING products (brief section 11):
    # feature_phone -> anything else, for a product that's already in the
    # catalogue. Never removes the product; a soft, low-severity signal.
    if not is_baseline:
        for t in classification_transitions:
            if t.new == "feature_phone" or t.prior != "feature_phone":
                continue
            product_key = f"{source_key}:{t.slug}"
            product_row = store.get_product(product_key)
            if product_row is None:
                continue
            event = _build_event(
                d=None, product_row=product_row, event_type=ChangeType.CLASSIFICATION_CHANGED,
                previous_observation_id=None, current_observation_id=None,
                changed_fields=[FieldChange(field="classification", old_value=t.prior, new_value=t.new)],
                alert_level=AlertLevel.LOW, confidence=Confidence.MEDIUM,
                meta={"reason": "re-classification evidence changed for an already-catalogued product"},
            )
            _record_and_notify(store, event, notify, stats)

    # Removal confirmation (brief section 9). Only advances on a healthy
    # ('ok') run, and only after a source baseline already exists — a
    # baseline run has no prior expectations to violate.
    if not is_baseline:
        for row in store.active_products_for_source(source_id):
            if row["id"] in seen_product_ids:
                continue
            absences = row["consecutive_absences"] + 1
            store.set_absence_count(row["id"], absences)
            if absences >= REMOVAL_CONFIRMATION_THRESHOLD:
                store.mark_removed(row["id"])
                stats["removed_products"] += 1
                event = _build_event(
                    d=None, product_row=row, event_type=ChangeType.PRODUCT_REMOVED,
                    previous_observation_id=(store.latest_observation(row["id"]) or {"id": None})["id"],
                    current_observation_id=None, changed_fields=[],
                    alert_level=AlertLevel.MEDIUM, confidence=Confidence.MEDIUM,
                    meta={"consecutive_absences": absences,
                          "threshold": REMOVAL_CONFIRMATION_THRESHOLD},
                )
                _record_and_notify(store, event, notify, stats)

    return stats
