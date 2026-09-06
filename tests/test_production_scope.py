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
    actually completed its promotion review (Stage 2.1: hmd-nokia; 2026-08-30
    natural-soak promotion: punkt-ch, doro-gb, mudita-com, sunbeam-f1-us,
    tcl-alcatel-global; 2026-09-05 explicit operator maturity promotion:
    lava-india — see the scope.yaml promotion notes). This intentionally
    does NOT assert emptiness — the whole point of the scope file is that it
    changes only via a reviewed, documented promotion, not that it stays
    empty forever."""
    scope = load_scope("config/scope.yaml")
    reviewed_and_promoted = {
        "hmd-nokia",
        "punkt-ch",
        "doro-gb",
        "mudita-com",
        "sunbeam-f1-us",
        "tcl-alcatel-global",
        # Maturity promoted 2026-09-05 by explicit operator decision. Its
        # health (3 whole-run ReadTimeouts in 50 cycles, unrepaired fetcher)
        # is unchanged and still honest — see scope.yaml.
        "lava-india",
    }
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


# ------------------------------------------------------------ promotion guard


def test_only_deliberately_retired_collectors_remain_outside_production():
    """Fleet guard (operator decision 2026-09-05): every registered collector
    is production-scoped EXCEPT ones retired for a reason that is not
    maturity. itel-india is MOTHBALLED for an infrastructure-cost reason
    (~1.88 GB image for ~6 products, docs/ticket-itel-playwright-
    architecture-decision.md) — promoting maturity must not revive it."""
    import feature_phone_clank.collectors as _  # noqa: F401 - registration side effect
    from feature_phone_clank.core.registry import collectors as reg

    scope = load_scope("config/scope.yaml")
    outside = set(reg.names()) - set(scope.production_collectors)
    assert outside == {"itel-india"}, f"unexpected non-production collectors: {outside}"


def test_promotion_did_not_falsify_lava_health():
    """lava-india was promoted on maturity only. Its known transport problem
    must still be documented rather than papered over — if someone 'fixes'
    this by deleting the health note, that is exactly the failure this
    guards."""
    from pathlib import Path

    scope_text = Path("config/scope.yaml").read_text(encoding="utf-8")
    assert "lava-india" in scope_text
    assert "ReadTimeouts" in scope_text
    assert "915f908" in scope_text  # the unrepaired-fetcher provenance


# ------------------------------------ retirement is a reason, not a permission


def test_mothballed_is_a_reason_and_never_a_permission():
    """A retired collector is outside production for a *different* reason
    than an unqualified one -- but it is outside just the same.

    `is_production` deliberately does not consult the mothballed list: if a
    key ever appeared in both, the allowlist alone would still decide, so
    the new axis can never revive a source or suppress one.
    """
    from feature_phone_clank.core.scope import is_mothballed, is_production

    scope = ScopeConfig(production_collectors=["hmd-nokia"],
                        mothballed_collectors=["itel-india"])
    assert is_production("hmd-nokia", scope) is True
    assert is_production("itel-india", scope) is False
    assert is_mothballed("itel-india", scope) is True
    assert is_mothballed("hmd-nokia", scope) is False

    # Contradictory config: permission still comes from the allowlist alone.
    contradictory = ScopeConfig(production_collectors=["hmd-nokia"],
                                mothballed_collectors=["hmd-nokia"])
    assert is_production("hmd-nokia", contradictory) is True


def test_shipped_scope_retires_itel_india_without_promoting_it():
    """The shipped config records *why* itel-india is not production.

    Before this axis existed the dashboard bucketed it as "Experimental /
    Soak", which told the operator it was still soaking toward promotion.
    It is not: it was withdrawn 2026-08-31 over browser-image cost, and
    resurrection needs an explicit operator review.
    """
    from feature_phone_clank.paths import resolve_config_path

    scope = load_scope(resolve_config_path("scope.yaml"))
    assert "itel-india" in scope.mothballed_collectors
    assert "itel-india" not in scope.production_collectors
    # Promotion of the seven active sources is unaffected by the new list.
    assert "lava-india" in scope.production_collectors
    assert not set(scope.mothballed_collectors) & set(scope.production_collectors)


def test_dashboard_splits_retired_out_of_the_experimental_bucket():
    from feature_phone_clank.dashboard import _registered_collectors

    production, experimental, mothballed = _registered_collectors()
    assert "itel-india" in mothballed
    assert "itel-india" not in experimental
    assert "itel-india" not in production
    # The operator's target: no ACTIVE collector is left at experimental
    # maturity. Retired ones are counted separately, not hidden.
    assert experimental == [], f"unexpected experimental collectors: {experimental}"
    assert "lava-india" in production
