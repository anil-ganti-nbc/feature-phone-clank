"""Regressions from the accepted September 14 recon; no live GETs."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from feature_phone_clank.collectors.lava import (
    FetchResult, LavaCollector, _extract_spec_rows, _meaningful_alias,
)
from feature_phone_clank.collectors.sunbeam import (
    SunbeamCollector, _parse_price, classify_product, SERVICE_CATEGORY,
)
from feature_phone_clank.core.diff import diff_meaningful_fields

FIXTURES = Path(__file__).parent / "fixtures"
LAVA = FIXTURES / "lava" / "recon-2026-09-14"


class StaticFetcher:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url):
        return FetchResult(url=url, status=200, text=self.payload)


def parse_lava(slug, specs=None):
    captured = json.loads((LAVA / f"{slug}.json").read_text(encoding="utf-8"))
    data = {"props": {"pageProps": {"slugData": {"product_deatil": {
        "view_details_specs": captured["specs_html"] if specs is None else specs,
    }}}}}
    html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
    return LavaCollector(StaticFetcher(html))._parse_product(slug, captured["product"])


@pytest.mark.parametrize("slug,capacity", [
    ("a1-vibe", 1000), ("a3-vibe", 1750), ("a5-23", 1000), ("a7-torch", 2575),
])
def test_four_real_attribute_tables_have_meaningful_specs(slug, capacity):
    d = parse_lava(slug)
    assert d.spec_completeness == "complete"
    assert d.fields["battery-capacity"] == {"value": capacity, "unit": "mAh"}
    assert d.fields["network-band-gsm"] == {"values": ["GSM 900/1800MHz"]}
    assert d.fields["usb-connection"] == {"values": ["Yes"]}
    if slug == "a1-vibe":
        # Its Expandable Memory section is actually commented out in the capture.
        assert "expandable_memory" not in d.fields and "external-storage" not in d.fields
    else:
        assert d.fields["external-storage"]["values"][0].replace(" ", "") == "32GB"
    assert any(s["label"] == "Type" for s in d.raw["source_specs"])


@pytest.mark.parametrize("slug", ["a1-2025", "a1-josh-21", "a3-torch", "a5-2025",
                                  "gem-power", "hero-shakti-2025", "hero600-pluse"])
def test_previously_working_real_lava_native_fields_remain_unchanged(slug):
    captured = json.loads((LAVA / f"{slug}.json").read_text(encoding="utf-8"))
    # Independent reference to the old literal-cell parser, with its first-label rule.
    expected = {}
    for label, value in re.findall(r"<th>\s*(.*?)\s*</th>\s*<td>\s*(.*?)\s*</td>",
                                   captured["specs_html"], re.S):
        clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
        label, value = clean(label), clean(value)
        if label and value:
            key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
            expected.setdefault(key, {"values": [value]})
    assert expected
    d = parse_lava(slug)
    assert {k: d.fields[k] for k in expected} == expected


@pytest.mark.parametrize("html", ["", "<table></table>",
    "<table><tr><th width='30%'>USB<td>Yes</td></tr></table>",
    "<table><tr><th>USB</th></tr><tr><td>Yes</td></tr></table>",
    "<th>USB</th><td>Yes</td>",
    "<table><tr><th>USB</th><td></td></tr></table>",
    "<table><tr><th>USB</th><td>Yes</td><td>unexpected</td></tr></table>",
    "<!-- <table><tr><th>USB</th><td>obsolete</td></tr></table> -->",
])
def test_malformed_empty_unrelated_or_commented_rows_stay_honest(html):
    assert _extract_spec_rows(html) == []
    assert parse_lava("a1-vibe", html).spec_completeness == "incomplete"


def test_comments_hidden_obsolete_rows_and_collapsed_current_accordion():
    html = """<div style="display:none"><table>
      <!--<!-- <tr><th>USB</th><td>obsolete comment</td></tr> -->
      <tr hidden><th>USB</th><td>obsolete hidden</td></tr>
      <tr aria-hidden="true"><th>USB</th><td>obsolete aria</td></tr>
      <tr style="display: none"><th>USB</th><td>obsolete style</td></tr>
      <tr><th hidden>USB</th><td>obsolete cell</td></tr>
      <tr><th width="30%"> USB <b>Connectivity</b> </th><td width="70%"> Yes<br> current </td></tr>
    </table></div><table hidden><tr><th>USB</th><td>obsolete table</td></tr></table>"""
    assert _extract_spec_rows(html) == [("USB Connectivity", "Yes current")]


@pytest.mark.parametrize("old,new,field", [
    ("1000 mAh Li-ion", "1200 mAh Li-ion", "battery-capacity"),
    ("GSM 900/1800MHz", "GSM 850/1900MHz", "network-band-gsm"),
    ("Yes", "No", "usb-connection"),
])
def test_actual_lava_label_changes_are_meaningful(old, new, field):
    captured = json.loads((LAVA / "a1-vibe.json").read_text(encoding="utf-8"))
    before = parse_lava("a1-vibe")
    after = parse_lava("a1-vibe", captured["specs_html"].replace(f">{old}<", f">{new}<"))
    changes = diff_meaningful_fields(before.fields, after.fields)
    assert field in {c.field for c in changes}


@pytest.mark.parametrize("key,value", [("type", "Li-ion"), ("type", "IPS"),
    ("operating_freq", "unknown"), ("size", "1.8 inches"), ("camera", "Yes")])
def test_ambiguous_lava_labels_are_not_guessed(key, value):
    assert _meaningful_alias(key, value) is None


@pytest.mark.parametrize("raw,unit,expected", [
    ("35900", 2, 359.0), (35900, 2, 359.0), (35900.0, 2, 359.0),
    ("359", 0, 359.0), ("359000", 3, 359.0), ("0", 2, 0.0),
    (None, 2, None), ("", 2, None), ("not a price", 2, None), ("35-90", 2, None),
    ({"min_amount": "100", "max_amount": "200"}, 2, None),
    (True, 2, None), (float("inf"), 2, None), ("NaN", 2, None), (-100, 2, None),
    ("100", None, None), ("100", -1, None), ("100", "2", None),
])
def test_sunbeam_minor_units_and_invalid_prices(raw, unit, expected):
    assert _parse_price({"price": raw, "currency_minor_unit": unit}) == expected


def sunbeam_capture():
    return json.loads((FIXTURES / "wave2" / "sunbeam-recon-2026-09-14.json").read_text(encoding="utf-8"))


def test_real_sunbeam_service_rejected_and_all_29_handsets_retained():
    captured = sunbeam_capture()
    collector = SunbeamCollector(StaticFetcher(json.dumps(captured["products"])))
    discoveries = collector.collect()
    # Frozen accepted handset identities from the pre-repair capture (excluding the proven service).
    actual = {d.model_number for d in discoveries}
    # The exact full set is stored as capture-derived evidence below; keep families distinct.
    expected = set(captured["accepted_handset_skus"])
    assert actual == expected and len(actual) == 29
    assert "F1PSS-1" not in actual
    service = next(c for c in collector.classification_log if c["slug"] == "F1PSS-1")
    assert service["classification"] == "rejected"
    assert all(d.price is not None and d.currency == "USD" for d in discoveries)
    assert next(d for d in discoveries if d.model_number == "PINE-1").price == 359.0
    assert all(i["prices"]["price_range"] is None for i in captured["products"])


def test_service_category_overlap_does_not_reject_unrelated_handset():
    classification, _ = classify_product("F1 Pro Oak", [SERVICE_CATEGORY, "F1 Pro Phones and Accessories"],
                                          sku="OAK-1", permalink="https://sunbeamwireless.com/product/f1-pro-oak/")
    assert classification == "feature_phone"


def test_price_extraction_does_not_invent_editorial_diff_policy():
    captured = sunbeam_capture()["products"]
    before = SunbeamCollector(StaticFetcher(json.dumps(captured))).collect()
    changed = json.loads(json.dumps(captured))
    for item in changed:
        if item["sku"] == "PINE-1":
            item["prices"]["price"] = "39900"
    after = SunbeamCollector(StaticFetcher(json.dumps(changed))).collect()
    old = next(d for d in before if d.model_number == "PINE-1")
    new = next(d for d in after if d.model_number == "PINE-1")
    assert old.price == 359.0 and new.price == 399.0
    assert old.content_hash() != new.content_hash()
    assert diff_meaningful_fields(old.fields, new.fields) == []
