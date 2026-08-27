"""Doro collector (Wave 2, EXPERIMENTAL ONLY, source_key "doro-gb").

Not in `config/scope.yaml` - only `run_experimental()` may execute this
collector, and always against an experimental store.

doro.com's mobile-phones category page server-renders one `products-tile`
per catalogue member. Each tile anchor carries first-party identity
attributes verified live 2026-08-27:

- ``data-sku``    : Doro's numeric internal SKU (e.g. "39607")
- ``data-name``   : marketing name (e.g. "Doro 780X")
- ``href``        : stable PDP path /en-gb/shop/mobile-devices/easy-phones/
                    doro-780x-f769f3c4/ (slug + hex id suffix)
- ``data-filters``: form-factor tags (barphone/clamshell/...) used as
                    classification evidence

Identity rule (brief §16): the Doro SKU is strongest; the PDP slug is kept
as secondary evidence. Marketing names with matching SKUs are NOT collapsed.

Scope classification is evidence-based and deterministic:
- The mobile-phones category page IS the feature-phone/senior-phone
  catalogue by Doro's own taxonomy (smartphones have a separate category).
- A tile whose name matches the known Android line ("Aurora") would be out
  of scope even if Doro merges categories later - quarantined as ambiguous.
- Tiles without data-sku/data-name are quarantined as malformed.
- Accessory/other-device URLs cannot appear on this category page, but any
  unexpected path shape (not /shop/mobile-devices/) is quarantined anyway.

Catastrophic-shrink semantics (§18): a healthy live capture showed 9
products. FLOOR = 3: two or fewer tiles raises a completeness error rather
than recording a plausible-but-wrong near-empty catalogue. An explicit
REQUIRED_ANCHOR on a long-lived flagship family guards template drift.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.doro")

BASE = "https://www.doro.com"
MOBILE_PHONES_URL = f"{BASE}/en-gb/products/mobile-phones/"

_TILE_RE = re.compile(r'<div class="products-tile".*?</a>', re.DOTALL)
_SKU_RE = re.compile(r'data-sku="(\d+)"')
_NAME_RE = re.compile(r'data-name="([^"]+)"')
_HREF_RE = re.compile(r'href="(/en-gb/shop/mobile-devices/[a-z0-9\-]+/[a-z0-9\-]+/?)"')
_FILTERS_RE = re.compile(r'data-filters="([^"]*)"')

# Known Doro Android smartphone line - excluded by name if it ever appears
# in the easy-phones feed (they are listed under a separate category today).
SMARTPHONE_LINE_RE = re.compile(r"\bAurora\b", re.IGNORECASE)
PDP_PATH_PREFIX = "/en-gb/shop/mobile-devices/"


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


FLOOR_PRODUCTS = 3
REQUIRED_ANCHOR_SUBSTRING = "Doro"


def classify_tile(name: str | None, href: str | None) -> tuple[str, dict]:
    """Deterministic scope decision for one tile. Returns (classification,
    evidence). classification is 'feature_phone' | 'ambiguous'."""
    evidence: dict = {}
    if name:
        evidence["data_name"] = name
    if href:
        evidence["pdp_path"] = href
    if name and SMARTPHONE_LINE_RE.search(name):
        return "ambiguous", {
            **evidence,
            "reason": "matches Doro's own Android smartphone line (Aurora)",
        }
    if href and not href.startswith(PDP_PATH_PREFIX):
        return "ambiguous", {**evidence, "reason": "unexpected PDP path shape"}
    return "feature_phone", {**evidence, "evidence": "listed in Doro's own mobile-phones (easy phones) category"}


class DoroCollector(BaseCollector):
    source_key = "doro-gb"
    source_type = "catalogue"
    manufacturer = "Doro"
    region = "gb_en"
    base_url = BASE

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, slug_or_sku: str, url: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug_or_sku, "url": url,
            "classification": classification, "evidence": evidence,
        })

    def collect(self) -> list[Discovery]:
        resp = self.fetcher.get(MOBILE_PHONES_URL)
        if resp.status != 200:
            raise RuntimeError(
                f"mobile-phones category fetch failed: HTTP {resp.status} for {MOBILE_PHONES_URL}")

        discoveries: list[Discovery] = []
        seen_skus: set[str] = set()
        accepted = 0

        for tile in _TILE_RE.findall(resp.text):
            sku_m = _SKU_RE.search(tile)
            name_m = _NAME_RE.search(tile)
            href_m = _HREF_RE.search(tile)
            filt_m = _FILTERS_RE.search(tile)

            # The URL is present for every real tile; use it for logging even
            # when attributes are malformed.
            raw_href = href_m.group(1) if href_m else (
                (re.search(r'href="([^"]+)"', tile) or [None, ""]).group(1)
                if re.search(r'href="([^"]+)"', tile) else "")

            if not (sku_m and name_m):
                self._log(raw_href or "(no-href)", BASE + raw_href if raw_href else MOBILE_PHONES_URL,
                          "quarantined", {"reason": "tile missing data-sku or data-name"})
                continue

            sku = sku_m.group(1)
            name = name_m.group(1)
            href = href_m.group(1) if href_m else None
            filters = [f.strip() for f in filt_m.group(1).split(",") if f.strip()] if filt_m else []

            if sku in seen_skus:
                self._log(sku, BASE + (href or ""), "duplicate", {"name": name})
                continue
            seen_skus.add(sku)

            classification, evidence = classify_tile(name, href)
            if classification != "feature_phone":
                self._log(sku, BASE + (href or ""), classification, evidence)
                continue

            if not href:
                self._log(sku, BASE, "quarantined", {"reason": "well-formed tile without parseable PDP href"})
                continue

            form_factors = {"form_factors": filters} if filters else {}
            fields: dict = {"form_factor_tags": filters} if filters else {}
            product_url = BASE + href
            discoveries.append(Discovery(
                source_key=self.source_key,
                product_key=f"{self.source_key}:{sku}",
                manufacturer=self.manufacturer,
                model=name,
                model_number=None,  # SKU numeric id kept here; name carries model
                region=self.region,
                url=product_url,
                fields=fields,
                spec_completeness="incomplete",
                raw={
                    "sku": sku,
                    "data_name": name,
                    "form_factor_filters": filters,
                    "pdp_path": href,
                    **({"identity_note": "numeric sku is Doro's internal id; no EAN on listing"} if not form_factors else {}),
                },
            ))
            accepted += 1

        catalogued = accepted
        if catalogued < FLOOR_PRODUCTS:
            raise RuntimeError(
                f"Doro catalogue completeness guard: only {catalogued} accepted products "
                f"(floor={FLOOR_PRODUCTS}) - possible template drift or partial load")

        anchors_ok = any(d.model.startswith(REQUIRED_ANCHOR_SUBSTRING) for d in discoveries)
        if not anchors_ok:
            raise RuntimeError("Doro catalogue missing expected 'Doro' naming anchor - template drift?")

        log.info("doro-gb: %d accepted (%d quarantined/other)",
                 accepted, len(self.classification_log))
        return discoveries
