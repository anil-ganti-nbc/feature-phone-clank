"""Stage 3 pipeline tests: baseline behaviour, event generation, removal
confirmation, classification transitions, idempotency. Fixture/DB-based
only — no live network (helpers.ScriptedCollector drives each crawl)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from feature_phone_clank.core.models import AlertLevel, ChangeType, Confidence, Event
from feature_phone_clank.core.pipeline import process_run
from feature_phone_clank.core.runner import run_experimental

from helpers import ScriptedCollector, classification_entry, make_discovery

SOURCE_KWARGS = dict(manufacturer="TestCo", source_type="catalogue", region="en_int",
                     base_url="https://example.test")


def _run(collector, store):
    return run_experimental(collector, store, **SOURCE_KWARGS)


def _events(store, source_key="test-scripted"):
    return store.recent_events(source_key=source_key, limit=100)


# -- 1/2: baseline + identical re-observation ------------------------------

def test_baseline_observation_generates_no_event(store):
    fields = {"usb-connection": {"values": ["usb-type-c"], "category": "connectivity"}}
    d = make_discovery("p1", fields=fields)
    collector = ScriptedCollector([([d], [])])
    _run(collector, store)
    assert _events(store) == []
    assert store.active_product_count("test-scripted") == 1


def test_identical_second_observation_generates_no_event(store):
    fields = {"usb-connection": {"values": ["usb-type-c"], "category": "connectivity"}}
    d = make_discovery("p1", fields=fields)
    collector = ScriptedCollector([([d], []), ([d], [])])
    _run(collector, store)
    _run(collector, store)
    assert _events(store) == []


# -- 3/4: new product after an established baseline ------------------------

def test_new_product_after_source_baseline_fires_exactly_one_event(store):
    baseline = [make_discovery(f"p{i}") for i in range(44)]
    collector = ScriptedCollector([
        (baseline, []),
        (baseline + [make_discovery("p44", model="The 45th Phone")], []),
    ])
    _run(collector, store)  # baseline: 44 products, 0 events
    assert _events(store) == []

    result, stats = _run(collector, store)  # crawl 2: 45th product appears
    events = _events(store)
    new_product_events = [e for e in events if e["event_type"] == "new_product"]
    assert len(new_product_events) == 1
    assert new_product_events[0]["product_key"] == "test-scripted:p44"
    assert new_product_events[0]["url"] == "https://example.test/p44"
    assert stats["events_created"] == 1
    assert store.active_product_count("test-scripted") == 45


def test_new_product_does_not_require_an_allowlist_entry(store):
    """No config, no override — a Discovery the collector produces is
    sufficient; identity resolution and event generation never consult an
    allowlist."""
    baseline = [make_discovery("p1")]
    collector = ScriptedCollector([(baseline, []), (baseline + [make_discovery("p2")], [])])
    _run(collector, store)
    _run(collector, store)
    assert store.get_product("test-scripted:p2") is not None


def test_repeated_identical_new_product_crawl_creates_no_duplicate_event(store):
    baseline = [make_discovery("p1")]
    with_new = baseline + [make_discovery("p2")]
    collector = ScriptedCollector([(baseline, []), (with_new, []), (with_new, [])])
    _run(collector, store)
    _run(collector, store)  # p2 appears -> 1 NEW_PRODUCT event
    _run(collector, store)  # identical crawl -> must not duplicate
    events = [e for e in _events(store) if e["event_type"] == "new_product"]
    assert len(events) == 1


# -- 5/6: field changes -----------------------------------------------------

def test_single_meaningful_field_change_produces_field_changed_event(store):
    old_fields = {"usb-connection": {"values": ["micro-usb"], "category": "connectivity"}}
    new_fields = {"usb-connection": {"values": ["usb-type-c"], "category": "connectivity"}}
    collector = ScriptedCollector([
        ([make_discovery("p1", fields=old_fields)], []),
        ([make_discovery("p1", fields=new_fields)], []),
    ])
    _run(collector, store)
    _run(collector, store)
    events = _events(store)
    assert len(events) == 1
    assert events[0]["event_type"] == "field_changed"
    changed = json.loads(events[0]["changed_fields_json"])
    assert len(changed) == 1
    assert changed[0]["field"] == "usb-connection"
    assert changed[0]["old_value"] == old_fields["usb-connection"]
    assert changed[0]["new_value"] == new_fields["usb-connection"]


def test_multiple_simultaneous_field_changes_are_one_event(store):
    """User preference: one event per observation transition containing all
    meaningful changes, not one event per field."""
    old_fields = {
        "usb-connection": {"values": ["micro-usb"], "category": "connectivity"},
        "max-network-speed": {"values": ["2g"], "category": "networks"},
    }
    new_fields = {
        "usb-connection": {"values": ["usb-type-c"], "category": "connectivity"},
        "max-network-speed": {"values": ["4g"], "category": "networks"},
    }
    collector = ScriptedCollector([
        ([make_discovery("p1", fields=old_fields)], []),
        ([make_discovery("p1", fields=new_fields)], []),
    ])
    _run(collector, store)
    _run(collector, store)
    events = _events(store)
    assert len(events) == 1
    changed = json.loads(events[0]["changed_fields_json"])
    assert {c["field"] for c in changed} == {"usb-connection", "max-network-speed"}
    assert events[0]["alert_level"] == "high"  # network speed is a HIGH_IMPACT field


# -- 7: cosmetic/provenance-only change -------------------------------------

def test_non_meaningful_field_change_produces_no_editorial_event(store):
    old_fields = {"buttons": {"values": ["physical"], "category": "design"}}
    new_fields = {"buttons": {"values": ["capacitive"], "category": "design"}}
    collector = ScriptedCollector([
        ([make_discovery("p1", fields=old_fields)], []),
        ([make_discovery("p1", fields=new_fields)], []),
    ])
    _run(collector, store)
    _run(collector, store)
    assert _events(store) == []


# -- 8: canonical-name cleanup -----------------------------------------------

def test_canonical_name_cleanup_produces_no_false_event(store):
    fields = {"usb-connection": {"values": ["usb-type-c"], "category": "connectivity"}}
    collector = ScriptedCollector([
        ([make_discovery("p1", model="HMD 130 Music | Your music on your terms", fields=fields)], []),
        ([make_discovery("p1", model="HMD 130 Music", fields=fields)], []),  # cleanup, same fields
    ])
    _run(collector, store)
    _run(collector, store)
    assert _events(store) == []
    product = store.get_product("test-scripted:p1")
    assert product["model"] == "HMD 130 Music | Your music on your terms"  # display name unchanged
    # display name isn't identity: the same product row is reused, not duplicated
    assert store.active_product_count("test-scripted") == 1


# -- 9: SKU mismatch on an existing identity --------------------------------

def test_sku_mismatch_on_existing_url_raises_anomaly_without_overwriting(store):
    collector = ScriptedCollector([
        ([make_discovery("p1", model_number="SKU1")], []),
        ([make_discovery("p1", model_number="SKU2")], []),
    ])
    _run(collector, store)
    _run(collector, store)
    events = _events(store)
    anomalies = [e for e in events if e["event_type"] == "identity_anomaly"]
    assert len(anomalies) == 1
    changed = json.loads(anomalies[0]["changed_fields_json"])
    assert changed[0] == {"field": "model_number", "old_value": "SKU1", "new_value": "SKU2"}
    assert anomalies[0]["alert_level"] == "high"
    # not silently overwritten
    product = store.get_product("test-scripted:p1")
    assert product["model_number"] == "SKU1"


# -- 10/11: spec completeness transitions -----------------------------------

def test_specs_incomplete_to_complete_fires_specs_became_available(store):
    collector = ScriptedCollector([
        ([make_discovery("p1", fields={}, spec_completeness="incomplete", model_number=None)], []),
        ([make_discovery(
            "p1", fields={"os": {"values": ["s30+"], "category": "operating-system"}},
            spec_completeness="complete", model_number="SKU1",
        )], []),
    ])
    _run(collector, store)
    _run(collector, store)
    events = _events(store)
    assert len(events) == 1
    assert events[0]["event_type"] == "specs_became_available"
    changed = json.loads(events[0]["changed_fields_json"])
    assert changed == [{"field": "os", "old_value": None,
                        "new_value": {"values": ["s30+"], "category": "operating-system"}}]


def test_specs_complete_to_incomplete_fires_one_summary_event_not_many(store):
    """brief section 8: don't generate 20 FIELD_CHANGED(value -> None)
    events merely because a page temporarily stopped parsing."""
    full_fields = {
        "os": {"values": ["s30+"], "category": "operating-system"},
        "usb-connection": {"values": ["usb-type-c"], "category": "connectivity"},
        "bluetooth": {"values": ["5-0"], "category": "connectivity"},
    }
    collector = ScriptedCollector([
        ([make_discovery("p1", fields=full_fields, spec_completeness="complete")], []),
        ([make_discovery("p1", fields={}, spec_completeness="incomplete")], []),
    ])
    _run(collector, store)
    _run(collector, store)
    events = _events(store)
    assert len(events) == 1
    assert events[0]["event_type"] == "specs_became_unavailable"
    assert json.loads(events[0]["changed_fields_json"]) == []


# -- 12-15: removal confirmation ---------------------------------------------

def test_one_run_disappearance_does_not_fire_removal(store):
    collector = ScriptedCollector([
        ([make_discovery("p1"), make_discovery("p2")], []),
        ([make_discovery("p1")], []),  # p2 missing once
    ])
    _run(collector, store)
    _run(collector, store)
    assert _events(store) == []
    p2 = store.get_product("test-scripted:p2")
    assert p2["status"] == "active"
    assert p2["consecutive_absences"] == 1


def test_repeated_healthy_disappearance_fires_product_removed(store):
    collector = ScriptedCollector([
        ([make_discovery("p1"), make_discovery("p2")], []),
        ([make_discovery("p1")], []),  # absence 1
        ([make_discovery("p1")], []),  # absence 2
        ([make_discovery("p1")], []),  # absence 3 -> threshold
    ])
    for _ in range(4):
        _run(collector, store)
    events = [e for e in _events(store) if e["event_type"] == "product_removed"]
    assert len(events) == 1
    assert events[0]["product_key"] == "test-scripted:p2"
    p2 = store.get_product("test-scripted:p2")
    assert p2["status"] == "removed"


def test_disappearance_during_catastrophic_run_does_not_advance_counter(store):
    collector = ScriptedCollector([
        ([make_discovery("p1"), make_discovery("p2")], []),
        ([], []),  # catastrophic zero-result: blocked, must not touch counters
    ])
    _run(collector, store)
    result, stats = _run(collector, store)
    assert stats["status"] == "blocked_zero_result"
    p1 = store.get_product("test-scripted:p1")
    p2 = store.get_product("test-scripted:p2")
    assert p1["consecutive_absences"] == 0
    assert p2["consecutive_absences"] == 0
    assert _events(store) == []


def test_reappearance_before_threshold_clears_suspected_removal(store):
    collector = ScriptedCollector([
        ([make_discovery("p1"), make_discovery("p2")], []),
        ([make_discovery("p1")], []),               # p2 absent once
        ([make_discovery("p1"), make_discovery("p2")], []),  # p2 reappears
    ])
    for _ in range(3):
        _run(collector, store)
    p2 = store.get_product("test-scripted:p2")
    assert p2["status"] == "active"
    assert p2["consecutive_absences"] == 0
    assert _events(store) == []


# -- 16/17: classification transitions ---------------------------------------

def test_ambiguous_to_feature_phone_after_baseline_is_a_new_product_with_promotion_evidence(store):
    collector = ScriptedCollector([
        ([make_discovery("p1")], [classification_entry("p2", "ambiguous")]),
        ([make_discovery("p1"), make_discovery("p2")], [classification_entry("p2", "feature_phone")]),
    ])
    _run(collector, store)
    _run(collector, store)
    events = [e for e in _events(store) if e["event_type"] == "new_product"]
    assert len(events) == 1
    assert events[0]["product_key"] == "test-scripted:p2"
    meta = json.loads(events[0]["meta_json"])
    assert meta["promoted_from_classification"] == "ambiguous"


def test_new_ambiguous_candidate_alone_fires_no_new_product_event(store):
    collector = ScriptedCollector([
        ([make_discovery("p1")], []),
        ([make_discovery("p1")], [classification_entry("p2", "ambiguous")]),
    ])
    _run(collector, store)
    _run(collector, store)
    assert _events(store) == []
    assert store.get_product("test-scripted:p2") is None
    log_row = store.get_classification("test-scripted", "p2")
    assert log_row["classification"] == "ambiguous"


def test_existing_product_demoted_from_feature_phone_gets_soft_event_not_removal(store):
    collector = ScriptedCollector([
        ([make_discovery("p1")], [classification_entry("p1", "feature_phone")]),
        ([make_discovery("p1")], [classification_entry("p1", "ambiguous")]),
    ])
    _run(collector, store)
    _run(collector, store)
    events = [e for e in _events(store) if e["event_type"] == "classification_changed"]
    assert len(events) == 1
    assert events[0]["alert_level"] == "low"
    # product must NOT be removed just because classification softened
    p1 = store.get_product("test-scripted:p1")
    assert p1["status"] == "active"


# -- 18: event persistence is idempotent -------------------------------------

def test_record_event_is_idempotent_by_dedup_key(store):
    d = make_discovery("p1")
    collector = ScriptedCollector([([d], [])])
    _run(collector, store)
    product = store.get_product("test-scripted:p1")
    obs = store.latest_observation(product["id"])

    event = Event(
        source_key="test-scripted", product_key="test-scripted:p1",
        manufacturer="TestCo", model="Test Phone", model_number="SKU1",
        region="en_int", url="https://example.test/p1",
        event_type=ChangeType.FIELD_CHANGED, previous_observation_id=obs["id"],
        current_observation_id=obs["id"], changed_fields=[],
        alert_level=AlertLevel.MEDIUM, confidence=Confidence.HIGH,
    )
    first_id = store.record_event(event)
    second_id = store.record_event(event)
    assert first_id is not None
    assert second_id is None  # duplicate, ignored
    rows = store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert rows == 1


# -- 19: real production baseline (copied DB) stays event-free --------------

PRODUCTION_DB = Path(__file__).resolve().parents[1] / "data" / "feature_phone_clank.db"


@pytest.mark.skipif(not PRODUCTION_DB.exists(), reason="no production database present")
def test_production_baseline_copy_stays_event_free_when_reobserved_unchanged(tmp_path):
    """Uses a COPY of the real 44-product HMD production database (never
    the live file — see user constraint 16) and re-feeds it its own
    unchanged stored state. Proves the pipeline is inert on real
    production-shaped data without any live network."""
    from feature_phone_clank.providers.sqlite import SqliteStore

    scratch = tmp_path / "production_copy.db"
    shutil.copy(PRODUCTION_DB, scratch)
    store = SqliteStore(str(scratch))
    try:
        source = store.db.execute(
            "SELECT id FROM sources WHERE source_key='hmd-nokia'"
        ).fetchone()
        assert source is not None
        products = store.db.execute(
            "SELECT * FROM products WHERE source_id=? AND status='active'", (source["id"],)
        ).fetchall()
        assert len(products) == 44

        discoveries = []
        for p in products:
            obs = store.latest_observation(p["id"])
            discoveries.append(make_discovery(
                p["product_key"].split(":", 1)[1], model=p["model"],
                model_number=p["model_number"], region=p["region"],
                fields=json.loads(obs["fields_json"]),
                spec_completeness=obs["spec_completeness"],
                manufacturer=p["manufacturer"], source_key="hmd-nokia",
            ))

        stats = process_run(store, "hmd-nokia", source["id"], discoveries, [], is_baseline=False)
        assert stats["events_created"] == 0
        assert stats["new_products"] == 0
        assert stats["identity_anomalies"] == 0
    finally:
        store.close()
