"""itel collector (Stage E1, EXPERIMENTAL ONLY, source_key "itel-india").

Not in `config/scope.yaml` — `run_experimental()` is the only way to run
this collector; it must never reach the production database. See
docs/FEATURE_PHONE_SCOPE_EXPANSION.md "itel research" for the full
investigation trail behind the design choices below.

Why this collector looks different from `hmd.py`
--------------------------------------------------
HMD's site is server-rendered: a plain `requests.get()` returns the full
product HTML, so `hmd.py`'s `Fetcher` protocol is a thin URL->text wrapper.

itel-india.com is a client-rendered Vite/React SPA. Verified directly
(2026-08-18): the initial HTML document for both the `/featurephones`
listing and any `/product/<slug>` detail page is an empty `<div id="root">`
splash shell — `requests.get()` on either returns no product data at all.
There is no discoverable JSON/REST API (`requests`-level, network capture,
and JS-bundle inspection all came up empty) and no build manifest exposed
that maps a route to its lazy-loaded chunk filename (those hashes also
change on redeploy — observed twice within one research session). Per the
brief's required ordering (official API > embedded structured data >
sitemap+SSR > deterministic CMS endpoint > headless browser, last resort),
the first four were each concretely tried and concretely failed for this
site; a headless browser is the smallest mechanism that actually works
here, not a default reached for because "the page uses JS".

Given a headless browser is already in the loop, the `ItelFetcher`
protocol below returns already-structured data (a list of product-card
dicts, a list of spec-table (label, value) pairs) via `page.evaluate()`,
rather than raw HTML for this module to regex — HMD's regex-over-HTML
approach doesn't transfer cleanly to itel's deeply-nested, class-obfuscated
Tailwind markup, and Playwright is already doing the rendering, so it can
just as easily do the extraction and hand back clean data.

Known V1 limitation (documented, not hidden): a product detail page's
specification block is a tabbed accordion ("General", "Display Features",
"Battery", "Camera", "Memory & Storage", "Connectivity", "Additional").
Only the "General" tab's rows are present in the DOM on page load; the
other six are populated on click. V1 does not simulate those clicks, so
`fields` only ever carries the General-tab rows (Model, Colors, Display,
Battery, Language Support, Phonebook, SMS, and a few feature flags) and
`spec_completeness` is always "incomplete" for this source — the same
documented, tolerated state as HMD's `hmd-touch-4g`. Clicking through the
remaining six tabs is a scoped fast-follow, not implemented here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from ..core.collector_base import BaseCollector
from ..core.models import Discovery

log = logging.getLogger("feature_phone_clank.collectors.itel")

BASE = "https://www.itel-india.com"
FEATURE_LISTING_URL = f"{BASE}/featurephones"
SMARTPHONE_LISTING_URL = f"{BASE}/smartphones"

_PRODUCT_HREF_RE = re.compile(r"^/product/([a-z0-9-]+)$")
_NEW_BADGE_RE = re.compile(r"^new", re.IGNORECASE)


class ProductCard(dict):
    """Shape: {"href": str, "name": str, "is_new": bool}."""


class ItelFetcher(Protocol):
    def get_cards(self, listing_url: str) -> list[dict]:
        """One entry per product-card anchor found on a rendered category
        listing page, e.g. {"href": "/product/super-guru-4g",
        "name": "Super Guru 4G", "is_new": False}."""

    def get_spec_rows(self, product_url: str) -> list[tuple[str, str]] | None:
        """(label, value) pairs from the rendered #specifications General
        tab, in DOM order. None if the page failed to load at all
        (distinct from an empty list, which means the page loaded but the
        specifications block itself was absent)."""


@dataclass
class ItelPlaywrightFetcher:
    """Real headless-browser fetcher. Never used by tests — see module
    docstring and hmd.py's HttpFetcher for the same no-live-network-in-tests
    pattern. Requires the optional `playwright` extra
    (`pip install feature-phone-clank[itel]`); imported lazily so the base
    install (and every other collector) never pays for it."""

    # NOT `wait_until="networkidle"`: observed live (2026-08-18) to hang
    # past a 20s timeout on at least one real product page
    # (`/product/ace-3-heera`) — itel's pages keep background network
    # chatter going (analytics beacons, GTM, an autoplaying video asset)
    # that never lets the network go fully idle. `domcontentloaded` +an
    # explicit wait for the actual content selector is the reliable
    # signal; "networkidle" is a known-fragile heuristic for exactly this
    # kind of page and was replaced after hitting the failure directly,
    # not preemptively.
    nav_timeout_ms: float = 20_000
    content_timeout_ms: float = 10_000

    def _card_extract_js(self) -> str:
        return """
        () => Array.from(document.querySelectorAll('a[href^="/product/"]'))
            .map(a => ({href: a.getAttribute('href'), text: a.textContent.trim()}))
            .filter(c => c.text.length > 0)
        """

    def _spec_extract_js(self) -> str:
        return """
        () => {
          const root = document.querySelector('#specifications');
          if (!root) return null;
          const captions = Array.from(root.querySelectorAll('span'))
            .filter(s => s.className.includes('caption'));
          return captions.map(c => {
            const value = c.nextElementSibling;
            return [c.textContent.trim(), value ? value.textContent.trim() : ''];
          });
        }
        """

    def get_cards(self, listing_url: str) -> list[dict]:
        """Returns raw {"href", "text"} entries, ONE PER ANCHOR — a product
        card is typically three separate `<a>` tags (image/name/price) all
        pointing at the same href, so slugs repeat with different text
        lengths. Deliberately not merged/cleaned here: `ItelCollector`
        does that normalization so the exact same code path is exercised
        whether the raw entries came from this real fetcher or a fixture
        list in a test (see test_itel_collector.py's FakeItelFetcher,
        which returns this same raw shape)."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(listing_url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                try:
                    page.wait_for_selector('a[href^="/product/"]', timeout=self.content_timeout_ms)
                except Exception:  # noqa: BLE001 — best-effort readiness signal only
                    log.warning("itel-india: no product-card anchor appeared within %sms on %s",
                                self.content_timeout_ms, listing_url)
                raw = page.evaluate(self._card_extract_js())
            finally:
                browser.close()
        return raw

    def get_spec_rows(self, product_url: str) -> list[tuple[str, str]] | None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                try:
                    page.goto(product_url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                except Exception:  # noqa: BLE001 — navigation failure -> caller treats as page-load failure
                    log.warning("itel-india: navigation failed for %s", product_url, exc_info=True)
                    return None
                try:
                    page.wait_for_selector("#specifications", timeout=self.content_timeout_ms)
                except Exception:  # noqa: BLE001 — page loaded but specs block never appeared
                    pass
                rows = page.evaluate(self._spec_extract_js())
            finally:
                browser.close()
        return [tuple(r) for r in rows] if rows is not None else None


def _slug_of(href: str) -> str | None:
    m = _PRODUCT_HREF_RE.match(href)
    return m.group(1) if m else None


class ItelCollector(BaseCollector):
    source_key = "itel-india"
    source_type = "catalogue"
    manufacturer = "itel"
    region = "india"
    base_url = BASE

    def __init__(self, fetcher: ItelFetcher | None = None) -> None:
        super().__init__()
        self.fetcher = fetcher or ItelPlaywrightFetcher()

    def _log(self, slug: str, classification: str, evidence: dict) -> None:
        self.classification_log.append({
            "slug": slug, "url": f"{BASE}/product/{slug}",
            "classification": classification, "evidence": evidence,
        })

    @staticmethod
    def _merge_cards(raw_entries: list[dict]) -> dict[str, dict]:
        """Collapse the 2-3 raw anchor entries a product card produces
        (image/name/price links, all same href) into one {"name", "is_new"}
        per slug — the longest text entry wins (the price- or image-only
        anchors have short/no text), and a leading "new" badge is stripped
        from whichever entry supplied the winning name."""
        cards: dict[str, dict] = {}
        for entry in raw_entries:
            slug = _slug_of(entry["href"])
            if not slug:
                continue
            text = entry["text"]
            is_new = bool(_NEW_BADGE_RE.match(text))
            name = _NEW_BADGE_RE.sub("", text, count=1).strip() if is_new else text
            existing = cards.get(slug)
            if existing is None or len(name) > len(existing["name"]):
                cards[slug] = {"name": name, "is_new": is_new}
        return cards

    def _discover(self) -> tuple[dict[str, dict], dict[str, dict], set[str]]:
        """Returns (feature_phone_cards, smartphone_cards, conflicted_slugs)
        keyed by slug. A slug appearing on both listings is a contradictory
        primary signal — quarantined, never guessed, mirroring hmd.py."""
        fp_by_slug = self._merge_cards(self.fetcher.get_cards(FEATURE_LISTING_URL))
        sp_by_slug = self._merge_cards(self.fetcher.get_cards(SMARTPHONE_LISTING_URL))
        conflicted = set(fp_by_slug) & set(sp_by_slug)
        for slug in conflicted:
            del fp_by_slug[slug]
            del sp_by_slug[slug]
        return fp_by_slug, sp_by_slug, conflicted

    def _parse_product(self, slug: str, card: dict) -> Discovery:
        product_url = f"{BASE}/product/{slug}"
        rows = self.fetcher.get_spec_rows(product_url)
        fields: dict = {}
        fetch_note = None
        model_number = None
        if rows is None:
            fetch_note = "product page failed to load or render"
            log.warning("itel-india: %s for %s", fetch_note, slug)
        else:
            for label, value in rows:
                if not value:
                    continue
                key = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
                fields[key] = {"values": [value]}
            if "model" in fields:
                model_number = fields["model"]["values"][0]
            elif "model_name" in fields:
                model_number = fields["model_name"]["values"][0]

        # A listing-card anchor's text was observed live (2026-08-18) to
        # sometimes concatenate the name with its bullet-point marketing
        # blurb and price with no separator at all (e.g.
        # "Ace 2 Heera1.77\" Big Display | ... |1,109") — `card["name"]` is
        # NOT reliably clean. The spec table's own "Model"/"Model Name" row
        # was clean in every real product observed so far; prefer it, and
        # fall back to the card text only when no spec table was available
        # at all (e.g. a page-load failure).
        clean_name = None
        if "model" in fields:
            clean_name = fields["model"]["values"][0]
        elif "model_name" in fields:
            clean_name = fields["model_name"]["values"][0]

        return Discovery(
            source_key=self.source_key,
            product_key=f"{self.source_key}:{slug}",
            manufacturer=self.manufacturer,
            model=clean_name or card["name"],
            model_number=model_number,
            region=self.region,
            url=product_url,
            fields=fields,
            # General-tab-only capture (see module docstring) — never
            # "complete" in V1, even when every General row is present.
            spec_completeness="incomplete",
            raw={
                "card_name": card["name"], "is_new_badge": card.get("is_new", False),
                "spec_rows_captured": [r[0] for r in rows] if rows else [],
                "fetch_note": fetch_note,
            },
        )

    def collect(self) -> list[Discovery]:
        fp_cards, sp_cards, conflicted = self._discover()
        discoveries: list[Discovery] = []

        for slug, card in sorted(fp_cards.items()):
            self._log(slug, "feature_phone", {
                "listing_membership": "featurephones", "is_new_badge": card.get("is_new", False),
            })
            discoveries.append(self._parse_product(slug, card))

        for slug in sorted(conflicted):
            self._log(slug, "ambiguous", {
                "listing_membership": "both",
                "reason": "product URL card appears on both /featurephones and "
                          "/smartphones listings — contradictory primary signal",
            })

        for slug in sorted(sp_cards):
            self._log(slug, "smartphone", {"listing_membership": "smartphones"})

        return discoveries
