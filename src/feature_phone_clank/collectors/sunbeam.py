"""Sunbeam Wireless collector (Wave 2, EXPERIMENTAL ONLY, source_key
"sunbeam-f1-us").

Not in `config/scope.yaml` - only `run_experimental()` may execute this
collector; it must never reach the production database.

Sunbeam Wireless runs WooCommerce and exposes the documented public Store
API (wp-json/wc/store/v1/products), verified live 2026-08-27: HTTP 200,
51 products with `sku`, `name`, `permalink`, `categories`, `type`, and
availability. This is tier 1 of the brief's ordering (documented public
API) - no browser automation, no scraping beyond the documented endpoint.

Identity rule (brief §16): manufacturer SKU is primary (e.g. "PINE-1",
"BLJ-1", "EMRLD"); permalink slug kept as secondary evidence. Sunbeam has
many closely-related variants by design - each SKU stays its own identity;
nothing is collapsed on shared marketing families (F1/F1 Pro/Horizon).

Scope classification is deterministic on first-party evidence:
- ONLY products in the "F1 Horizon Phones and Accessories" or "F1 Pro
  Phones and Accessories" categories are phone candidates.
- Items whose name matches the accessory/peripheral vocabulary
  (holder/mount/cable/adapter/charger/dock/battery/case/protector/sd card/
  screen) are rejected as accessories.
- Service items ("Return Claim", "Device Claim", "Data Recovery",
  "Premium") are rejected as services.
- The "Original F1 Accessories" category is legacy-accessory: phones there
  are marked Discontinued by the store itself ("F1 Orchid -
  Discontinued"); any product living ONLY in that category is quarantined
  as ambiguous (discontinued-era hardware, out of current-catalogue scope).
- Anything else unrecognised is quarantined for human classification.

Catastrophic-shrink semantics (§18): live catalogue is ~51 items across the
two phone categories with FLOOR=6 accepted phones (each F1 family carries
8+ colour/config variants). A parse failure returning zero, a template
drift renaming the two categories, or geoblock all produce far fewer than
6 and raise instead of recording a false collapse.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.sunbeam")

STORE_API_URL = (
    "https://sunbeamwireless.com/wp-json/wc/store/v1/products?per_page=100&page=1"
)
PHONE_CATEGORY_NAMES = {
    "F1 Horizon Phones and Accessories",
    "F1 Pro Phones and Accessories",
}
LEGACY_ACCESSORY_CATEGORY = "Original F1 Accessories"
SERVICE_CATEGORY = "Sunbeam Wireless Services"

ACCESSORY_NAME_RE = re.compile(
    r"\b(holder|mount|cable|adapter|charger|dock|battery|case\b|cover|"
    r"protector|sd card|holster)\b", re.IGNORECASE)
SERVICE_NAME_RE = re.compile(r"\b(claim|recovery service|premium subscription|insurance)\b", re.IGNORECASE)
DISCONTINUED_RE = re.compile(r"\bdiscontinued\b", re.IGNORECASE)


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


def _clean_name(raw: str) -> str:
    import html as _html

    return _html.unescape(_html.unescape(str(raw))).strip()


FLOOR_ACCEPTED_PHONES = 6


def classify_product(name: str, category_names: list[str]) -> tuple[str, dict]:
    """Deterministic scope decision. 'feature_phone' | 'rejected' |
    'ambiguous'."""
    evidence = {"categories": category_names}
    if SERVICE_CATEGORY in category_names and len(category_names) == 1:
        return "ambiguous", {**evidence, "reason": "service-only product"}

    # Accessory vocabulary beats everything else in the name.
    if ACCESSORY_NAME_RE.search(name):
        return "rejected", {**evidence, "reason": "accessory/peripheral vocabulary in name"}
    if SERVICE_NAME_RE.search(name):
        return "rejected", {**evidence, "reason": "service item"}

    if not any(c in PHONE_CATEGORY_NAMES for c in category_names):
        return "ambiguous", {**evidence, "reason": "outside both phone categories"}

    # Legacy-only hardware: discontinued Original-F1 devices.
    if LEGACY_ACCESSORY_CATEGORY in category_names and \
            not ({"F1 Horizon Phones and Accessories", "F1 Pro Phones and Accessories"} & set(category_names)):
        if DISCONTINUED_RE.search(name):
            return "ambiguous", {**evidence, "reason": "store-marked discontinued legacy device"}
        return "ambiguous", {**evidence, "reason": "legacy-original-F1-only placement"}

    return "feature_phone", {**evidence, "evidence": "listed in Sunbeam's own F1 phone categories"}


class SunbeamCollector(BaseCollector):
    source_key = "sunbeam-f1-us"
    source_type = "catalogue"
    manufacturer = "Sunbeam"
    region = "us_en"
    base_url = "https://sunbeamwireless.com"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, sku_or_name: str, url: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": sku_or_name, "url": url,
            "classification": classification, "evidence": evidence,
        })

    def collect(self) -> list[Discovery]:
        resp = self.fetcher.get(STORE_API_URL)
        if resp.status != 200:
            raise RuntimeError(f"Store API fetch failed: HTTP {resp.status} for {STORE_API_URL}")
        try:
            products = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Store API returned unparsable JSON: {exc}") from exc
        if not isinstance(products, list):
            raise RuntimeError("Store API payload is not a list")

        discoveries: list[Discovery] = []
        seen_skus: set[str] = set()

        for item in products:
            if not isinstance(item, dict):
                continue
            name = _clean_name(str(item.get("name") or ""))
            sku = str(item.get("sku") or "")
            permalink = str(item.get("permalink") or "")
            cats = [str(c.get("name")) for c in item.get("categories", []) if isinstance(c, dict)]
            pid = item.get("id")

            key_for_log = sku or name[:40]

            classification, evidence = classify_product(name, cats)
            if classification != "feature_phone":
                self._log(key_for_log, permalink or STORE_API_URL, classification, evidence)
                continue

            identity = sku or f"noid-{pid}"
            if identity in seen_skus:
                self._log(identity, permalink, "duplicate", {"name": name})
                continue
            seen_skus.add(identity)

            availability = None
            if isinstance(item.get("is_purchasable"), bool):
                availability = "InStock" if item["is_purchasable"] else "OutOfStock"

            p = item.get("prices") or {}
            prices = []
            raw_price = p.get("price")
            if isinstance(raw_price, (int, float)):
                prices.append(float(raw_price))
            price = (min(prices) / 100.0) if prices else None  # Woo minor units

            currency = None
            pc = p.get("currency_code") or p.get("currency_symbol")
            if isinstance(pc, str):
                currency = pc

            fields: dict = {}
            if prices:
                fields["price_minor_unit_raw"] = [raw_price] if raw_price else []

            discoveries.append(Discovery(
                source_key=self.source_key,
                product_key=f"{self.source_key}:{identity}",
                manufacturer=self.manufacturer,
                model=name,
                model_number=sku or None,
                region=self.region,
                url=permalink or f"{self.base_url}/product/{identity}",
                price=price,
                currency=currency,
                availability=availability,
                fields=fields,
                spec_completeness="incomplete",
                raw={
                    "woo_id": pid,
                    "sku": sku,
                    "categories": cats,
                    "type": item.get("type"),
                    "classification_evidence": evidence,
                    "store_payload_url": STORE_API_URL,
                },
            ))

        if len(discoveries) < FLOOR_ACCEPTED_PHONES:
            raise RuntimeError(
                f"Sunbeam completeness guard: only {len(discoveries)} accepted feature phones "
                f"(floor={FLOOR_ACCEPTED_PHONES}) - API payload drift or partial load")

        log.info("sunbeam-f1-us: %d accepted (%d classified other)",
                 len(discoveries), len(self.classification_log))
        return discoveries
