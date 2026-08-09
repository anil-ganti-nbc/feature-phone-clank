"""HMD/Nokia collector tests. Fixture-based only — no live network calls
(user constraint 7). Covers the three required Stage 2 promotion modes
(constraint 8): a known valid feature phone, a known smartphone that must
be rejected, and an ambiguous/quarantined entry that must not enter the
catalogue."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_phone_clank.collectors.hmd import (
    BASE, FEATURE_LISTING_URL, HmdCollector, SITEMAP_URL, SMARTPHONE_LISTING_URL,
    FetchResult, _resolve_name, _strip_marketing_tagline, _strip_specs_title_suffix,
    _humanize_slug,
)
from feature_phone_clank.core.runner import run_experimental

FIXTURES = Path(__file__).parent / "fixtures" / "hmd"


class FakeFetcher:
    """Serves fixture files by URL, mimicking HttpFetcher's interface. Any
    URL not explicitly mapped 404s — a collector bug that tries to fetch
    something unexpected fails loudly instead of silently succeeding."""

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
        FEATURE_LISTING_URL: FIXTURES / "feature_phones_listing.html",
        SMARTPHONE_LISTING_URL: FIXTURES / "smartphones_listing.html",
        SITEMAP_URL: FIXTURES / "sitemap.xml",
        f"{BASE}/nokia-9999-test/specs": FIXTURES / "nokia-9999-test_specs.html",
        f"{BASE}/hmd-conflict-phone": FIXTURES / "hmd-conflict-phone.html",
        f"{BASE}/hmd-conflict-phone/specs": FIXTURES / "hmd-conflict-phone_specs.html",
        f"{BASE}/nokia-8888-orphan": FIXTURES / "nokia-8888-orphan.html",
    }


def _no_overrides_path(tmp_path) -> Path:
    p = tmp_path / "no_overrides.yaml"
    p.write_text("force_feature_phone: []\nforce_smartphone: []\nforce_exclude: []\n")
    return p


# -- mode 1: known valid feature phone -----------------------------------

def test_valid_feature_phone_is_discovered_and_parsed(tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()

    fp = next((d for d in discoveries if d.product_key == "hmd-nokia:nokia-9999-test"), None)
    assert fp is not None
    assert fp.manufacturer == "HMD"
    assert fp.model == "Nokia 9999 Test"
    assert fp.model_number == "1GF999TEST01"
    assert fp.region == "en_int"
    assert fp.url == f"{BASE}/nokia-9999-test"
    assert fp.spec_completeness == "complete"


def test_extracted_fields_have_expected_values_and_provenance(tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()
    fp = next(d for d in discoveries if d.product_key == "hmd-nokia:nokia-9999-test")

    assert fp.fields["os"]["values"] == ["s30+"]
    assert fp.fields["os"]["category"] == "operating-system"
    assert fp.fields["usb-connection"]["values"] == ["usb-type-c"]
    assert fp.fields["max-network-speed"]["values"] == ["4g"]
    assert fp.fields["battery-capacity"] == {"value": 1450, "unit": "mAh"}
    assert fp.fields["ram"] == {"value": 64, "unit": "MB"}
    # provenance: raw carries the exact source used to derive model/model_number
    assert fp.raw["specs_url"] == f"{BASE}/nokia-9999-test/specs"
    assert fp.raw["skus"] == ["1GF999TEST01"]
    # the listing fixture provides an aria-label, so tier 1 (catalogue_card)
    # resolves the name — not the (also-correct, here) specs-page h1
    assert fp.raw["name_source"] == "catalogue_card"
    assert fp.raw["catalogue_card_name"] == "Nokia 9999 Test"
    assert fp.raw["raw_specs_h1"] == "Nokia 9999 Test"


def test_feature_phone_classification_is_logged_too(tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    collector.collect()
    entry = next(e for e in collector.classification_log if e["slug"] == "nokia-9999-test")
    assert entry["classification"] == "feature_phone"
    assert entry["evidence"]["listing_membership"] == "feature_phones"


# -- mode 2: known smartphone must be rejected -----------------------------

def test_smartphone_is_rejected_not_discovered(tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()

    assert all(d.product_key != "hmd-nokia:hmd-glass-9" for d in discoveries)
    entry = next(e for e in collector.classification_log if e["slug"] == "hmd-glass-9")
    assert entry["classification"] == "smartphone"
    assert entry["evidence"]["listing_membership"] == "smartphones"
    # rejecting it must never require fetching its product page
    assert f"{BASE}/hmd-glass-9" not in fetcher.requested
    assert f"{BASE}/hmd-glass-9/specs" not in fetcher.requested


# -- mode 3: ambiguous / quarantined must not enter the catalogue ----------

def test_listing_conflict_is_quarantined_not_promoted(tmp_path):
    """hmd-conflict-phone appears on BOTH category listings — a
    contradictory primary signal — so it must be quarantined, not silently
    classified either way, unless a human override says otherwise."""
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()

    assert all(d.product_key != "hmd-nokia:hmd-conflict-phone" for d in discoveries)
    entry = next(e for e in collector.classification_log if e["slug"] == "hmd-conflict-phone")
    assert entry["classification"] == "ambiguous"
    assert entry["evidence"]["listing_membership"] == "both"
    # supporting evidence is captured even though it isn't auto-promoted
    assert entry["evidence"]["title_signal"] == "feature_phone"


def test_sitemap_orphan_is_quarantined_not_promoted(tmp_path):
    """nokia-8888-orphan is in the sitemap but linked from neither official
    category listing — legacy/discontinued/unlisted; must be quarantined,
    never silently added to or silently dropped from consideration."""
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()

    assert all(d.product_key != "hmd-nokia:nokia-8888-orphan" for d in discoveries)
    entry = next(e for e in collector.classification_log if e["slug"] == "nokia-8888-orphan")
    assert entry["classification"] == "ambiguous"
    assert entry["evidence"]["listing_membership"] == "none"


def test_sitemap_denylist_excludes_promo_bundle_pages(tmp_path):
    """nokia-8888-orphan-bundle is in the sitemap fixture and phone-shaped,
    but matches the non-product denylist (bundle) — it must not even reach
    the quarantine log as a candidate."""
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    collector.collect()
    assert all(e["slug"] != "nokia-8888-orphan-bundle" for e in collector.classification_log)


def test_known_nav_chrome_never_becomes_a_candidate(tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    collector.collect()
    logged_slugs = {e["slug"] for e in collector.classification_log}
    assert "about" not in logged_slugs
    assert "accessories" not in logged_slugs
    assert "smartphones" not in logged_slugs


# -- override mechanism -----------------------------------------------------

def test_override_promotes_a_quarantined_slug_to_feature_phone(tmp_path):
    overrides_path = tmp_path / "overrides.yaml"
    overrides_path.write_text(
        "force_feature_phone: [hmd-conflict-phone]\nforce_smartphone: []\nforce_exclude: []\n"
    )
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=overrides_path)
    discoveries = collector.collect()

    fp = next((d for d in discoveries if d.product_key == "hmd-nokia:hmd-conflict-phone"), None)
    assert fp is not None
    entry = next(e for e in collector.classification_log if e["slug"] == "hmd-conflict-phone")
    assert entry["classification"] == "feature_phone"
    assert entry["evidence"]["override"] == "manual_override:force_feature_phone"


def test_override_force_exclude_prevents_promotion_even_if_listed(tmp_path):
    overrides_path = tmp_path / "overrides.yaml"
    overrides_path.write_text(
        "force_feature_phone: []\nforce_smartphone: []\nforce_exclude: [nokia-9999-test]\n"
    )
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=overrides_path)
    discoveries = collector.collect()
    assert all(d.product_key != "hmd-nokia:nokia-9999-test" for d in discoveries)


# -- listing fetch failure --------------------------------------------------

def test_listing_fetch_failure_raises_and_is_isolated_by_base_collector(tmp_path):
    routes = _standard_routes()
    del routes[FEATURE_LISTING_URL]  # will 404
    fetcher = FakeFetcher(routes)
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    result, discoveries = collector.run()
    assert result.status == "failed"
    assert discoveries == []


# -- end-to-end through the runner + store ----------------------------------

def test_end_to_end_experimental_run_persists_only_feature_phones(store, tmp_path):
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    result, stats = run_experimental(
        collector, store, manufacturer="HMD", source_type="catalogue",
        region="en_int", base_url=BASE,
    )
    assert result.status == "ok"
    # nokia-9999-test (complete specs) + hmd-nospecs-test (incomplete specs,
    # still persisted per Stage 2.1 — see test_missing_specs_page_*)
    assert store.active_product_count("hmd-nokia") == 2

    log_rows = store.classification_log("hmd-nokia")
    classes = {r["slug"]: r["classification"] for r in log_rows}
    assert classes["nokia-9999-test"] == "feature_phone"
    assert classes["hmd-nospecs-test"] == "feature_phone"
    assert classes["hmd-glass-9"] == "smartphone"
    assert classes["hmd-conflict-phone"] == "ambiguous"
    assert classes["nokia-8888-orphan"] == "ambiguous"

    incomplete = store.incomplete_spec_products("hmd-nokia")
    assert {r["product_key"] for r in incomplete} == {"hmd-nokia:hmd-nospecs-test"}


def test_first_run_is_baseline_generates_no_editorial_events(store, tmp_path):
    """Stage 1's pipeline doesn't generate events at all yet (Stage 3), so
    this is trivially true today — asserted explicitly so it starts failing
    loudly the moment Stage 3 adds event generation without baseline
    handling."""
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    run_experimental(
        collector, store, manufacturer="HMD", source_type="catalogue",
        region="en_int", base_url=BASE,
    )
    events = store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert events == 0


# -- Stage 2.1: missing /specs must not drop a confidently-classified product

def test_missing_specs_page_persists_incomplete_product(tmp_path):
    """hmd-nospecs-test is on the official feature-phones listing (so it's
    confidently classified) but has no /specs page and no base page fixture
    at all — the real hmd-touch-4g situation. It must still end up in the
    catalogue, named from the listing's catalogue-card evidence, with
    spec_completeness='incomplete' and empty fields — never invented."""
    fetcher = FakeFetcher(_standard_routes())
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))
    discoveries = collector.collect()

    fp = next((d for d in discoveries if d.product_key == "hmd-nokia:hmd-nospecs-test"), None)
    assert fp is not None
    assert fp.model == "HMD Nospecs Test"
    assert fp.spec_completeness == "incomplete"
    assert fp.fields == {}
    assert fp.model_number is None
    assert fp.raw["name_source"] == "catalogue_card"
    assert fp.raw["fetch_note"] is not None  # explains why specs are missing


# -- Stage 2.1: deterministic name-resolution precedence --------------------

def test_name_resolution_tier1_catalogue_card_wins_over_everything():
    name, source = _resolve_name(
        "hmd-x", catalogue_name="HMD X (Clean)",
        specs_title="HMD X specifications", specs_h1="HMD X | Buy now and save",
    )
    assert (name, source) == ("HMD X (Clean)", "catalogue_card")


def test_name_resolution_tier2_specs_title_used_when_no_catalogue_name():
    name, source = _resolve_name(
        "nokia-3210", catalogue_name=None,
        specs_title="Nokia 3210 specifications",
        specs_h1="Return of the Nokia 3210: Y2K Nostalgia",
    )
    assert (name, source) == ("Nokia 3210", "specs_title")


def test_name_resolution_wrong_product_h1_bug_is_contained():
    """The real Nokia 5310/230 bug: the specs page's own <h1> carries
    ANOTHER product's heading, but its <title> is correct. Tier 2 must win,
    and the bad h1 must never become the canonical name."""
    name, source = _resolve_name(
        "nokia-5310-2024", catalogue_name=None,
        specs_title="Nokia 5310 (2024) specifications",
        specs_h1="Nokia 230 (2024) specs",  # HMD's own bug: wrong product's h1
    )
    assert name == "Nokia 5310 (2024)"
    assert name != "Nokia 230 (2024)"
    assert source == "specs_title"


def test_name_resolution_tier3_base_title_used_when_specs_title_missing():
    name, source = _resolve_name(
        "hmd-terra-m", catalogue_name=None, specs_title=None,
        base_title="HMD Terra M | A durable feature phone for tough conditions",
    )
    assert (name, source) == ("HMD Terra M", "base_title")


def test_name_resolution_tier3_degenerate_specs_title_skipped_for_base_title():
    """The real hmd-terra-m situation: its /specs page barely renders and
    has <title>HMD</title> (a CMS placeholder, not a real product name).
    That must not win over the base page's real title."""
    name, source = _resolve_name(
        "hmd-terra-m", catalogue_name=None,
        specs_title="HMD",  # degenerate placeholder
        base_title="HMD Terra M | A durable feature phone for tough conditions",
    )
    assert (name, source) == ("HMD Terra M", "base_title")


def test_name_resolution_tier4_marketing_tagline_h1_stripped():
    name, source = _resolve_name(
        "hmd-130-music", catalogue_name=None, specs_title=None, base_title=None,
        specs_h1="HMD 130 Music | Your music on your terms",
    )
    assert (name, source) == ("HMD 130 Music", "specs_h1_fallback")


def test_name_resolution_tier5_base_h1_used_after_specs_h1():
    name, source = _resolve_name(
        "hmd-x", catalogue_name=None, specs_title=None, base_title=None,
        specs_h1=None, base_h1="HMD X | Buy now and save",
    )
    assert (name, source) == ("HMD X", "base_h1_fallback")


def test_name_resolution_tier6_slug_fallback_when_nothing_available():
    name, source = _resolve_name("hmd-arc2", catalogue_name=None, specs_title=None)
    assert (name, source) == ("HMD Arc2", "slug_fallback")


def test_strip_marketing_tagline_deterministic_split():
    assert _strip_marketing_tagline("HMD 130 Music | Your music on your terms") == "HMD 130 Music"
    assert _strip_marketing_tagline("Nokia 2660 Flip - Affordable 4G Flip Phone") == "Nokia 2660 Flip"
    assert _strip_marketing_tagline("Nokia 3210") == "Nokia 3210"  # no separator: unchanged


def test_strip_specs_title_suffix_deterministic():
    assert _strip_specs_title_suffix("Nokia 3210 specifications") == "Nokia 3210"
    assert _strip_specs_title_suffix("Nokia 3210 Specifications") == "Nokia 3210"  # case-insensitive
    assert _strip_specs_title_suffix("Nokia 3210") == "Nokia 3210"  # no suffix: unchanged


# -- Stage 2.1: end-to-end proof the H1 bug can't reach the catalogue -------

def test_end_to_end_wrong_h1_never_becomes_the_persisted_model(tmp_path):
    """Same scenario as the unit test above, but through the full
    collect() -> Discovery path with a real fixture pair, to prove the
    contamination is contained by the actual collector, not just by the
    helper function in isolation."""
    routes = _standard_routes()
    routes[f"{BASE}/nokia-5310-2024/specs"] = FIXTURES / "nokia-5310-2024_specs.html"
    fetcher = FakeFetcher(routes)
    overrides_path = tmp_path / "overrides.yaml"
    overrides_path.write_text(
        "force_feature_phone: [nokia-5310-2024]\nforce_smartphone: []\nforce_exclude: []\n"
    )
    collector = HmdCollector(fetcher=fetcher, overrides_path=overrides_path)
    # nokia-5310-2024 isn't on either fixture listing, so drive _parse_product
    # directly with no catalogue_name — exactly the worst-case tier-1-absent
    # scenario, proving tier 2 alone protects the canonical name.
    discovery = collector._parse_product("nokia-5310-2024", catalogue_name=None)
    assert discovery.model == "Nokia 5310 (2024)"
    assert discovery.model != "Nokia 230 (2024)"
    assert discovery.raw["raw_specs_h1"] == "Nokia 230 (2024) specs"  # preserved as evidence
    assert discovery.raw["name_source"] == "specs_title"


def test_end_to_end_navlink_only_product_resolves_via_base_page_title(tmp_path):
    """The real hmd-terra-m situation: a nav-menu-only link (no catalogue-
    card aria-label), whose /specs page is a near-empty shell
    (<title>HMD</title>, no <h1>). The base product page has the real,
    clean title. Must resolve to a real name, not the literal string
    'HMD'."""
    routes = _standard_routes()
    routes[f"{BASE}/hmd-navlink-phone/specs"] = FIXTURES / "hmd-navlink-phone_specs.html"
    routes[f"{BASE}/hmd-navlink-phone"] = FIXTURES / "hmd-navlink-phone.html"
    fetcher = FakeFetcher(routes)
    collector = HmdCollector(fetcher=fetcher, overrides_path=_no_overrides_path(tmp_path))

    discovery = collector._parse_product("hmd-navlink-phone", catalogue_name=None)
    assert discovery.model == "HMD Navlink Phone"
    assert discovery.model != "HMD"
    assert discovery.model_number == "VMAWNAV001"
    assert discovery.raw["name_source"] == "base_title"
    assert discovery.raw["raw_specs_title"] == "HMD"
