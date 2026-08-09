"""Deterministic field diffing: what counts as an editorial change, and what
doesn't (brief sections 4/7/8 — meaningful-field filtering, normalization,
missing-field semantics).

Nothing here touches the database or a collector. Pure functions over plain
`fields` dicts (as produced by `Discovery.fields`), so they're testable in
isolation from the store/pipeline.
"""

from __future__ import annotations

from typing import Any

from .models import AlertLevel, FieldChange

# The explicit editorial-field allowlist (brief section 4: "build the list
# explicitly rather than diffing everything except a few ignored fields").
# Keyed on the HMD PimSpec/PimNumericSpec key names actually observed in
# production (collectors/hmd.py). A field NOT in this set can still change
# from crawl to crawl — it's just never treated as editorially meaningful,
# so it produces no event (the new observation row still records it).
#
# Deliberately excluded: "is-present" (in-the-box accessory list — quick
# start guide, safety booklet, etc. — packaging, not specification) and
# purely industrial-design descriptors ("back", "frame", "sound", "voltage",
# "ergonomic-design", "buttons") per the brief's own caution against
# over-collecting presentation-dependent fields; the same caution applies to
# what's worth alerting on.
MEANINGFUL_FIELDS: frozenset[str] = frozenset({
    # connectivity / platform
    "usb-connection", "max-network-speed", "networks",
    "network-band-gsm", "network-band-lte", "network-band-wcdma",
    "network-bands-2g", "network-bands-4g",
    "os", "bluetooth", "bluetooth-version", "wifi", "nfc", "positioning-systems",
    "sim-size", "sim-type", "sim-1-card-type", "sim-2-card-type",
    # power
    "battery-capacity", "battery-type", "battery-life",
    "charging", "charging-v", "charging-a",
    # memory / storage
    "ram", "internal-storage", "external-storage", "google-drive-storage",
    # display
    "resolution", "display-type", "aspect-ratio", "secondary-display", "cover-glass",
    # camera
    "rear-camera-1-spec", "rear-camera-2-spec", "rear-camera-3-spec", "rear-camera-4-spec",
    "rear-camera-1-sensor", "rear-camera-2-sensor", "rear-camera-3-sensor", "rear-camera-4-sensor",
    "rear-camera-1-spec-text", "rear-camera-2-spec-text",
    "front-camera-sensor", "front-camera-spec-text",
    "number-of-rear-cameras", "rear-flash-text", "fingerprint-sensor",
    # ruggedness / audio / misc capability
    "water-resistant-ipx-grading", "cpu", "fm-radio-receiver", "headphone-type",
    "headphone-jack",
    # commerce (reserved — HMD collector doesn't populate this yet)
    "price",
})

# Fields whose change alone justifies HIGH severity (brief section 14:
# "significant connectivity/platform changes"). Everything else meaningful
# is MEDIUM. This is the entire severity model — no scoring, no weights.
HIGH_IMPACT_FIELDS: frozenset[str] = frozenset({
    "max-network-speed", "networks", "network-band-gsm", "network-band-lte",
    "network-band-wcdma", "network-bands-2g", "network-bands-4g",
    "os", "usb-connection", "wifi", "nfc", "positioning-systems",
})


def normalize_field_value(raw: Any) -> Any:
    """Comparison-only projection of a field value — used to decide whether
    two observations differ, never persisted in place of the raw value.

    Handles the two shapes `Discovery.fields` values actually take
    (`core/collectors/hmd.py::_extract_spec_fields`):
    - PimSpec:        {"values": [...], "category": ...}     (list of strings)
    - PimNumericSpec: {"value": <num>, "unit": <str>}

    Conservative on purpose (brief section 7): only whitespace, case, and
    value-list ordering are normalized away. No token rewriting (`"USB C"`
    vs `"USB-C"` are NOT merged) — HMD's own `normalisedValue` field is
    already a canonical slug in practice, so guessing at looser equivalence
    would risk erasing a real spec change for no observed benefit.
    """
    if isinstance(raw, dict) and "values" in raw:
        values = sorted(str(v).strip().lower() for v in raw.get("values", []))
        return ("values", tuple(values))
    if isinstance(raw, dict) and "value" in raw:
        unit = str(raw.get("unit") or "").strip().lower()
        return ("numeric", raw.get("value"), unit)
    if isinstance(raw, str):
        return raw.strip().lower()
    return raw


def diff_meaningful_fields(
    old_fields: dict[str, Any], new_fields: dict[str, Any],
) -> list[FieldChange]:
    """Field-level diff, restricted to `MEANINGFUL_FIELDS`, with the
    missing-field policy from brief section 8:

    - present in both, same after normalization  -> no change
    - present in both, different after normalization -> FIELD_CHANGED entry
    - absent before, present now (a field appearing) -> FIELD_CHANGED entry
      (old_value=None) — a genuine information gain, not suspicious
    - present before, absent now (a field disappearing) -> NOT a change.
      A single field vanishing while the rest of the page still parsed is
      far more likely a parser/page hiccup for that one field than a real
      spec removal, and HMD has already demonstrated pages that render
      inconsistently (Stage 2.1 findings). Whole-page unavailability is a
      distinct, explicit signal (`spec_completeness`, handled one level up
      in core/pipeline.py as SPECS_BECAME_UNAVAILABLE) — that is where
      "the specification became unavailable" gets to be an event; a lone
      missing key here does not.

    Only call this when both observations have `spec_completeness ==
    'complete'` — the pipeline never diffs fields when either side is
    incomplete (see brief section 8 / pipeline.py).
    """
    changes: list[FieldChange] = []
    keys = (old_fields.keys() | new_fields.keys()) & MEANINGFUL_FIELDS
    for key in sorted(keys):
        has_old = key in old_fields
        has_new = key in new_fields
        if has_old and not has_new:
            continue  # disappearance: not a change (see docstring)
        if not has_old and has_new:
            changes.append(FieldChange(field=key, old_value=None, new_value=new_fields[key]))
            continue
        old_raw, new_raw = old_fields[key], new_fields[key]
        if normalize_field_value(old_raw) != normalize_field_value(new_raw):
            changes.append(FieldChange(field=key, old_value=old_raw, new_value=new_raw))
    return changes


def severity_for_changes(changes: list[FieldChange]) -> AlertLevel:
    """HIGH if any changed field is a connectivity/platform field, else
    MEDIUM. Never called with an empty list (caller only builds an event
    when `changes` is non-empty)."""
    if any(c.field in HIGH_IMPACT_FIELDS for c in changes):
        return AlertLevel.HIGH
    return AlertLevel.MEDIUM
