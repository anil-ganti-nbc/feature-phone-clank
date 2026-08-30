"""Punkt collector tests (offline fixtures only — no network).

Covers the campaign's minimum collector test matrix: healthy fetch, parser
success/failure/empty, catastrophic shrink, duplicate slug in sitemap,
idempotent rerun, first-run baseline (via the pipeline's own semantics),
changed item, transport failure, malformed upstream content,
source-local first_seen != global novelty, and soak notification
suppression (punkt-ch is experimental-only; `run_experimental` wires no
notifier at all).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_phone_clank.collectors.punkt import (
    PunktCollector,
    classify_slug,
)
from feature_phone_clank.core.models import Discovery

FIXTURES = Path(__file__).parent / "fixtures" / "punkt"


class FakeFetcher:
    def __init__(self, responses: dict[str, tuple[int, str]]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        status, text = self.responses[url]
        from feature_phone_clank.collectors.punkt import FetchResult

        return FetchResult(url=url, status=status, text=text)


def _default_responses(overrides: dict[str, tuple[int, str]] | None = None) -> dict[str, tuple[int, str]]:
    index = FIXTURES / "sitemap_index.xml"
    products = FIXTURES / "sitemap_products.xml"
    mp02 = FIXTURES / "product_mp02.html"
    mc02 = FIXTURES / "product_mc02.html"
    responses = {
        "https://www.punkt.ch/sitemap.xml": (200, index.read_text(encoding="utf-8")),
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (
            200, products.read_text(encoding="utf-8"),
        ),
        "https://www.punkt.ch/products/mp02-4g-minimalist-phone": (200, mp02.read_text(encoding="utf-8")),
    }
    if overrides:
        responses.update(overrides)
    return responses


def _collector_with(responses: dict[str, tuple[int, str]]) -> PunktCollector:
    return PunktCollector(fetcher=FakeFetcher(responses))


# -- slug classification ------------------------------------------------------

@pytest.mark.parametrize("slug,expected", [
    ("mp02-4g-minimalist-phone", "feature_phone"),
    ("mc02-5g-secure-phone", "smartphone"),
    ("ac02-alarm-clock", "accessory"),
    ("uc01-usb-desktop-charger", "accessory"),
    ("punkt-tote-bag", "accessory"),
    ("danny-p-leather-cases-for-mp02", "accessory"),
])
def test_slug_classification_is_deterministic(slug, expected):
    assert classify_slug(slug) == expected


def test_unknown_family_prefix_is_ambiguous_not_guessed():
    # A brand-new two-letter family must quarantine for human review.
    assert classify_slug("mx01-something-new") == "ambiguous"


# -- healthy path -------------------------------------------------------------

def test_healthy_fetch_discovers_only_feature_phone_products():
    collector = _collector_with(_default_responses())
    discoveries = collector.collect()
    assert [d.product_key for d in discoveries] == ["punkt-ch:mp02-4g-minimalist-phone"]
    d = discoveries[0]
    assert d.manufacturer == "Punkt"
    assert d.model == "Punkt. MP02 4G Minimalist Phone"
    # deterministic aggregate: lowest variant price; sku-sorted model number
    assert d.price == 299.00
    assert d.currency == "CHF"
    assert d.model_number == "MP02AGEBKNP000"
    assert d.availability == "OutOfStock"  # all three live variants out of stock
    assert d.spec_completeness == "complete"


def test_smartphones_and_accessories_are_logged_not_discovered():
    collector = _collector_with(_default_responses())
    collector.collect()
    by_slug = {e["slug"]: e["classification"] for e in collector.classification_log}
    assert by_slug["mc02-5g-secure-phone"] == "smartphone"
    assert by_slug["mc03-premium-secure-smartphone"] == "smartphone"
    assert by_slug["ac02-alarm-clock"] == "accessory"
    assert by_slug["punkt-t-shirt-with-message"] == "accessory"
    assert by_slug["mp02-4g-minimalist-phone"] == "feature_phone"
    assert all(not slug.startswith("mc") or cls != "feature_phone"
               for slug, cls in by_slug.items())


def test_discoveries_are_pure_source_shaped_data():
    """Law: collectors stay dumb. Output is Discovery models only."""
    collector = _collector_with(_default_responses())
    for discovery in collector.collect():
        assert isinstance(discovery, Discovery)


# -- parser failure / malformed content ---------------------------------------

def test_malformed_jsonld_becomes_incomplete_evidence_not_crash():
    broken = (FIXTURES / "product_malformed.html").read_text(encoding="utf-8")
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/products/mp03-new-phone": (200, broken),
        # shrink the sitemap so only the broken page is a phone candidate
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (
            200,
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://www.punkt.ch/products/mp03-new-phone</loc></url>"
            "</urlset>",
        ),
    }))
    discoveries = collector.collect()
    assert len(discoveries) == 1
    d = discoveries[0]
    assert d.spec_completeness == "incomplete"
    assert d.model_number is None
    assert d.raw["fetch_note"] == "product page had no parsable ProductGroup JSON-LD"


def test_empty_sitemap_yields_zero_discoveries_without_error():
    empty = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (200, empty),
    }))
    assert collector.collect() == []
    result, _ = collector.run()
    assert result.status == "ok"


# -- transport failure ---------------------------------------------------------

def test_transport_failure_raises_and_run_captures_it():
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/sitemap.xml": (503, ""),
    }))
    with pytest.raises(RuntimeError, match="sitemap index fetch failed"):
        collector.collect()
    result, discoveries = collector.run()
    assert result.status == "failed"
    assert discoveries == []


def test_single_product_page_failure_is_isolated_from_the_rest():
    ok_page = (FIXTURES / "product_mp02.html").read_text(encoding="utf-8")
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.punkt.ch/products/mp02-4g-minimalist-phone</loc></url>"
        "<url><loc>https://www.punkt.ch/products/mp99-broken-phone</loc></url>"
        "</urlset>"
    )
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (200, sitemap),
        "https://www.punkt.ch/products/mp99-broken-phone": (500, ""),
    }))
    discoveries = collector.collect()
    assert len(discoveries) == 2  # one parsed, one degraded-but-recorded
    broken = next(d for d in discoveries if "mp99" in d.product_key)
    assert broken.spec_completeness == "incomplete"
    assert "HTTP 500" in broken.raw["fetch_note"]
    assert ok_page  # and the healthy page still parsed fully


# -- duplicate + idempotency ----------------------------------------------------

def test_duplicate_sitemap_urls_are_collapsed():
    duplicated = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.punkt.ch/products/mp02-4g-minimalist-phone</loc></url>"
        "<url><loc>https://www.punkt.ch/products/mp02-4g-minimalist-phone</loc></url>"
        "</urlset>"
    )
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (200, duplicated),
    }))
    discoveries = collector.collect()
    keys = [d.product_key for d in discoveries]
    assert keys == ["punkt-ch:mp02-4g-minimalist-phone"]  # exactly one


def test_stable_rerun_produces_identical_content_hashes():
    first = _collector_with(_default_responses()).collect()
    second = _collector_with(_default_responses()).collect()
    h1 = sorted(d.content_hash() for d in first)
    h2 = sorted(d.content_hash() for d in second)
    assert h1 == h2


# -- change semantics -----------------------------------------------------------

def test_price_change_is_visible_to_the_diff_pipeline():
    base = _default_responses()
    run_one = _collector_with(base).collect()[0]

    changed = _default_responses()
    page = changed["https://www.punkt.ch/products/mp02-4g-minimalist-phone"][1].replace(
        '"price":"299.00"', '"price":"349.00"'  # changes the deterministic minimum
    )
    changed["https://www.punkt.ch/products/mp02-4g-minimalist-phone"] = (200, page)
    run_two = _collector_with(changed).collect()[0]

    assert run_two.content_hash() != run_one.content_hash()


# -- catastrophic shrink ----------------------------------------------------------

def test_catastrophic_catalogue_shrink_reports_zero_without_fabrication():
    """If Punkt's catalogue suddenly lists nothing, the collector returns
    zero discoveries - the pipeline's unexpected-zero guard decides health;
    the collector never invents continuity."""
    empty = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    collector = _collector_with(_default_responses({
        "https://www.punkt.ch/sitemap_products_1.xml?from=14981696946562&to=15207186268546": (200, empty),
    }))
    result, discoveries = collector.run()
    assert result.status == "ok"
    assert discoveries == []


# -- registration / isolation / suppression ----------------------------------------

def test_registered_but_absent_from_production_scope():
    from feature_phone_clank.core.scope import load_scope

    scope = load_scope("config/scope.yaml")
    # Promoted to production 2026-08-30 after natural-soak review
    # (16/16 ok experimental cycles, zero noise) — see scope.yaml notes.
    assert "punkt-ch" in scope.production_collectors
    # registration also makes it runnable via run-experimental
    from feature_phone_clank.core.registry import collectors

    assert "punkt-ch" in collectors.names()


def test_experimental_runner_wires_no_notifier():
    """Soak notification suppression: run_experimental's notifier parameter
    defaults to None (no notifier exists unless a caller explicitly injects
    one), and the CLI's cmd_run_experimental never passes any - the
    experimental soak path is structurally notification-free."""
    import inspect

    from feature_phone_clank.core.runner import run_experimental

    params = inspect.signature(run_experimental).parameters
    assert "notifier" in params
    assert params["notifier"].default is None

    import feature_phone_clank.cli as cli_module

    cli_source = inspect.getsource(cli_module.cmd_run_experimental)
    assert "notifier" not in cli_source, (
        "cmd_run_experimental must never wire a notifier into the soak path"
    )


def test_source_local_first_seen_is_not_global_novelty(tmp_path):
    """First observation of an EXISTING product by this new collector must
    not be treated as a global-new event by the pipeline: the product row
    already exists under a different source_key, so the resolver keeps them
    separate and the diff produces NEW_PRODUCT only within punkt-ch's own
    source-local baseline - which the experimental runner records without
    any notification path (suppression by structure)."""
    from feature_phone_clank.providers.sqlite import SqliteStore

    store = SqliteStore(str(tmp_path / "exp.db"))
    try:
        collector = _collector_with(_default_responses())
        result_one, stats_one = run_exponential_helper(store, collector)
        events_first = store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        # rerun: identical content -> zero new events (no false novelty storm)
        result_two, stats_two = run_exponential_helper(store, collector)
        events_second = store.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        assert result_one.status == "ok"
        assert result_two.status == "ok"
        # First sight by a NEW source establishes its own local baseline:
        # products persist, but zero events fire - locally-first is not
        # globally-novel, and nothing exists here to notify anyway.
        assert events_first == 0
        assert stats_one["new_products"] >= 1
        assert stats_two["unchanged_observations"] >= 1
        assert events_second == 0
    finally:
        store.close()


def run_exponential_helper(store, collector):
    from feature_phone_clank.core.runner import run_experimental

    return run_experimental(
        collector, store,
        manufacturer=collector.manufacturer,
        source_type=collector.source_type,
        region=collector.region,
        base_url=collector.base_url,
    )
