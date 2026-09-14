"""Lava collector (source_key "lava-india").

Production-authorised by `config/scope.yaml`; experimental runs remain
available against an isolated store. See
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
  not a bug here) carries `view_details_specs`: HTML tables whose cells
  may carry attributes. Comments and explicitly hidden rows are excluded
  using the standard-library parser before matching complete table rows.

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
from html.parser import HTMLParser
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
    r"\s*<th\b[^>]*>\s*((?:(?!</?(?:th|td|tr|table)\b).)*?)\s*</th>"
    r"\s*<td\b[^>]*>\s*((?:(?!</?(?:th|td|tr|table)\b).)*?)\s*</td>\s*",
    re.DOTALL | re.IGNORECASE,
)
_SPEC_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
_TR_RE = re.compile(r"<tr\b[^>]*>((?:(?!<tr\b).)*?)</tr>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class FetchResult(BaseModel):
    url: str
    status: int
    text: str = ""
    # Coarse transport-failure classification ("timeout", "connection_error",
    # "network_error"); empty for any real HTTP response. Callers keep
    # deciding on `status` alone (same contract as the 915f908 hmd repair).
    error: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


def _classify_network_error(requests_mod, exc: Exception) -> str:
    """Coarse transport-failure classification for logs/FetchResult.error."""
    if isinstance(exc, requests_mod.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests_mod.exceptions.ConnectionError):
        return "connection_error"
    return "network_error"


# Server answers that are transient by nature and worth a retry; a final
# answer of one of these is still returned verbatim (never rewritten to 0),
# because callers treat the HTTP status as evidence.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# (connect, read) ceiling — mirrors the proven 915f908 hmd fetcher repair.
# urllib3's read timeout is an inter-byte stall limit, not a total deadline;
# generous-but-hard, paired with bounded retries so a dead connection costs
# at most max_attempts x timeout, never a hung run.
DEFAULT_HTTP_TIMEOUT: tuple[float, float] = (10.0, 30.0)


@dataclass
class HttpFetcher:
    """Real network fetcher — 2026-08-30 transport repair, porting the
    proven 915f908 hmd fetcher pattern (bounded retries, body read inside
    the protected path, hard (10, 30)s connect/read ceiling, transport
    classification). Lava's own pre-repair fetcher was single-attempt with
    the body read outside any error handling: 3 of ~50 natural soak runs
    failed whole-run with ReadTimeout on lavamobiles.com. Tests never
    touch the live network with it (user constraint 7); mocked-transport
    regressions live in tests/test_lava_fetcher.py.

    Fetch policy (identical to hmd post-915f908):

    - bounded retries with exponential backoff (2s, 4s) on transport
      failures and transient server statuses; lavamobiles.com fails
      intermittently run-to-run, so one fresh attempt usually succeeds;
    - the response BODY is downloaded inside the same try (requests.get
      returns on headers; `.text` streams the body — a mid-transfer stall
      raises ReadTimeout there);
    - exhausted transport retries -> FetchResult(status=0, error=<class>);
      `status == 200` call sites route status=0 into the same skip/fallback
      paths a 404/500 already took, so a dead page never aborts the crawl.
    """

    user_agent: str = "Mozilla/5.0 (compatible; FeaturePhoneClank/0.1; +https://github.com/)"
    timeout: tuple[float, float] = DEFAULT_HTTP_TIMEOUT
    delay_s: float = 0.3  # politeness delay before every real request
    max_attempts: int = 3
    backoff_s: float = 2.0  # doubled per retry: 2s, 4s, ...

    def get(self, url: str) -> FetchResult:
        import requests

        last_error = ""
        last_status = 0
        for attempt in range(1, self.max_attempts + 1):
            time.sleep(self.delay_s)
            try:
                resp = requests.get(
                    url, headers={"User-Agent": self.user_agent}, timeout=self.timeout,
                )
                text = resp.text  # inside the try: the body download is where
                # a mid-transfer stall raises ReadTimeout (the pre-repair
                # whole-run failure mode)
            except requests.exceptions.RequestException as exc:
                last_error = _classify_network_error(requests, exc)
                last_status = 0
                log.warning(
                    "lava-india: %s fetching %s (attempt %d/%d): %r",
                    last_error, url, attempt, self.max_attempts, exc,
                )
                if attempt < self.max_attempts:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_attempts:
                last_status = resp.status_code
                log.warning(
                    "lava-india: transient HTTP %d fetching %s (attempt %d/%d); retrying",
                    resp.status_code, url, attempt, self.max_attempts,
                )
                time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            return FetchResult(url=url, status=resp.status_code, text=text)
        return FetchResult(url=url, status=last_status, text="", error=last_error)


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


class _VisibleSpecs(HTMLParser):
    """Remove comments and explicitly obsolete rows without unfolding accordions.

    Lava's collapsed accordion divs contain current specs. A display:none
    table/row/cell, or an explicit hidden/aria-hidden subtree, does not.
    Text cleaning remains the existing _clean_html_text implementation.
    """
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[tuple[str, bool]] = []
        self.fragments: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1]) or "hidden" in attributes
                  or (attributes.get("aria-hidden") or "").lower() == "true"
                  or (tag in {"table", "tbody", "tr", "th", "td"} and
                      re.search(r"display\s*:\s*none\b", attributes.get("style") or "", re.I)))
        if not hidden:
            self.fragments.append(self.get_starttag_text())
        if tag not in {"br", "img", "hr", "input", "meta", "link", "wbr"}:
            self.stack.append((tag, bool(hidden)))

    def handle_endtag(self, tag):
        hidden = bool(self.stack and self.stack[-1][1])
        if not hidden:
            self.fragments.append(f"</{tag}>")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not (self.stack and self.stack[-1][1]):
            self.fragments.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _extract_spec_rows(specs_html: str) -> list[tuple[str, str]]:
    visible = _VisibleSpecs()
    visible.feed(specs_html)
    rows = []
    for table in _SPEC_TABLE_RE.findall("".join(visible.fragments)):
        for row in _TR_RE.findall(table):
            match = _SPEC_ROW_RE.fullmatch(row)
            if not match:
                continue
            label, value = map(_clean_html_text, match.groups())
            if label and value:
                rows.append((label, value))
    return rows


def _meaningful_alias(key: str, value: str) -> tuple[str, dict] | None:
    """Only equivalents evidenced by current first-party labels and units."""
    if key == "type":
        capacity = re.fullmatch(r"(\d+)\s*mAh\s+Li-ion", value, re.I)
        if capacity:
            return "battery-capacity", {"value": int(capacity[1]), "unit": "mAh"}
    if key in {"operating_freq", "operating_frequency"} and re.fullmatch(
            r"GSM\s+[0-9/\s]+\s*MHz", value, re.I):
        return "network-band-gsm", {"values": [value]}
    aliases = {"usb_connectivity": "usb-connection", "expandable_memory": "external-storage"}
    if key in aliases:
        return aliases[key], {"values": [value]}
    return None


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
        source_specs = []

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
                        source_specs.append({"label": label, "value": value})
                        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                        if key in fields:
                            continue  # first tab section wins on a duplicate label
                        fields[key] = {"values": [value]}
                        alias = _meaningful_alias(key, value)
                        if alias:
                            fields.setdefault(*alias)
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
                "source_specs": source_specs,
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
