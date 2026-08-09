from __future__ import annotations

import pytest

from feature_phone_clank.core.collector_base import BaseCollector
from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.runner import ScopeError, run_production_collector
from feature_phone_clank.core.scope import ScopeConfig, load_scope


class UnapprovedCollector(BaseCollector):
    source_key = "test-unapproved"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        return [Discovery(
            source_key=self.source_key, product_key="p1",
            manufacturer="TestCo", model="Sneaky Phone",
            url="https://example.test/p1",
        )]


def test_empty_scope_excludes_every_collector():
    scope = ScopeConfig(production_collectors=[])
    assert "hmd-nokia" not in scope.production_collectors


def test_scope_yaml_only_lists_deliberately_promoted_collectors():
    """Every entry in config/scope.yaml must correspond to a collector that
    actually completed its promotion review (Stage 2.1: hmd-nokia). This
    intentionally does NOT assert emptiness — the whole point of the scope
    file is that it changes only via a reviewed, documented promotion, not
    that it stays empty forever."""
    scope = load_scope("config/scope.yaml")
    reviewed_and_promoted = {"hmd-nokia"}
    assert set(scope.production_collectors) <= reviewed_and_promoted
    assert set(scope.production_collectors) == reviewed_and_promoted


def test_unapproved_collector_refused_by_production_path(store):
    scope = ScopeConfig(production_collectors=[])  # explicitly empty
    with pytest.raises(ScopeError):
        run_production_collector(
            UnapprovedCollector(), store, scope,
            manufacturer="TestCo", source_type="catalogue", region=None,
            base_url="https://example.test",
        )
    # Nothing should have been written to the production store.
    assert store.db.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0


def test_approved_collector_runs_through_production_path(store):
    scope = ScopeConfig(production_collectors=["test-unapproved"])
    result, stats = run_production_collector(
        UnapprovedCollector(), store, scope,
        manufacturer="TestCo", source_type="catalogue", region=None,
        base_url="https://example.test",
    )
    assert result.status == "ok"
    assert store.db.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 1
