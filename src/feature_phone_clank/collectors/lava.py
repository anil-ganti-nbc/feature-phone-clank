"""Lava collector (Stage E2, EXPERIMENTAL ONLY, source_key "lava-india").

Not in `config/scope.yaml` — `run_experimental()` is the only way to run
this collector; it must never reach the production database. See
docs/FEATURE_PHONE_SCOPE_EXPANSION.md "Lava research" for the full
investigation trail.

Unlike itel (client-rendered SPA, no discoverable data source — see
`collectors/itel.py`), lavamobiles.com is a Next.js site that server-renders
(or statically generates) every page with a `__NEXT_DATA__` script tag
carrying the full page's props as JSON — no headless browser needed at all.
This is genuinely tier 2 of the brief's required ordering ("embedded
structured data"), the best case, verified directly:

- `/featurephones?subCat=all` -> `props.pageProps.smartphoneData.all_products`
  (yes, that key name, even for the feature-phone listing — an artifact of
  Lava's own frontend code, not a mistake here) — each entry already carries
  `parent_id: "featurephones"` or `"smartphones"`, an explicit, official,
  first-party category field. This is stronger classification evidence than
  itel provides (itel's only signal is listing-page membership).
- `/featurephones/<slug>` -> `props.pageProps.slugData.product_deatil`
  (typo — "deatil" — preserved verbatim from Lava's own API/CMS field name,
  not a bug here) carries `view_details_specs`: a clean HTML `<table>` of
  `<th>label</th><td>value</td>` rows, easy to regex without a full HTML
  parser dependency.

Known data-quality caveat (documented, not silently trusted): `launch_date`
on several currently-live "2025"-named products (e.g. "A1 2025") reads
`null` or a stale 2024 date that clearly predates the product's own name.
Per brief section 20 ("do not trust publication dates blindly if the source
rewrites them"), this field is retained as raw evidence only — it is never
used as the freshness signal that decides whether something is "new".
`new_launches` (a first-party yes/no flag) and identity_anomaly detection
via the existing diff pipeline are the reliable freshness signals here.

The `sitemap.xml` at the site root is third-party-generated (xml-sitemaps.com),
last regenerated 2024-12-17, and does not list individual feature-phone
product URLs at all (only the `/featurephones?subCat=all` category page) —
confirmed stale, not used for discovery. The category listing's own embedded
`all_products` array is the actual discovery mechanism.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.lava")

BASE = "https://lavamobiles.com"
FEATURE_LISTING_URL = f"{BASE}/featurephones?subCat=all"
SMARTPHONE_LISTING_URL = f"{BASE}/smartphones?subCat=all"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_SPEC_ROW_RE = re.compile(
    r"<th>\s*(.*?)\s*</th>\s*<td>\s*(.*?)\s*</td>", re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


class FetchResult(BaseModel):
    url: str
    status: int
    text: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


@dataclass
class HttpFetcher:
    """Real network fetcher. Never used by tests (user constraint 7)."""

    user_agent: str = "Mozilla/5.0 (compatible; FeaturePhoneClank/0.1; +https://github.com/)"
    timeout: float = 15.0
    delay_s: float = 0.3

    def get(self, url: str) -> FetchResult:
        import requests

        time.sleep(self.delay_s)
        resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        return FetchResult(url=url, status=resp.status_code, text=resp.text)


def _extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("lava-india: __NEXT_DATA__ present but not valid JSON")
        return None


def _clean_html_text(fragment: str) -> str:
    """Strip tags and collapse whitespace — spec table cell values are
    plain text but may carry stray inline markup (e.g. `<br>`)."""
    text = _TAG_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", text).strip()


def _extract_spec_rows(specs_html: str) -> list[tuple[str, str]]:
    rows = []
    for label, value in _SPEC_ROW_RE.findall(specs_html):
        label = _clean_html_text(label)
        value = _clean_html_text(value)
        if label and value:
            rows.append((label, value))
    return rows


class LavaCollector(BaseCollector):
    source_key = "lava-india"
    source_type = "catalogue"
    manufacturer = "Lava"
    region = "india"
    base_url = BASE

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, slug: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug, "url": f"{BASE}/featurephones/{slug}",
            "classification": classification, "evidence": evidence,
        })

    def _fetch_products(self, listing_url: str) -> list[dict]:
        resp = self.fetcher.get(listing_url)
        if resp.status != 200:
            raise RuntimeError(f"listing fetch failed: HTTP {resp.status} for {listing_url}")
        data = _extract_next_data(resp.text)
        if data is None:
            raise RuntimeError(f"no __NEXT_DATA__ found at {listing_url}")
        try:
            return data["props"]["pageProps"]["smartphoneData"]["all_products"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"unexpected __NEXT_DATA__ shape at {listing_url}: {exc}") from exc

    def _discover(self) -> tuple[dict[str, dict], dict[str, dict], set[str]]:
        """Returns (feature_phone_products, smartphone_products,
        conflicted_slugs) keyed by slug — `parent_id` is Lava's own,
        explicit, first-party category field (stronger evidence than
        listing-membership alone), but a slug whose `parent_id` disagrees
        with which listing it was fetched from is still a contradictory
        signal and is quarantined, never guessed."""
        fp_raw = self._fetch_products(FEATURE_LISTING_URL)
        sp_raw = self._fetch_products(SMARTPHONE_LISTING_URL)

        fp_by_slug = {p["slug"]: p for p in fp_raw if p.get("parent_id") == "featurephones"}
        sp_by_slug = {p["slug"]: p for p in sp_raw if p.get("parent_id") == "smartphones"}

        # products whose own parent_id disagrees with the listing they were
        # served from — Lava's data has been observed messy elsewhere
        # (typo'd field names); never silently trust listing membership
        # over the product's own declared category.
        fp_mislabeled = {p["slug"] for p in fp_raw if p.get("parent_id") not in (None, "featurephones")}
        sp_mislabeled = {p["slug"] for p in sp_raw if p.get("parent_id") not in (None, "smartphones")}

        conflicted = (set(fp_by_slug) & set(sp_by_slug)) | fp_mislabeled | sp_mislabeled
        for slug in conflicted:
            fp_by_slug.pop(slug, None)
            sp_by_slug.pop(slug, None)
        return fp_by_slug, sp_by_slug, conflicted

    def _parse_product(self, slug: str, product: dict) -> Discovery:
        product_url = f"{BASE}/featurephones/{slug}"
        resp = self.fetcher.get(product_url)
        fields: dict = {}
        fetch_note = None
        model_number = None

        if resp.status != 200:
            fetch_note = f"product page fetch failed: HTTP {resp.status}"
            log.warning("lava-india: %s for %s", fetch_note, slug)
        else:
            data = _extract_next_data(resp.text)
            if data is None:
                fetch_note = "product page had no __NEXT_DATA__"
            else:
                try:
                    detail = data["props"]["pageProps"]["slugData"]["product_deatil"]
                except (KeyError, TypeError):
                    detail = None
                specs_html = detail.get("view_details_specs") if detail else None
                if specs_html:
                    for label, value in _extract_spec_rows(specs_html):
                        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                        if key in fields:
                            continue  # first tab section wins on a duplicate label
                        fields[key] = {"values": [value]}
                        if "model_number" in key or key == "model":
                            model_number = model_number or value
                else:
                    fetch_note = "product page loaded but had no view_details_specs block"

        completeness = "complete" if fields else "incomplete"
        price = product.get("price")
        return Discovery(
            source_key=self.source_key,
            product_key=f"{self.source_key}:{slug}",
            manufacturer=self.manufacturer,
            model=product.get("name") or slug,
            model_number=model_number,
            region=self.region,
            url=product_url,
            price=float(price) if isinstance(price, (int, float)) else None,
            currency="INR" if isinstance(price, (int, float)) else None,
            fields=fields,
            spec_completeness=completeness,
            raw={
                "catalogue_id": product.get("id"),
                "catalogue_category_id": product.get("category_id"),
                "new_launches_flag": product.get("new_launches"),
                # retained as evidence only — known unreliable, see module
                # docstring's data-quality caveat; never used as a freshness
                # signal by the diff/event pipeline.
                "raw_launch_date": product.get("launch_date"),
                "cut_price": product.get("cut_price"),
                "fetch_note": fetch_note,
            },
        )

    def collect(self) -> list[Discovery]:
        fp_products, sp_products, conflicted = self._discover()
        discoveries: list[Discovery] = []

        for slug, product in sorted(fp_products.items()):
            self._log(slug, "feature_phone", {
                "listing_membership": "featurephones", "parent_id": product.get("parent_id"),
            })
            discoveries.append(self._parse_product(slug, product))

        for slug in sorted(conflicted):
            self._log(slug, "ambiguous", {
                "reason": "product's parent_id disagreed with its listing, or the "
                          "slug appeared on both category listings — contradictory "
                          "primary signal",
            })

        for slug, product in sorted(sp_products.items()):
            self._log(slug, "smartphone", {
                "listing_membership": "smartphones", "parent_id": product.get("parent_id"),
            })

        return discoveries
