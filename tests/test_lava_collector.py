"""Lava collector tests (Stage E2, EXPERIMENTAL). Fixture-based only — no
live network calls (user constraint 7). Covers: a valid feature phone,
smartphone rejection, cross-listing conflict, a `parent_id` mislabel,
incomplete specs, a fetch failure, baseline via run_experimental, and a
repeat unchanged run."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_phone_clank.collectors.lava import (
    BASE, FEATURE_LISTING_URL, LavaCollector, SMARTPHONE_LISTING_URL, FetchResult,
)
from feature_phone_clank.core.runner import run_experimental
from feature_phone_clank.providers.sqlite import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures" / "lava"


class FakeFetcher:
    def __init__(self, routes: dict[str, Path]):
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.requested.append(url)
        path = self.routes.get(url)
        if path is None:
            return FetchResult(url=url, status=404, text="")
        return FetchResult(url=url, status=200, text=path.read_text(encoding="utf-8"))


def _standard_routes() -> dict[str, Path]:
    return {
        FEATURE_LISTING_URL: FIXTURES / "featurephones_listing.html",
        SMARTPHONE_LISTING_URL: FIXTURES / "smartphones_listing.html",
        f"{BASE}/featurephones/hero600-pluse": FIXTURES / "hero600-pluse.html",
        f"{BASE}/featurephones/hero-shakti-2025": FIXTURES / "hero-shakti-2025.html",
        # dual-listed and mislabeled-phone deliberately have NO product-page
        # route mapped: they're quarantined before a detail fetch happens
    }


# -- mode 1: valid feature phone -------------------------------------------

def test_valid_feature_phone_is_discovered_and_parsed():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "lava-india:hero600-pluse")
    assert d.manufacturer == "Lava"
    assert d.model == "Hero 600+"
    assert d.region == "india"
    assert d.url == f"{BASE}/featurephones/hero600-pluse"
    assert d.price == 1599.0
    assert d.currency == "INR"
    assert d.fields["sim"]["values"] == ["Dual SIM, GSM + GSM"]
    assert d.fields["size"]["values"] == ['4.6 cm (1.8")']
    assert d.spec_completeness == "complete"


def test_launch_date_is_retained_as_raw_evidence_only():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "lava-india:hero600-pluse")
    assert d.raw["raw_launch_date"] == "2024-06-30T00:00:00.000Z"
    assert d.raw["new_launches_flag"] == "no"
    # not surfaced as a first-class Discovery field — content_hash() must
    # not change just because Lava rewrites a stale date server-side
    assert "launch_date" not in d.fields


# -- mode 2: smartphone rejection ------------------------------------------

def test_smartphone_only_products_are_not_discovered():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    keys = {d.product_key for d in discoveries}
    assert "lava-india:agni-3" not in keys


def test_smartphone_rejection_is_logged():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    collector.collect()

    agni = next(e for e in collector.classification_log if e["slug"] == "agni-3")
    assert agni["classification"] == "smartphone"


# -- mode 3: ambiguous — cross-listing conflict and parent_id mislabel -----

def test_slug_on_both_listings_is_quarantined():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    keys = {d.product_key for d in discoveries}
    assert "lava-india:dual-listed" not in keys
    entry = next(e for e in collector.classification_log if e["slug"] == "dual-listed")
    assert entry["classification"] == "ambiguous"


def test_parent_id_mismatch_is_quarantined_not_trusted_from_listing_alone():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    # "mislabeled-phone" was served on the /featurephones listing but its
    # OWN parent_id field says "smartphones" — contradictory, must not be
    # silently trusted either way
    keys = {d.product_key for d in discoveries}
    assert "lava-india:mislabeled-phone" not in keys
    entry = next(e for e in collector.classification_log if e["slug"] == "mislabeled-phone")
    assert entry["classification"] == "ambiguous"


# -- mode 4: incomplete specs / fetch failure -------------------------------

def test_null_specs_block_yields_incomplete_not_error():
    collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "lava-india:hero-shakti-2025")
    assert d.fields == {}
    assert d.spec_completeness == "incomplete"
    assert "no view_details_specs" in d.raw["fetch_note"]


def test_product_page_fetch_failure_still_yields_discovery_with_catalogue_name():
    routes = _standard_routes()
    del routes[f"{BASE}/featurephones/hero-shakti-2025"]  # now 404s
    collector = LavaCollector(fetcher=FakeFetcher(routes))
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "lava-india:hero-shakti-2025")
    assert d.model == "Hero Shakti 2025"
    assert d.spec_completeness == "incomplete"
    assert "HTTP 404" in d.raw["fetch_note"]


# -- mode 5: baseline / repeat run via run_experimental (isolation) --------

def test_baseline_run_via_run_experimental_does_not_touch_production_db(tmp_path):
    store = SqliteStore(str(tmp_path / "lava_experimental.db"))
    try:
        collector = LavaCollector(fetcher=FakeFetcher(_standard_routes()))
        result, stats = run_experimental(
            collector, store, manufacturer="Lava", source_type="catalogue",
            region="india", base_url=BASE,
        )
        assert result.status == "ok"
        assert stats["new_products"] == 2  # dual-listed + mislabeled excluded
    finally:
        store.close()


def test_repeat_unchanged_run_produces_no_new_events(tmp_path):
    store = SqliteStore(str(tmp_path / "lava_experimental.db"))
    try:
        run_experimental(
            LavaCollector(fetcher=FakeFetcher(_standard_routes())), store,
            manufacturer="Lava", source_type="catalogue", region="india", base_url=BASE,
        )
        result, stats = run_experimental(
            LavaCollector(fetcher=FakeFetcher(_standard_routes())), store,
            manufacturer="Lava", source_type="catalogue", region="india", base_url=BASE,
        )
        assert result.status == "ok"
        assert stats["events_created"] == 0
        assert stats["new_products"] == 0
    finally:
        store.close()
