"""Punkt collector (Stage F, EXPERIMENTAL ONLY, source_key "punkt-ch").

Not in `config/scope.yaml` — `run_experimental()` is the only way to run
this collector; it must never reach the production database.

punkt.ch is Punkt.'s own first-party storefront (Shopify) and server-
renders schema.org JSON-LD on every product page — genuinely tier 2 of
the brief's required ordering ("embedded structured data"), verified live
2026-08-25:

- `/sitemap.xml` -> sitemap index listing `sitemap_products_1.xml?from=..&to=..`
  (the query parameters drift as the catalogue changes, so the index is
  always fetched first and the products sitemap URL is discovered from it,
  never hard-coded).
- product pages embed one `application/ld+json` block with
  `"@type": "ProductGroup"` carrying `name`, `productGroupID`,
  `hasVariant[]` (each with `sku`, `gtin`, `name`, and an `offers` object:
  `price`, `priceCurrency`, schema.org `availability`). No headless
  browser, no anti-bot interference observed.

Classification is deterministic on Punkt's own slug families:
`mpNN-*` are minimalist phones (feature phones: MP02), `mcNN-*` are
secure smartphones (MC02/MC03 — outside this Clank's beat, logged not
discovered), `ac/uc/esNN` are known accessory families, anything else is
merch/accessory (excluded). A NEW two-letter-plus-digits slug family that
this table does not recognise is quarantined as ambiguous rather than
silently dropped or guessed — the same philosophy as the HMD orphan sweep.

Multi-variant pricing note: variants can differ in price/availability.
The Discovery carries a deterministic aggregate (lowest price; InStock if
any variant is in stock) plus the full variant evidence verbatim in
`raw`. Availability itself is a suppressed event type by policy
(core/notifications.py SUPPRESS_BY_DEFAULT), so this aggregation never
drives production notifications during experimental soak.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol
from xml.sax.saxutils import unescape as _xml_unescape

from pydantic import BaseModel

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.punkt")

BASE = "https://www.punkt.ch"
SITEMAP_INDEX_URL = f"{BASE}/sitemap.xml"

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")
_PRODUCT_URL_PATH = "/products/"
_JSONLD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
# Punkt's own product-slug shape: family prefix + model number + descriptor.
_FAMILY_PREFIX_RE = re.compile(r"^([a-z]{2})(\d{2})-")
FEATURE_PHONE_FAMILIES = {"mp"}
SMARTPHONE_FAMILIES = {"mc"}
ACCESSORY_FAMILIES = {"ac", "uc", "es"}  # alarm clock, USB charger, extension socket


class FetchResult(BaseModel):
    url: str
    status: int
    text: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


@dataclass
class HttpFetcher:
    """Real network fetcher. Never used by tests."""

    user_agent: str = "Mozilla/5.0 (compatible; FeaturePhoneClank/0.1; +https://github.com/)"
    timeout: float = 15.0
    delay_s: float = 0.3

    def get(self, url: str) -> FetchResult:
        import requests

        time.sleep(self.delay_s)
        resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        return FetchResult(url=url, status=resp.status_code, text=resp.text)


def classify_slug(slug: str) -> str:
    """Deterministic classification from Punkt's own family prefixes."""
    m = _FAMILY_PREFIX_RE.match(slug)
    if m:
        family = m.group(1)
        if family in FEATURE_PHONE_FAMILIES:
            return "feature_phone"
        if family in SMARTPHONE_FAMILIES:
            return "smartphone"
        if family in ACCESSORY_FAMILIES:
            return "accessory"
        # A brand-new family we have never seen: ambiguous until a human
        # rules on it — never silently dropped, never guessed.
        return "ambiguous"
    return "accessory"


def _extract_product_group(html: str) -> dict | None:
    """Return the ProductGroup JSON-LD object from a product page, or None
    when absent/unparsable (logged by the caller as incomplete evidence)."""
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "ProductGroup":
            return data
    return None


def _slug_from_url(url: str) -> str | None:
    marker = _PRODUCT_URL_PATH
    idx = url.find(marker)
    if idx < 0:
        return None
    tail = url[idx + len(marker):].strip("/")
    return tail.split("?")[0] or None


class PunktCollector(BaseCollector):
    source_key = "punkt-ch"
    source_type = "catalogue"
    manufacturer = "Punkt"
    region = "ch_en"
    base_url = BASE

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, slug: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug, "url": f"{BASE}/products/{slug}",
            "classification": classification, "evidence": evidence,
        })

    def _fetch_products_sitemap_url(self) -> str:
        resp = self.fetcher.get(SITEMAP_INDEX_URL)
        if resp.status != 200:
            raise RuntimeError(f"sitemap index fetch failed: HTTP {resp.status} for {SITEMAP_INDEX_URL}")
        for loc in _SITEMAP_LOC_RE.findall(resp.text):
            if "sitemap_products_" in loc:
                # <loc> values carry XML entities (&amp;); unescape before use.
                return _xml_unescape(loc)
        raise RuntimeError(f"sitemap index at {SITEMAP_INDEX_URL} lists no products sitemap")

    def _discover_slugs(self) -> list[str]:
        sitemap_url = self._fetch_products_sitemap_url()
        resp = self.fetcher.get(sitemap_url)
        if resp.status != 200:
            raise RuntimeError(f"products sitemap fetch failed: HTTP {resp.status} for {sitemap_url}")
        slugs: set[str] = set()
        for loc in _SITEMAP_LOC_RE.findall(resp.text):
            slug = _slug_from_url(_xml_unescape(loc))
            if slug:
                slugs.add(slug)
        return sorted(slugs)

    def _parse_product(self, slug: str) -> Discovery:
        product_url = f"{BASE}/products/{slug}"
        resp = self.fetcher.get(product_url)
        fields: dict = {}
        fetch_note = None
        group: dict | None = None

        if resp.status != 200:
            fetch_note = f"product page fetch failed: HTTP {resp.status}"
            log.warning("punkt-ch: %s for %s", fetch_note, slug)
        else:
            group = _extract_product_group(resp.text)
            if group is None:
                fetch_note = "product page had no parsable ProductGroup JSON-LD"

        name = None
        model_number = None
        price = None
        currency = None
        availability = None
        variants: list[dict] = []

        if group is not None:
            name = group.get("name")
            description = group.get("description")
            if isinstance(description, str) and description.strip():
                fields["description"] = {"values": [description.strip()]}
            raw_variants = group.get("hasVariant") or []
            if isinstance(raw_variants, dict):
                raw_variants = [raw_variants]
            prices: list[float] = []
            currencies: set[str] = set()
            in_stock = False
            for variant in raw_variants:
                if not isinstance(variant, dict):
                    continue
                offer = variant.get("offers") or {}
                sku = variant.get("sku")
                gtin = variant.get("gtin")
                variant_name = variant.get("name")
                entry: dict = {}
                if sku:
                    entry["sku"] = sku
                if gtin:
                    entry["gtin"] = gtin
                if variant_name:
                    entry["variant_name"] = variant_name
                v_price = offer.get("price") if isinstance(offer, dict) else None
                if isinstance(v_price, (int, float, str)):
                    try:
                        prices.append(float(v_price))
                        entry["price"] = float(v_price)
                    except ValueError:
                        pass
                v_currency = offer.get("priceCurrency") if isinstance(offer, dict) else None
                if v_currency:
                    currencies.add(str(v_currency))
                    entry["currency"] = str(v_currency)
                v_availability = offer.get("availability") if isinstance(offer, dict) else None
                if isinstance(v_availability, str):
                    entry["availability"] = v_availability.rsplit("/", 1)[-1]
                    if entry["availability"] == "InStock":
                        in_stock = True
                if entry:
                    variants.append(entry)
            model_number = next(
                (v.get("sku") for v in sorted(variants, key=lambda v: v.get("sku", "")) if v.get("sku")),
                None,
            )
            if prices:
                price = min(prices)  # deterministic aggregate; full evidence in raw
                currency = sorted(currencies)[0] if currencies else None
            availability = "InStock" if in_stock else (
                "OutOfStock" if variants else None
            )
            if not variants:
                fetch_note = fetch_note or "ProductGroup carried no hasVariant entries"

        completeness = "complete" if fields else "incomplete"
        return Discovery(
            source_key=self.source_key,
            product_key=f"{self.source_key}:{slug}",
            manufacturer=self.manufacturer,
            model=name or slug,
            model_number=model_number,
            region=self.region,
            url=product_url,
            price=price,
            currency=currency,
            availability=availability,
            fields=fields,
            spec_completeness=completeness,
            raw={
                "product_group_id": group.get("productGroupID") if group else None,
                "variants": variants,
                "fetch_note": fetch_note,
            },
        )

    def collect(self) -> list[Discovery]:
        slugs = self._discover_slugs()
        discoveries: list[Discovery] = []

        for slug in slugs:
            classification = classify_slug(slug)
            if classification == "feature_phone":
                self._log(slug, "feature_phone", {
                    "evidence": "Punkt's own slug family prefix (mp*) - minimalist phone line",
                })
                discoveries.append(self._parse_product(slug))
            elif classification == "smartphone":
                self._log(slug, "smartphone", {
                    "evidence": "Punkt's own slug family prefix (mc*) - secure smartphone line",
                })
            elif classification == "ambiguous":
                self._log(slug, "ambiguous", {
                    "reason": "unrecognised slug family - needs human classification",
                })
            else:
                self._log(slug, "accessory", {
                    "reason": "not a phone slug (merch/accessory/peripheral)",
                })

        return discoveries
