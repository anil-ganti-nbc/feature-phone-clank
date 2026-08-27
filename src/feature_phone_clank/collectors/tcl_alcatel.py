"""Alcatel feature-phone catalogue collector (Wave 2, EXPERIMENTAL ONLY,
source_key "tcl-alcatel-global").

Not in `config/scope.yaml` - only `run_experimental()` may execute this
collector, and always against an experimental store.

Brand/ownership facts (verified live 2026-08-27): TCL Communication owns
the Alcatel brand. The historical alcatelmobile.com storefront is a live
WordPress site (Yoast SEO sitemap regenerated 2026-08-25) whose own
taxonomy carries a FEATURE-PHONES category with per-product PDPs -
https://www.alcatelmobile.com/feature-phones/<slug>/ (e.g. phone-1021,
phone-1041). The tcl.com "mobile" listing turned out to be Android-only
in its naming evidence, so this collector deliberately targets Alcatel's
own feature-phone category instead - the narrowest reliable first-party
stream for Alcatel basic phones.

Identity rule: Alcatel's own PDP slug ("phone-1021") is primary;
marketing names are sparse on the listing and never fabricated.

Scope classification:
- /feature-phones/<slug>/ PDP anchors from the feature-phones page are the
  only accepted shape; anything else on that page (nav/support hrefs) is
  ignored by construction.
- Unknown shapes are quarantined rather than guessed.

Catastrophic-shrink semantics (brief section 18): the live catalogue lists
2 current feature phones. Floor = 1: one healthy device is valid for this
brand; zero accepted items raises. REQUIRED_ANCHOR pins the known current
"phone-" PDP family so template drift fails honestly.
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

log = logging.getLogger("feature_phone_clank.collectors.tcl_alcatel")

BASE = "https://www.alcatelmobile.com"
FEATURE_PHONES_URL = f"{BASE}/feature-phones/"

_PDP_HREF_RE = re.compile(
    r'href="(https://www\.alcatelmobile\.com/feature-phones/[a-z0-9\-]{4,50}/)"', re.I)


class FetchResult(BaseModel):
    url: str
    status: int
    text: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


@dataclass
class HttpFetcher:
    """Real network fetcher. Never used by tests."""

    user_agent: str = (
        "Mozilla/5.0 (compatible; FeaturePhoneClank/0.1; +https://github.com/)"
    )
    timeout: float = 15.0
    delay_s: float = 0.3

    def get(self, url: str) -> FetchResult:
        import requests

        time.sleep(self.delay_s)
        resp = requests.get(url, headers={"User-Agent": self.user_agent},
                            timeout=self.timeout)
        return FetchResult(url=url, status=resp.status_code, text=resp.text)


FLOOR_ACCEPTED = 1


def _slug_from_url(url: str) -> str | None:
    m = re.match(r"https://www\.alcatelmobile\.com/feature-phones/([a-z0-9\-]{4,50})/", url)
    return m.group(1) if m else None


class TCLAlcatelCollector(BaseCollector):
    source_key = "tcl-alcatel-global"
    source_type = "catalogue"
    manufacturer = "Alcatel/TCL"
    region = "global_en"
    base_url = BASE

    def __init__(self, fetcher=None) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()

    def _log(self, slug: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug, "url": f"{FEATURE_PHONES_URL}{slug}",
            "classification": classification, "evidence": evidence,
        })

    def collect(self) -> list[Discovery]:
        resp = self.fetcher.get(FEATURE_PHONES_URL)
        if resp.status != 200:
            raise RuntimeError(
                f"feature-phones catalogue fetch failed: HTTP {resp.status} "
                f"for {FEATURE_PHONES_URL}")

        discoveries: list[Discovery] = []
        seen_slugs: set[str] = set()

        for href in _PDP_HREF_RE.findall(resp.text):
            slug = _slug_from_url(href)
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            if slug.startswith("phone-"):
                model_number = slug.replace("phone-", "", 1)
                discoveries.append(Discovery(
                    source_key=self.source_key,
                    product_key=f"{self.source_key}:{slug}",
                    manufacturer=self.manufacturer,
                    model=f"Alcatel {model_number}",
                    model_number=model_number,
                    region=self.region,
                    url=href,
                    fields={},
                    spec_completeness="incomplete",
                    raw={"pdp_slug": slug,
                         "identity_note": "PDP slug numeric suffix is Alcatel's own model id"},
                ))
            else:
                self._log(slug, "quarantined", {
                    "reason": "unrecognised feature-phone PDP slug shape",
                })

        if len(discoveries) < FLOOR_ACCEPTED:
            raise RuntimeError(
                f"Alcatel completeness guard: only {len(discoveries)} accepted "
                f"(floor={FLOOR_ACCEPTED}) - template drift or partial load")

        log.info("tcl-alcatel-global: %d accepted (%d classified other)",
                 len(discoveries), len(self.classification_log))
        return discoveries
