from __future__ import annotations

from datetime import datetime, timedelta, timezone

from feature_phone_clank.core.runner import run_experimental

from helpers import ScriptedCollector, classification_entry, make_discovery

SOURCE_KWARGS = dict(manufacturer="TestCo", source_type="catalogue", region="en_int",
                     base_url="https://example.test")


def test_soak_report_aggregates_runs_events_and_classification(store):
    d1 = make_discovery("p1")
    collector = ScriptedCollector([
        ([d1], [classification_entry("p2", "ambiguous")]),
        ([d1, make_discovery("p2")], [classification_entry("p2", "feature_phone")]),
    ])
    run_experimental(collector, store, **SOURCE_KWARGS)
    run_experimental(collector, store, **SOURCE_KWARGS)

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    report = store.soak_report("test-scripted", since)

    assert report["runs_attempted"] == 2
    assert report["runs_ok"] == 2
    assert report["runs_failed_or_blocked"] == 0
    assert report["product_count_last"] == 2
    assert report["events_by_type"] == {"new_product": 1}
    assert report["events_total"] == 1
    assert report["classification_counts_current"]["feature_phone"] == 1  # only p2 was logged
    assert report["duplicate_dedup_keys"] == []
    assert report["schema_version"] >= 4


def test_soak_report_window_excludes_old_runs(store):
    collector = ScriptedCollector([([make_discovery("p1")], [])])
    run_experimental(collector, store, **SOURCE_KWARGS)

    future_since = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    report = store.soak_report("test-scripted", future_since)
    assert report["runs_attempted"] == 0
