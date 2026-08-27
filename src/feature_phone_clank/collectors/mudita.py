"""Mudita collector (Wave 2, EXPERIMENTAL ONLY, source_key "mudita-com").

Not in `config/scope.yaml` - only `run_experimental()` may execute this
collector, and always against an experimental store.

Mudita is a Gatsby/Contentful first-party site. Verified live 2026-08-27:

- the products listing page-data document
  ``https://mudita.com/page-data/products/page-data.json`` server-renders
  the full catalogue as Contentful nodes carrying stable
  ``ContentfulListItem`` entries: title ("Mudita Kompakt", "Mudita Pure",
  "Mudita Harmony 2"), availability label, and a stable first-party link
  path (/products/phones/mudita-kompakt/).
- product identity therefore uses Mudita's own canonical slug; no SKU/EAN
  is exposed in any public surface (documented limitation - never guessed).

Scope classification is deterministic on the link path, which is Mudita's
own taxonomy:
- /products/phones/<slug>  -> feature-phone candidate (minimalist phones)
- /products/alarm-clocks/* -> accessory/adjacent hardware (rejected)
- /products/watches/*      -> rejected (watches out of scope)
- /products/software-apps/*-> rejected (software)
The known phone models are phones by the manufacturer's own placement;
anything else with an unrecognised /products/<other>/ prefix is
quarantined as ambiguous.

Catastrophic-shrink semantics (§18): the live catalogue currently lists
TWO phones. The floor is deliberately 1: a single healthy minimalist
device is a valid catalogue state for this brand. Zero accepted items,
however, is ALWAYS a failure (parser drift, geoblock or page-data schema
change) and raises rather than fabricating a silent empty baseline.
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

log = logging.getLogger("feature_phone_clank.collectors.mudita")

BASE = "https://mudita.com"
PAGE_DATA_URL = f"{BASE}/page-data/products/page-data.json"

_PHONE_PATH_RE = re.compile(r"^/products/phones/([a-z0-9\-]+)/?$")
_ACCESSORY_PREFIXES = ("/products/alarm-clocks/", "/products/watches/",
                       "/products/software-apps/")
_LIST_ITEM_RE = re.compile(r'"title"\s*:\s*"((?:Mudita )[^"]{2,40})"')
_LINK_RE = re.compile(r'"link"\s*:\s*"(/products/[a-z\-]+/[a-z0-9\-]+/?)"')


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


def classify_link(path: str) -> tuple[str | None, dict]:
    """Deterministic scope decision from Mudita's own link taxonomy.
    Returns (classification, evidence) where classification None means
    'rejected outright' (accessory/adjacent, not interesting enough to log
    individually)."""
    if _PHONE_PATH_RE.match(path):
        return "feature_phone", {"evidence": "manufacturer places it under /products/phones/"}
    if any(path.startswith(p) for p in _ACCESSORY_PREFIXES):
        return None, {"reason": f"{path.split('/')[2]} category - adjacent hardware/software"}
    return "ambiguous", {"reason": "unrecognised /products/ subcategory"}


def parse_page_data(payload_text: str) -> list[tuple[str, str]]:
    """Extract (title, link) pairs for ContentfulListItem nodes that carry a
    products link. Returns raw candidate pairs; classification happens in
    collect(). Malformed JSON raises; absent links return []."""
    data = json.loads(payload_text)
    blob = json.dumps(data.get("result", {}).get("data", {}))

    titles = re.findall(r'"title"\s*:\s*"((?:Mudita )[^"]{2,40})"', blob)
    links = re.findall(r'"link"\s*:\s*"(/products/[a-z\-]+/[a-z0-9\-]+/?)"', blob)

    # Contentful serialises each list item as {"title": ..., "link": ...}
    # with NO keys in between - pair strictly on that adjacency. A loose
    # window would pair titles with the wrong links (e.g. "Mudita Oasis"
    # watch copy against a phones link).
    pairs: list[tuple[str, str]] = []
    node_re = re.compile(
        r'"title"\s*:\s*"((?:Mudita )[^"]{2,40})"\s*,\s*'
        r'"link"\s*:\s*"(/products/[a-z\-]+/[a-z0-9\-]+/?)"')
    pairs = node_re.findall(blob)
    deduped = []
    seen: set[str] = set()
    for title, link in pairs:
        if link in seen:
            continue
        seen.add(link)
        deduped.append((title, link))
    return deduped


class MuditaCollector(BaseCollector):
    source_key = "mudita-com"
    source_type = "catalogue"
    manufacturer = "Mudita"
    region = "int_en"
    base_url = BASE

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, slug_or_title: str, url: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug_or_title, "url": url,
            "classification": classification, "evidence": evidence,
        })

    def collect(self) -> list[Discovery]:
        resp = self.fetcher.get(PAGE_DATA_URL)
        if resp.status != 200:
            raise RuntimeError(f"page-data fetch failed: HTTP {resp.status} for {PAGE_DATA_URL}")

        pairs = parse_page_data(resp.text)
        discoveries: list[Discovery] = []
        seen_slugs: set[str] = set()

        for title, link in pairs:
            classification, evidence = classify_link(link)
            if classification is None:
                continue  # adjacent hardware/software: not logged per-item
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            url = BASE + link

            if classification != "feature_phone":
                self._log(slug, url, classification, evidence)
                continue

            if slug in seen_slugs:
                self._log(slug, url, "duplicate", {"title": title})
                continue
            seen_slugs.add(slug)

            availability = None
            label_m = re.search(r'"label"\s*:\s*"([^"]{1,24})"', resp.text[
                max(0, resp.text.find(f'"/{link.strip(chr(47))}"') - 600):
                (resp.text.find(f'/{link.strip("/")}"') + 300) if resp.text.find(f'/{link.strip("/")}"') > 0 else 0])
            if label_m:
                raw_label = label_m.group(1)
                norm = {"AVAILABLE": "InStock", "Available": "InStock",
                        "Sold Out": "OutOfStock", "Now in Outlet": "Outlet"}.get(raw_label)
                availability = norm or raw_label

            discoveries.append(Discovery(
                source_key=self.source_key,
                product_key=f"{self.source_key}:{slug}",
                manufacturer=self.manufacturer,
                model=title,
                model_number=None,  # documented: no public SKU surface
                region=self.region,
                url=url,
                availability=availability,
                fields={},
                spec_completeness="incomplete",
                raw={
                    "contentful_link": link,
                    "availability_label_raw": label_m.group(1) if label_m else None,
                    **evidence,
                },
            ))

        if not discoveries:
            raise RuntimeError(
                "Mudita completeness guard: zero accepted phone candidates "
                "- parser drift, schema change or geoblock (this source may "
                "legitimately be tiny, but never empty)")

        log.info("mudita-com: %d accepted (%d classified other)",
                 len(discoveries), len(self.classification_log))
        return discoveries
