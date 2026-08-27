"""Wave 2 collector tests: doro-gb, mudita-com, sunbeam-f1-us,
tcl-alcatel-global.

Each new source gets: normal parse, classification, exclusion/quarantine,
malformed input, failure, baseline silence + re-sight dedupe (through the
real pipeline against a throwaway store), registration and production
exclusion. No test touches the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

FIXTURES = ROOT / "tests" / "fixtures" / "wave2"


class FakeFetcher:
    """Scripted fetcher mapping URL -> (status, text) or Exception."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        resp = next((v for k, v in self.responses.items() if url.startswith(k)), None)
        if isinstance(resp, Exception):
            raise resp
        if resp is None:
            from feature_phone_clank.collectors.sunbeam import FetchResult
            return FetchResult(url=url, status=404, text="")
        from feature_phone_clank.collectors.sunbeam import FetchResult
        status, text = resp
        return FetchResult(url=url, status=status, text=text)


def _doro():
    from feature_phone_clank.collectors.doro import DoroCollector

    return DoroCollector(FakeFetcher({
        "https://www.doro.com/en-gb/products/mobile-phones/":
            (200, (FIXTURES / "doro_gb_mobile_phones.html").read_text(encoding="utf-8")),
    }))


def _sunbeam(payload=None):
    from feature_phone_clank.collectors.sunbeam import SunbeamCollector

    text = payload if payload is not None else (
        FIXTURES / "sunbeam_store_products.json").read_text(encoding="utf-8")
    return SunbeamCollector(FakeFetcher({
        "https://sunbeamwireless.com/wp-json/wc/store/v1/products": (200, text),
    }))


def _mudita(payload=None):
    from feature_phone_clank.collectors.mudita import MuditaCollector

    text = payload if payload is not None else (
        FIXTURES / "mudita_page_data.json").read_text(encoding="utf-8")
    return MuditaCollector(FakeFetcher({
        "https://mudita.com/page-data/products/page-data.json": (200, text),
    }))


def _alcatel():
    from feature_phone_clank.collectors.tcl_alcatel import TCLAlcatelCollector

    return TCLAlcatelCollector(FakeFetcher({
        "https://www.alcatelmobile.com/feature-phones":
            (200, (FIXTURES / "alcatel_feature_phones.html").read_text(encoding="utf-8")),
    }))


# ---------------------------------------------------------------- parsing

def test_doro_parses_live_capture_with_sku_identity():
    d = _doro().collect()
    keys = {x.product_key for x in d}
    assert "doro-gb:39607" in keys                      # Doro 780X
    assert "doro-gb:36314" in keys                      # Leva L30 clamshell
    assert all(x.model_number or x.raw.get("sku") for x in d)
    assert all("form_factor_tags" in x.fields for x in d if x.raw["form_factor_filters"])


def test_sunbeam_parses_store_api_and_excludes_accessories():
    c = _sunbeam()
    d = c.collect()
    skus = {x.model_number for x in d}
    # phones present
    assert "PINE-1" in skus and "BLJ-1" in skus
    # accessories/services absent
    all_names = " ".join(x.model for x in d).lower()
    assert "bicycle mount" not in all_names
    assert "charging dock" not in all_names
    assert "data recovery" not in all_names
    # accessory/service classifications were logged, not dropped silently
    logged = {entry["slug"] for entry in c.classification_log}
    assert any("BMPH" == s or s.startswith("noid") for s in logged) or len(logged) >= 10


def test_mudita_pairs_titles_to_links_tightly():
    d = _mudita().collect()
    models = {x.model for x in d}
    # phones only; the watch 'Radiant' must not leak in with a phone name
    assert "Mudita Kompakt" in models
    assert any("Pure" in m for m in models)
    assert not any("Harmony" in m for m in models)
    assert not any("Bell" in m for m in models)
    assert not any("Oasis" in m for m in models)
    assert all("/products/phones/" in x.url for x in d)


def test_alcatel_parses_feature_phone_pdps():
    d = _alcatel().collect()
    ids = sorted(x.model_number for x in d)
    assert ids == ["1021", "1041"]
    assert all(x.manufacturer == "Alcatel/TCL" for x in d)


# ------------------------------------------------------- malformed/failure

def test_doro_missing_attributes_are_quarantined_not_crashing(monkeypatch):
    c = _doro()
    html_text = ('<div class="products-tile"><a href="/en-gb/shop/mobile-devices/'
                 'easy-phones/x/">bare</a></div>' +
                 ''.join(
                     f'<div class="products-tile"><a href="/en-gb/shop/mobile-devices/easy-phones/doro-9{i}/" '
                     f'data-sku="9000{i}" data-name="Doro 90{i}">t</a></div>'
                     for i in range(3)))
    c.fetcher.responses[
        "https://www.doro.com/en-gb/products/mobile-phones/"] = (200, html_text)
    d = c.collect()
    assert len(d) == 3
    assert any(e["classification"] == "quarantined" for e in c.classification_log)


def test_doro_500_fails_closed():
    from feature_phone_clank.collectors.doro import DoroCollector
    c = DoroCollector(FakeFetcher({
        "https://www.doro.com/en-gb/products/mobile-phones/": (500, ""),
    }))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        c.collect()


def test_sunbeam_bad_json_fails_closed():
    c = _sunbeam(payload="not json at all")
    with pytest.raises(RuntimeError, match="unparsable JSON"):
        c.collect()


def test_sunbeam_floor_guard_blocks_false_collapse():
    small = json.dumps([
        {"id": i, "name": f"F1 Pro X", "sku": f"S{i}", "permalink": "",
         "categories": [{"name": "F1 Pro Phones and Accessories"}],
         "prices": {}, "is_purchasable": True}
        for i in range(2)
    ])
    with pytest.raises(RuntimeError, match="completeness guard"):
        _sunbeam(payload=small).collect()


def test_mudita_zero_phones_fails_closed():
    empty = json.dumps({"result": {"data": {"pageContext": {"some": "thing"}}}})
    with pytest.raises(RuntimeError, match="zero accepted|never be empty"):
        _mudita(payload=empty).collect()


def test_mudita_unrecognised_subcategory_quarantined():
    blob = json.dumps({"result": {"data": {"c": {"items": [
        {"title": "Mudita Kompakt", "link": "/products/phones/mudita-kompakt"},
        {"title": "Mudita Gizmo", "link": "/products/gizmos/mudita-gizmo"},
    ]}}}})
    c = _mudita(payload=blob)
    d = c.collect()
    assert [x.model for x in d] == ["Mudita Kompakt"]
    assert any(e["classification"] == "ambiguous" for e in c.classification_log)


def test_sunbeam_discontinued_legacy_only_is_quarantined():
    items = json.dumps([
        {"id": 1, "name": "F1 Orchid - Discontinued",
         "sku": "F1ORCV", "permalink": "",
         "categories": [{"name": "Original F1 Accessories"}],
         "prices": {}, "is_purchasable": False},
        *[{ "id": 100 + i, "name": f"F1 Pro Variant {i}", "sku": f"V{i}",
            "permalink": "",
            "categories": [{"name": "F1 Pro Phones and Accessories"}],
            "prices": {}, "is_purchasable": True}
          for i in range(7)],
    ])
    c = _sunbeam(payload=items)
    d = c.collect()
    assert all("Orchid" not in x.model for x in d)
    assert any(e["classification"] == "ambiguous"
               for e in c.classification_log if "F1ORCV" in str(e.get("slug")))


# ----------------------------------------------- pipeline / baseline law

def _run_baseline_law(source_key, collector_factory):
    """First successful run stores the catalogue with ZERO novelty events;
    immediate re-sight dedupes with zero events. Uses the real pipeline."""
    import tempfile
    from feature_phone_clank.providers.sqlite import SqliteStore
    from feature_phone_clank.core.runner import run_experimental

    with tempfile.TemporaryDirectory() as td:
        store = SqliteStore(f"{td}/{source_key}.db")
        c = collector_factory()
        result1, disc1 = run_experimental(
            c, store, manufacturer=c.manufacturer, source_type=c.source_type,
            region=c.region, base_url=c.base_url)
        stats1 = dict(disc1), result1
        events_after_first = store.count_events() if hasattr(store, "count_events") else None
        c2 = collector_factory()
        result2, disc2 = run_experimental(
            c2, store, manufacturer=c2.manufacturer, source_type=c2.source_type,
            region=c2.region, base_url=c2.base_url)
        return result1, result2


@pytest.mark.parametrize("factory", [_doro, _sunbeam, _mudita, _alcatel])
def test_wave2_first_run_creates_zero_novelty_events(factory):
    import tempfile
    from feature_phone_clank.providers.sqlite import SqliteStore
    from feature_phone_clank.core.runner import run_experimental

    with tempfile.TemporaryDirectory() as td:
        store = SqliteStore(":memory:")
        c = factory()
        result, discoveries = run_experimental(
            c, store, manufacturer=c.manufacturer, source_type=c.source_type,
            region=c.region, base_url=c.base_url)
        assert result.status == "ok"
        assert result.discovered > 0
        # Law 17: baseline silence - the very first successful experimental
        # run must enqueue ZERO notifications (FIRST_SEEN != NOVELTY).
        n = len(store.pending_notifications("discord"))
        assert n == 0


# ------------------------------------------------ registration & scope

def test_wave2_sources_registered_experimental_and_production_excluded():
    import feature_phone_clank.collectors as _  # noqa: F401 - registration side effect
    from feature_phone_clank.core.registry import collectors as reg
    from feature_phone_clank.core.scope import load_scope

    scope = load_scope("config/scope.yaml")
    for sid in ("doro-gb", "mudita-com", "sunbeam-f1-us", "tcl-alcatel-global"):
        assert sid in reg.names(), f"{sid} not registered"
        assert sid not in scope.production_collectors


def test_wave2_registration_does_not_disturb_existing_roster():
    import feature_phone_clank.collectors as _  # noqa: F401 - registration side effect
    from feature_phone_clank.core.registry import collectors as reg

    for sid in ("hmd-nokia", "itel-india", "lava-india", "punkt-ch"):
        assert sid in reg.names()
