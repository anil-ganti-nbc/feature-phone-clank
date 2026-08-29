"""HMD/Nokia collector (Stage 2, source_key "hmd-nokia").

Discovery is automatic, not allowlist-gated: every crawl re-derives its
candidate set from HMD's own official catalogue (the `/feature-phones` and
`/smartphones` listing pages) plus the official product sitemap. A new
Nokia/HMD feature phone HMD adds to its live catalogue is found on the next
run without anyone touching this repository. See `docs` in the class
docstring below for the full mechanism.

No field is invented. Every extracted spec traces back to a `PimSpec` /
`PimNumericSpec` object HMD's own page embeds as structured data (a
Contentful-backed content model, not scraped prose) — see
`_extract_spec_fields()`. Unknown stays `None`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.hmd")

BASE = "https://www.hmd.com/en_int"
FEATURE_LISTING_URL = f"{BASE}/feature-phones"
SMARTPHONE_LISTING_URL = f"{BASE}/smartphones"
SITEMAP_URL = f"{BASE}/sitemap-dtc.xml"

# Known HMD site navigation/legal links that legitimately appear on *every*
# catalogue listing page (header/footer chrome), not because they're a
# product listed in both the feature-phone and smartphone grids. This is a
# small, explicit, documented exclusion of known non-product URLs — NOT an
# allowlist of phones. Any nokia-*/hmd-* slug NOT in this set that appears
# on both listings is treated as a genuine classification conflict
# (quarantined), never silently dropped. Derived empirically from HMD's own
# site structure on 2026-08-09; review if HMD restructures its nav.
KNOWN_NAV_SLUGS = frozenset({
    "about", "accessibility", "accessories", "better-phone-project", "blog",
    "choose-your-language", "compliance", "device-recycling", "ethics",
    "feature-phones", "for-business", "nokia-smartphones-and-tablets",
    "phones-for-kids", "press", "press-contact", "privacy",
    "security-updates", "self-repair", "smartphones", "support",
    "sustainability", "tablets", "terms",
})

# Sitemap entries matching these are marketing bundles/offers/legal/test
# pages, not product pages — excluded before they ever become an "ambiguous"
# quarantine candidate. This is the "small explicit exclusion mechanism" for
# non-product noise (brief section: "retain a small explicit
# exclusion/override mechanism for ambiguous cases"), not a phone allowlist.
_NON_PRODUCT_SLUG_RE = re.compile(
    r"(bundle|-offer-\d|teaser|discount|-terms$|-terms-|promo|christmas|"
    r"-test$|gtm-test|coverage|partner|screen-replacement|employee|-news$|"
    r"softlock|-form$)"
)
_PHONE_SLUG_RE = re.compile(r"^(nokia|hmd)-[a-z0-9-]+$")

_HREF_RE = re.compile(r'href="/en_int/([a-z0-9-]+)"')
_ANCHOR_TAG_RE = re.compile(r"<a\s[^>]*>")
_ANCHOR_HREF_RE = re.compile(r'href="/en_int/([a-z0-9-]+)"')
_ANCHOR_ARIA_LABEL_RE = re.compile(r'aria-label="Learn more about ([^"]+)"')
_SITEMAP_LOC_RE = re.compile(r"<loc>https://www\.hmd\.com/en_int/([a-z0-9-]+)</loc>")
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>")
_H1_RE = re.compile(r"<h1[^>]*>([^<]*)</h1>")
_SPECS_TITLE_SUFFIX_RE = re.compile(r"\s+specifications\s*$", re.IGNORECASE)
_PIMSPEC_RE = re.compile(
    r'"__typename":"PimSpec","key":"([a-z0-9-]+)","normalisedValue":'
    r'("(?:[^"\\]|\\.)*"|null|\d+(?:\.\d+)?)(?:,"category":"([a-z0-9&-]+)")?'
)
_PIMNUMERIC_RE = re.compile(
    r'"([a-zA-Z]+)":\{"__typename":"PimNumericSpec"(?:,"category":"[a-z0-9&-]+")?'
    r'(?:,"key":"[a-z0-9-]+")?,"value":([0-9.]+),"unit":"([^"]*)"'
)
_SKU_RE = re.compile(r'"sku":"([A-Za-z0-9]+)"')


class FetchResult(BaseModel):
    url: str
    status: int
    text: str = ""
    # Classification of a network-level failure ("timeout",
    # "connection_error", "network_error"); empty for any real HTTP
    # response (including 4xx/5xx — those are honest server answers, not
    # transport failures). Purely additive evidence: callers keep deciding
    # on `status` alone.
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
# answer of one of these is still returned verbatim (never converted to 0),
# because callers treat the HTTP status as evidence.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# (connect, read) ceiling. Generous read headroom for HMD's intermittent
# mid-transfer stalls (urllib3's read timeout is an inter-byte limit, not a
# total deadline) — still hard, and paired with bounded retries so a dead
# connection costs at most max_attempts x timeout, never a hung run.
DEFAULT_HTTP_TIMEOUT: tuple[float, float] = (10.0, 30.0)


@dataclass
class HttpFetcher:
    """Real network fetcher. Tests never touch the live network with it
    (user constraint 7): the collector suite stands in a fake Fetcher, and
    `tests/test_http_fetcher.py` exercises THIS class against a mocked
    `requests.get` transport only.

    Fetch policy (2026-08-29 repair of the natural-run ReadTimeout
    failures — 24 of 27 staging runs failed with a single-attempt fetcher):

    - `timeout` is (connect, read) seconds. urllib3's read timeout is an
      inter-byte stall limit, not a total deadline: www.hmd.com answers
      healthy requests in well under 1s (measured 2026-08-29 from the
      staging host) but intermittently stalls responses outright, so the
      read ceiling is generous — and still hard.
    - Bounded retries with exponential backoff on transport failures
      (timeouts, connection errors) and transient server statuses.
      HMD's edge fails intermittently (successes and failures alternate
      run to run), so one fresh attempt usually succeeds.
    - The response BODY is downloaded inside the same try: `requests.get`
      returns as soon as headers arrive, and `.text` streams the body —
      a mid-transfer stall raises ReadTimeout THERE. Before this repair
      `.text` was evaluated outside the except block, so one stalled page
      escaped as an exception, propagated through collect(), and failed
      the entire run (DB run_errors show the raw ReadTimeout repr, ~2m45s
      into runs — discovery and dozens of successful product fetches lost).
    - Exhausted transport retries -> FetchResult(status=0, error=<class>).
      Every caller in this module treats FetchResult.status as the single
      source of truth for "did this fetch work" (non-200 already means
      "fall back / skip", never "abort the whole crawl" -- see
      _parse_product's specs->base-page fallback and _sitemap_orphans'
      warn-and-skip). status=0 is not a real HTTP status, so it always
      fails the `== 200` checks and routes into the same fallback paths a
      404/500 would.
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
                text = resp.text  # inside the try: body download is where a
                # mid-transfer stall raises ReadTimeout (the 2026-08-29
                # root cause — see class docstring)
            except requests.exceptions.RequestException as exc:
                last_error = _classify_network_error(requests, exc)
                last_status = 0
                log.warning(
                    "hmd-nokia: %s fetching %s (attempt %d/%d): %r",
                    last_error, url, attempt, self.max_attempts, exc,
                )
                if attempt < self.max_attempts:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_attempts:
                last_status = resp.status_code
                log.warning(
                    "hmd-nokia: transient HTTP %d fetching %s (attempt %d/%d); retrying",
                    resp.status_code, url, attempt, self.max_attempts,
                )
                time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            return FetchResult(url=url, status=resp.status_code, text=text)
        return FetchResult(url=url, status=last_status, text="", error=last_error)


class HmdOverrides(BaseModel):
    """Small explicit human-curated override, applied AFTER automatic
    classification — for the rare case the deterministic rules get a
    specific, already-reviewed URL wrong. Never the primary discovery path;
    see the collector's module docstring."""

    force_feature_phone: list[str] = Field(default_factory=list)
    force_smartphone: list[str] = Field(default_factory=list)
    force_exclude: list[str] = Field(default_factory=list)  # not a phone at all


def load_overrides(path: str | Path) -> HmdOverrides:
    path = Path(path)
    if not path.exists():
        return HmdOverrides()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return HmdOverrides.model_validate(data)


def _extract_links(html: str) -> set[str]:
    return set(_HREF_RE.findall(html))


def _extract_links_with_names(html: str) -> dict[str, str]:
    """Slug -> clean catalogue-card product name, read from each listing
    anchor's own `aria-label="Learn more about <Name>"` attribute. This is
    tier 1 of the name-resolution chain (see `_resolve_name`): it comes
    from the catalogue listing page itself, not a per-product page, so it
    is unaffected by the per-product `/specs` page bugs documented below
    (e.g. the Nokia 5310/230 H1 collision)."""
    names: dict[str, str] = {}
    for tag in _ANCHOR_TAG_RE.findall(html):
        href_m = _ANCHOR_HREF_RE.search(tag)
        if not href_m:
            continue
        slug = href_m.group(1)
        aria_m = _ANCHOR_ARIA_LABEL_RE.search(tag)
        if aria_m and slug not in names:
            names[slug] = aria_m.group(1).strip()
    return names


def _extract_sitemap_slugs(xml: str) -> set[str]:
    return set(_SITEMAP_LOC_RE.findall(xml))


def _strip_specs_title_suffix(title: str) -> str | None:
    """'Nokia 3210 specifications' -> 'Nokia 3210'. The `/specs` page's
    `<title>` follows this pattern consistently and, unlike that same
    page's `<h1>`, was never observed to carry another product's content —
    tier 2 of the name-resolution chain."""
    cleaned = _SPECS_TITLE_SUFFIX_RE.sub("", title).strip()
    return cleaned or None


def _strip_marketing_tagline(text: str) -> str | None:
    """'HMD 130 Music | Your music on your terms' -> 'HMD 130 Music'.
    Deterministic split on the first ' | ' or ' - ' separator — no fuzzy
    matching. Tier 3 of the name-resolution chain (last resort before the
    URL slug): this is the same field known to carry another product's
    content on at least one page (Nokia 5310/230), so it is only used when
    nothing cleaner is available."""
    for sep in (" | ", " - "):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head:
                return head
    return text.strip() or None


def _humanize_slug(slug: str) -> str:
    """Final fallback: 'hmd-130-music' -> 'Hmd 130 Music'. Never used for a
    product that has any other name evidence at all."""
    return " ".join(part.upper() if part.lower() in {"hmd"} else part.capitalize()
                     for part in slug.split("-"))


# Exact, literal titles observed to be CMS placeholder/shell content (e.g.
# hmd-terra-m's `/specs` page — a nav-menu-only link with almost nothing
# server-rendered — has `<title>HMD</title>` and no `<h1>` at all). This is
# a deterministic denylist of specific known-bad strings, not fuzzy
# matching: an exact, case-insensitive membership test.
_DEGENERATE_TITLE_VALUES = frozenset({"hmd", "nokia", ""})


def _usable(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _strip_specs_title_suffix(text)
    cleaned = _strip_marketing_tagline(cleaned) if cleaned else None
    if cleaned and cleaned.lower() not in _DEGENERATE_TITLE_VALUES:
        return cleaned
    return None


def _resolve_name(
    slug: str, catalogue_name: str | None,
    specs_title: str | None, base_title: str | None = None,
    specs_h1: str | None = None, base_h1: str | None = None,
) -> tuple[str, str]:
    """Deterministic name-resolution precedence (user constraint: canonical
    display name must never be raw marketing-tagline H1, another product's
    stale H1, or a CMS placeholder shell title):

    1. catalogue-card name (listing page `aria-label`) — cleanest,
       structured, proven immune to the per-product page bugs below.
    2. `/specs` page `<title>`, tagline-suffix stripped — distinct field
       from H1, never observed carrying another product's content.
    3. product base page `<title>`, tagline stripped — used for candidates
       with no catalogue-card evidence at all (e.g. nav-menu-only links
       like hmd-terra-m) or a degenerate `/specs` title.
    4. `/specs` page `<h1>`, marketing-tagline stripped — the known-buggy
       source (Nokia 5310/230 collision), used only if nothing above is
       available.
    5. base page `<h1>`, marketing-tagline stripped.
    6. URL slug, humanized — absolute last resort.

    Returns (name, source_tier) so the tier is recorded as provenance.
    """
    if catalogue_name:
        return catalogue_name, "catalogue_card"
    for text, tier in (
        (specs_title, "specs_title"), (base_title, "base_title"),
        (specs_h1, "specs_h1_fallback"), (base_h1, "base_h1_fallback"),
    ):
        cleaned = _usable(text)
        if cleaned:
            return cleaned, tier
    return _humanize_slug(slug), "slug_fallback"


def _title_of(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    return m.group(1).strip() if m else None


def _h1_of(html: str) -> str | None:
    m = _H1_RE.search(html)
    return m.group(1).strip() if m else None


def _title_signal(title: str | None) -> str | None:
    if not title:
        return None
    low = title.lower()
    has_fp = "feature phone" in low
    has_sp = "smartphone" in low
    if has_fp and not has_sp:
        return "feature_phone"
    if has_sp and not has_fp:
        return "smartphone"
    return None


def _extract_spec_fields(html: str) -> dict[str, dict]:
    """Every `PimSpec`/`PimNumericSpec` object on the page — HMD's own
    structured content model (Contentful-backed), not prose scraping. Field
    presence varies per product; absent fields are simply not in the dict
    (never guessed)."""
    fields: dict[str, dict] = {}
    for key, raw_value, category in _PIMSPEC_RE.findall(html):
        value = raw_value.strip('"')
        if value == "null":
            continue
        entry = fields.setdefault(key, {"values": [], "category": category or None})
        if value not in entry["values"]:
            entry["values"].append(value)
    for camel_key, value, unit in _PIMNUMERIC_RE.findall(html):
        # camelCase JSON field name -> snake-case, consistent with PimSpec keys
        snake = re.sub(r"(?<!^)(?=[A-Z])", "-", camel_key).lower()
        if snake in fields:
            continue  # PimSpec already has this key with richer evidence
        fields[snake] = {"value": float(value) if "." in value else int(value), "unit": unit}
    return fields


def _extract_skus(html: str) -> list[str]:
    seen: list[str] = []
    for sku in _SKU_RE.findall(html):
        if sku not in seen:
            seen.append(sku)
    return seen


class HmdCollector(BaseCollector):
    source_key = "hmd-nokia"
    source_type = "catalogue"
    manufacturer = "HMD"
    region = "en_int"
    base_url = BASE

    def __init__(
        self, fetcher: Fetcher | None = None,
        overrides_path: str | Path = "config/hmd_overrides.yaml",
        include_sitemap_orphans: bool = True,
    ) -> None:
        super().__init__()
        self.fetcher = fetcher or HttpFetcher()
        self.overrides = load_overrides(overrides_path)
        self.include_sitemap_orphans = include_sitemap_orphans

    # -- classification ---------------------------------------------------

    def _final_classification(self, slug: str, auto: str) -> tuple[str, str | None]:
        """Apply human overrides on top of the automatic decision. Returns
        (classification, override_reason)."""
        if slug in self.overrides.force_exclude:
            return "ambiguous", "manual_override:force_exclude"
        if slug in self.overrides.force_feature_phone:
            return "feature_phone", "manual_override:force_feature_phone"
        if slug in self.overrides.force_smartphone:
            return "smartphone", "manual_override:force_smartphone"
        return auto, None

    def _log(self, slug: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug, "url": f"{BASE}/{slug}",
            "classification": classification, "evidence": evidence,
        })

    def _discover(self) -> tuple[set[str], set[str], set[str], dict[str, str]]:
        """Returns (feature_phone_slugs, smartphone_slugs, conflicted_slugs,
        catalogue_names) from the two official category listing pages — the
        live, allowlist-free discovery mechanism. `catalogue_names` covers
        every slug seen on either listing (tier 1 of name resolution)."""
        fp_html = self.fetcher.get(FEATURE_LISTING_URL)
        sp_html = self.fetcher.get(SMARTPHONE_LISTING_URL)
        if fp_html.status != 200:
            raise RuntimeError(f"feature-phones listing fetch failed: HTTP {fp_html.status}")
        if sp_html.status != 200:
            raise RuntimeError(f"smartphones listing fetch failed: HTTP {sp_html.status}")

        fp_links = _extract_links(fp_html.text)
        sp_links = _extract_links(sp_html.text)
        overlap = fp_links & sp_links
        chrome = {s for s in overlap if s in KNOWN_NAV_SLUGS or not _PHONE_SLUG_RE.match(s)}
        conflicted = overlap - chrome

        fp_only = fp_links - overlap - chrome
        sp_only = sp_links - overlap - chrome
        fp_only = {s for s in fp_only if _PHONE_SLUG_RE.match(s)}
        sp_only = {s for s in sp_only if _PHONE_SLUG_RE.match(s)}

        catalogue_names = _extract_links_with_names(sp_html.text)
        catalogue_names.update(_extract_links_with_names(fp_html.text))  # fp wins on conflict
        return fp_only, sp_only, conflicted, catalogue_names

    def _sitemap_orphans(self, already_seen: set[str]) -> set[str]:
        sm = self.fetcher.get(SITEMAP_URL)
        if sm.status != 200:
            log.warning("hmd-nokia: sitemap fetch failed (HTTP %s); skipping orphan sweep", sm.status)
            return set()
        slugs = _extract_sitemap_slugs(sm.text)
        candidates = set()
        for slug in slugs:
            if slug in already_seen or slug in KNOWN_NAV_SLUGS:
                continue
            if not _PHONE_SLUG_RE.match(slug):
                continue
            if _NON_PRODUCT_SLUG_RE.search(slug):
                continue
            candidates.add(slug)
        return candidates

    # -- parsing ------------------------------------------------------------

    def _parse_product(self, slug: str, catalogue_name: str | None = None) -> Discovery:
        """Always returns a Discovery for a confidently-classified candidate
        (brief: product existence/classification is separate from spec
        extraction success — a missing `/specs` page must not make an
        officially-listed feature phone disappear from the catalogue).

        Tries the `/specs` page first (richest source: structured fields +
        SKU + title). If that's unavailable, falls back to the base product
        page for whatever SKU/H1 evidence it has, and finally to
        `catalogue_name`/slug alone with empty fields — marking
        `spec_completeness='incomplete'` either way so the gap itself stays
        observable (and a later appearance of real specs is a detectable
        change, not silently absorbed).
        """
        specs_url = f"{BASE}/{slug}/specs"
        base_page_url = f"{BASE}/{slug}"
        specs_resp = self.fetcher.get(specs_url)

        specs_title = specs_h1 = base_title = base_h1 = None
        fields: dict = {}
        skus: list[str] = []
        fetch_note = None
        source_pages = [specs_url]

        if specs_resp.status == 200:
            specs_title = _title_of(specs_resp.text)
            specs_h1 = _h1_of(specs_resp.text)
            fields = _extract_spec_fields(specs_resp.text)
            skus = _extract_skus(specs_resp.text)
        else:
            fetch_note = f"specs page fetch failed: HTTP {specs_resp.status}"
            log.warning("hmd-nokia: %s for %s; falling back to base page", fetch_note, slug)

        # The base page is fetched whenever it might add evidence the specs
        # page didn't: no catalogue-card name (tier 1 unresolved — e.g. a
        # nav-menu-only link like hmd-terra-m), no SKU yet, or the specs
        # page failed outright. Skipped when tier 1 + SKU are already in
        # hand, to avoid a second request for the common case (43 of 44
        # baseline products never need it).
        if not catalogue_name or not skus or specs_resp.status != 200:
            base_resp = self.fetcher.get(base_page_url)
            source_pages.append(base_page_url)
            if base_resp.status == 200:
                base_title = _title_of(base_resp.text)
                base_h1 = _h1_of(base_resp.text)
                if not skus:
                    skus = _extract_skus(base_resp.text)
            else:
                note = f"base page fetch also failed: HTTP {base_resp.status}"
                fetch_note = f"{fetch_note}; {note}" if fetch_note else note

        fields_are_present = bool(fields)
        completeness = "complete" if fields_are_present else "incomplete"

        name, name_source = _resolve_name(
            slug, catalogue_name, specs_title, base_title, specs_h1, base_h1,
        )
        model_number = skus[0] if skus else None

        return Discovery(
            source_key=self.source_key,
            product_key=f"{self.source_key}:{slug}",
            manufacturer=self.manufacturer,
            model=name,
            model_number=model_number,
            region=self.region,
            url=f"{BASE}/{slug}",
            fields=fields,
            spec_completeness=completeness,
            raw={
                "specs_url": specs_url, "source_pages": source_pages, "skus": skus,
                "name_source": name_source,
                "raw_specs_h1": specs_h1, "raw_specs_title": specs_title,
                "raw_base_h1": base_h1, "raw_base_title": base_title,
                "catalogue_card_name": catalogue_name, "fetch_note": fetch_note,
            },
        )

    # -- BaseCollector contract ---------------------------------------------

    def collect(self) -> list[Discovery]:
        fp_slugs, sp_slugs, conflicted, catalogue_names = self._discover()
        discoveries: list[Discovery] = []

        for slug in sorted(fp_slugs):
            classification, override_reason = self._final_classification(slug, "feature_phone")
            evidence = {"listing_membership": "feature_phones", "override": override_reason}
            self._log(slug, classification, evidence)
            if classification == "feature_phone":
                discoveries.append(self._parse_product(slug, catalogue_names.get(slug)))

        for slug in sorted(sp_slugs):
            classification, override_reason = self._final_classification(slug, "smartphone")
            evidence = {"listing_membership": "smartphones", "override": override_reason}
            self._log(slug, classification, evidence)
            if classification == "feature_phone":  # only reachable via override
                discoveries.append(self._parse_product(slug, catalogue_names.get(slug)))

        for slug in sorted(conflicted):
            resp = self.fetcher.get(f"{BASE}/{slug}")
            title = _title_of(resp.text) if resp.status == 200 else None
            signal = _title_signal(title)
            classification, override_reason = self._final_classification(slug, "ambiguous")
            evidence = {
                "listing_membership": "both",
                "reason": "product URL appears on both feature-phones and smartphones "
                          "category listings — contradictory primary signal",
                "title": title, "title_signal": signal, "override": override_reason,
            }
            self._log(slug, classification, evidence)
            if classification == "feature_phone":
                discoveries.append(self._parse_product(slug, catalogue_names.get(slug)))

        if self.include_sitemap_orphans:
            seen = fp_slugs | sp_slugs | conflicted
            orphans = self._sitemap_orphans(seen)
            for slug in sorted(orphans):
                resp = self.fetcher.get(f"{BASE}/{slug}")
                title = _title_of(resp.text) if resp.status == 200 else None
                classification, override_reason = self._final_classification(slug, "ambiguous")
                evidence = {
                    "listing_membership": "none",
                    "reason": "present in the official sitemap but not linked from either "
                              "current category listing — likely legacy/discontinued, a "
                              "promotional/test artifact, or (rarely) an unlisted pre-launch "
                              "page; needs human review either way",
                    "http_status": resp.status, "title": title, "override": override_reason,
                }
                self._log(slug, classification, evidence)
                if classification == "feature_phone":  # only reachable via override
                    discoveries.append(self._parse_product(slug, catalogue_names.get(slug)))

        return discoveries
