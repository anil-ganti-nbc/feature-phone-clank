"""itel collector tests (Stage E1, EXPERIMENTAL). Fixture-based only — no
live network calls, no headless browser launched in the automated suite
(mirrors user constraint 7, same as test_hmd_collector.py). Covers: a valid
feature phone, smartphone rejection, cross-listing conflict/ambiguity,
baseline no-alert via run_experimental, a page-load failure, and repeat
unchanged runs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "itel"))
from cards import (  # noqa: E402
    ACE_2_HEERA_SPEC_ROWS, FEATURE_PHONE_CARDS, IT2165C_SPEC_ROWS, SMARTPHONE_CARDS,
    SUPER_GURU_4G_SPEC_ROWS,
)

from feature_phone_clank.collectors.itel import BASE, ItelCollector
from feature_phone_clank.core.runner import run_experimental
from feature_phone_clank.providers.sqlite import SqliteStore


class FakeItelFetcher:
    def __init__(self, fp_cards, sp_cards, spec_rows: dict[str, list[tuple[str, str]] | None]):
        self._fp_cards = fp_cards
        self._sp_cards = sp_cards
        self._spec_rows = spec_rows
        self.requested_specs: list[str] = []

    def get_cards(self, listing_url: str) -> list[dict]:
        if listing_url.endswith("/featurephones"):
            return self._fp_cards
        if listing_url.endswith("/smartphones"):
            return self._sp_cards
        raise AssertionError(f"unexpected listing url: {listing_url}")

    def get_spec_rows(self, product_url: str):
        self.requested_specs.append(product_url)
        slug = product_url.rsplit("/", 1)[-1]
        return self._spec_rows.get(slug)


def _standard_fetcher() -> FakeItelFetcher:
    return FakeItelFetcher(
        FEATURE_PHONE_CARDS, SMARTPHONE_CARDS,
        {
            "super-guru-4g": SUPER_GURU_4G_SPEC_ROWS,
            "it2165c": IT2165C_SPEC_ROWS,
            "ace-3-heera": None,  # page-load failure
            "flip-one": [],  # loaded but no specifications block
            "ace-2-heera": ACE_2_HEERA_SPEC_ROWS,
        },
    )


# -- mode 1: valid feature phone -----------------------------------------

def test_valid_feature_phone_is_discovered_and_parsed():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "itel-india:super-guru-4g")
    assert d.manufacturer == "itel"
    assert d.model == "Super Guru 4G"
    assert d.model_number == "Super Guru 4G"
    assert d.url == f"{BASE}/product/super-guru-4g"
    assert d.fields["battery"]["values"] == ["1000 mAH"]
    assert d.fields["display"]["values"] == ['5.09 cm(2")']
    # General-tab-only capture is a known V1 limit — always "incomplete"
    assert d.spec_completeness == "incomplete"


def test_clean_spec_table_name_wins_over_noisy_concatenated_card_text():
    """Regression test for a real bug caught during the 2026-08-18 live
    baseline: a listing-card anchor's text can concatenate the product name
    with its marketing blurb and price with no separator at all. The spec
    table's "Model Name" row must win over that noisy card text."""
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "itel-india:ace-2-heera")
    assert d.model == "Ace 2 Heera"
    assert "Big Display" not in d.model
    assert "1,109" not in d.model
    # the noisy original is still retained as raw evidence, not discarded
    assert "Big Display" in d.raw["card_name"]


def test_new_badge_is_stripped_from_name_and_recorded():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "itel-india:ace-3-heera")
    assert d.model == "Ace 3 Heera"  # "new" prefix stripped
    assert d.raw["is_new_badge"] is True


# -- mode 2: smartphone rejection ------------------------------------------

def test_smartphone_only_products_are_not_discovered():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    keys = {d.product_key for d in discoveries}
    assert "itel-india:zeno-100-pro" not in keys
    assert "itel-india:a100-pro" not in keys


def test_smartphone_rejection_is_logged():
    collector = ItelCollector(fetcher=_standard_fetcher())
    collector.collect()

    zeno = next(e for e in collector.classification_log if e["slug"] == "zeno-100-pro")
    assert zeno["classification"] == "smartphone"


# -- mode 3: ambiguous / cross-listing conflict -----------------------------

def test_slug_on_both_listings_is_quarantined_not_guessed():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    # it2165c appears on BOTH listings in this fixture set — must be
    # quarantined as ambiguous, never silently treated as the feature-phone
    # card from the first (feature-phones) discovery pass
    keys = {d.product_key for d in discoveries}
    assert "itel-india:it2165c" not in keys

    entry = next(e for e in collector.classification_log if e["slug"] == "it2165c")
    assert entry["classification"] == "ambiguous"
    assert entry["evidence"]["listing_membership"] == "both"


# -- mode 4: incomplete / page-load failure ---------------------------------

def test_product_page_load_failure_still_yields_discovery_with_card_name():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "itel-india:ace-3-heera")
    assert d.model == "Ace 3 Heera"
    assert d.fields == {}
    assert d.spec_completeness == "incomplete"
    assert "failed to load" in d.raw["fetch_note"]


def test_loaded_page_with_no_specifications_block_is_empty_fields_not_error():
    collector = ItelCollector(fetcher=_standard_fetcher())
    discoveries = collector.collect()

    d = next(d for d in discoveries if d.product_key == "itel-india:flip-one")
    assert d.fields == {}
    assert d.raw["fetch_note"] is None


# -- mode 5: baseline / repeat run via run_experimental (isolation) --------

def test_baseline_run_via_run_experimental_does_not_touch_production_db(tmp_path):
    store = SqliteStore(str(tmp_path / "itel_experimental.db"))
    try:
        collector = ItelCollector(fetcher=_standard_fetcher())
        result, stats = run_experimental(
            collector, store, manufacturer="itel", source_type="catalogue",
            region="india", base_url=BASE,
        )
        assert result.status == "ok"
        assert stats["new_products"] == 4  # it2165c excluded (ambiguous)
    finally:
        store.close()


def test_repeat_unchanged_run_produces_no_new_events(tmp_path):
    store = SqliteStore(str(tmp_path / "itel_experimental.db"))
    try:
        run_experimental(
            ItelCollector(fetcher=_standard_fetcher()), store,
            manufacturer="itel", source_type="catalogue", region="india", base_url=BASE,
        )
        result, stats = run_experimental(
            ItelCollector(fetcher=_standard_fetcher()), store,
            manufacturer="itel", source_type="catalogue", region="india", base_url=BASE,
        )
        assert result.status == "ok"
        assert stats["events_created"] == 0
        assert stats["new_products"] == 0
    finally:
        store.close()
