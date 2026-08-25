# Feature Phone Clank — Scope Expansion (Stage E)

Status: **E0 (research) + E1/E2 (itel, Lava experimental collectors, now
with a real live baseline — see section 5a) complete. E3 (specialist
collectors) researched, implementation deferred** — see "Specialist
collectors: implementation status" below. Neither itel nor Lava is
promoted to production. No database has been retired or reset.

## 0. Owner-defined source policy

Core OEM scope for this phase: **HMD/Nokia** (existing production
reference), **itel**, **Lava**.

Jio — intentionally excluded from owner-defined source scope.

## 1. Current truth (re-established 2026-08-18, before any new code)

- GitHub `main` HEAD: `8690c49` ("Enable interactive macOS collection").
- Open PRs at the time of this work: #8 (macOS bundle-ID fix, unrelated),
  #6 ("Completion Phase A: notification outbox + Discord delivery", open at
  the time — later merged to main as `73793fb`; left untouched per owner
  instruction), #4 (Stage A docs, draft, unrelated).
- Hetzner deployed revision: `c749df3`, predates PR #6 — **Discord/
  notifications were not live in production at the time of this work.**
  (Post-loss note, 2026-08-25: the Hetzner volume was destroyed on
  2026-08-23; see the continuity registry — fpc-epoch-2.)
- Production scope (`config/scope.yaml`): `hmd-nokia` only.
- Production DB (queried live via the Hetzner Docker volume): 44 products,
  healthy history back to 2026-08-09.
- **Live finding, not caused by this work:** the last 3 scheduled hmd-nokia
  runs before this session (2026-08-18 01:15 and 07:15 UTC, plus one manual
  run during E0) failed with `ReadTimeout` against `www.hmd.com` (15s
  timeout). The catastrophic-zero guard correctly preserved the prior 44
  products; nothing was corrupted. Not fixed here — HMD is the mature
  reference implementation and out of scope for this expansion unless it
  exposes an architectural defect, which this doesn't. Flagged for owner
  awareness.
- Local test suite (branch closest to main): 91 passed, 1 skipped, before
  any of this work. After E1+E2 (itel + Lava collectors, CLI wiring): **112
  passed, 1 skipped.**
- **Unrelated security finding:** Lava's own `press-and-media` page
  (`lavamobiles.com/press-and-media`) currently serves a CMS content field
  containing an obfuscated injected `<script>` tag pointing at a
  `raw.githack.com` URL — looks like a stored-XSS compromise of Lava's
  website, unrelated to anything in this repository. Flagged to the owner
  directly; not something this project acts on. Confirms the design
  decision to always treat embedded CMS/HTML content as inert text to
  parse, never render.

## 2. itel research

**Official surfaces investigated:** `itel-india.com` (India catalogue —
the only official surface investigated in depth; global/other-region itel
sites were not).

**Feature-phone catalogue:** `itel-india.com/featurephones` (listing) +
`itel-india.com/product/<slug>` (detail, shared URL prefix with
smartphones — the path itself is NOT a category signal, unlike HMD).
Approx. 12-13 feature phones live at investigation time (Super Guru 4G,
Super Guru 4G Max, it2165C, King Signal, Flip One, Ace 2 Heera, Ace 3
Heera, Aqua, City 100, Circle 1, and a few `IT####`-numbered models).

**Smartphone separation:** No shared URL-path signal. The only reliable
signal found is **listing-page membership** (a slug's product card appears
on `/featurephones` vs `/smartphones`) — same approach as `hmd.py`, and the
`ItelCollector._discover()` cross-listing conflict logic mirrors it
directly.

**Sitemap:** exists (`itel-india.com/sitemap.xml`), regenerated
dynamically (lastmod timestamps identical across all entries, changing on
every fetch — i.e. it's generated fresh per-request, not tracking real
per-page change times). Lists `/product/<slug>` URLs but was observed
**missing at least one live product** (`ace-3-heera`, confirmed live on the
rendered `/featurephones` listing and referenced in an official blog post,
absent from the sitemap at the same point in time). Conclusion: the
listing page is the more current discovery source; the sitemap is
supplementary at best, not authoritative for itel.

**API/JSON evidence:** none found. `itel-india.com` is a client-rendered
Vite/React SPA — the initial HTML document for every route (listing and
detail alike) is an empty `<div id="root">` splash shell; product data
only appears after JavaScript execution. No REST/GraphQL endpoint was
found via network capture, JS-bundle string search, or common manifest
paths (`/manifest.json`, `/.vite/manifest.json` — both 200 but SPA-fallback
noise, not real manifests). Per-route data is baked directly into
hash-named JS chunks resolved only at runtime (`FeaturePhoneProductPage-
<hash>.js`, a product-specific chunk like `SuperGuru4G-<hash>.js`, etc.) —
the hashes were observed to change across two fetches within the same
research session (itel redeploys frequently), so there is no stable chunk
filename to hardcode.

**CMS/framework:** Vite + React, custom-built frontend (no headless CMS
signature found; Tailwind-style utility classes throughout).

**Conclusion — headless browser is justified, not a default.** Tried, in
the brief's required order: (1) official API — none; (2) embedded
structured data in the initial HTML — none, empty shell; (3) sitemap +
server-rendered HTML — not server-rendered; (4) deterministic CMS endpoint
— none discoverable. A headless browser (Playwright) is the smallest
mechanism that actually works, confirmed by directly rendering the page
and reading the resulting DOM (clean `<a href="/product/slug">` product
cards on listings; a clean, deterministic `#specifications` label/value
block on detail pages — see `collectors/itel.py` module docstring for the
exact JS extraction used).

**Known V1 gap:** the detail page's full spec table is a tabbed accordion
(General / Display Features / Battery / Camera / Memory & Storage /
Connectivity / Additional); only the "General" tab's rows are present in
the DOM without a click. V1 captures General-tab fields only (Model,
Colors, Display, Battery, language support, Phonebook, SMS, a few feature
flags) and always reports `spec_completeness="incomplete"`. Clicking
through the other six tabs is a scoped, not-yet-built enhancement.

**Identity evidence:** no persistent SKU/model-number field found separate
from the marketing model name; V1 uses the "Model" spec-table row (when
present) as `model_number`, which in practice usually just repeats the
display name. A genuine SKU-level identifier was not found for itel.

## 3. itel implementation (Stage E1)

- **Status:** experimental, not production-promoted.
- **Collector:** `src/feature_phone_clank/collectors/itel.py`,
  `source_key="itel-india"`, registered in `collectors/__init__.py` but
  **absent from `config/scope.yaml`** — the existing production-scope lock
  (`core/scope.py`, `run_production_collector` vs `run_experimental` in
  `core/runner.py`) is reused as-is for isolation; no new mechanism was
  built.
- **Dependency:** `playwright` added as an optional extra
  (`pip install feature-phone-clank[itel]`) in `pyproject.toml` — not a
  base dependency, so hmd-nokia and the test suite never pay for it.
- **Fixtures/tests:** `tests/fixtures/itel/cards.py` (structured fixture
  data — see rationale in that file's docstring: itel's fetcher returns
  already-structured card/spec data via `page.evaluate()`, not raw HTML, so
  there's no HTML file to fixture), `tests/test_itel_collector.py` — 10
  tests covering a valid feature phone, the live-caught noisy-card-text
  bug (regression test), "new" badge handling, smartphone rejection
  (+ logged), cross-listing conflict/quarantine, a product-page load
  failure, a loaded page with no specs block, and baseline + repeat
  unchanged runs via `run_experimental`.
- **Live controlled reads:** **yes — full end-to-end `ItelPlaywrightFetcher`
  runs against the live site, 2026-08-18** (Playwright + Chromium
  installed into the project's `.venv` for this pass). See section 5a for
  the transport re-verification and section 5a's baseline results —
  including a real bug this uncovered and fixed (noisy card-text names).
- **Baseline established:** **yes, 2026-08-18, 2 runs** — see section 5a.
- **Production promoted:** NO.

## 4. Lava research

**Official surfaces investigated:** `lavamobiles.com` (India).

**Feature-phone catalogue:** `lavamobiles.com/featurephones?subCat=all` —
11 live feature phones at investigation time (Hero 600+, A1 Josh 21, A1
Vibe, A3 Vibe, A5 23, A7 Torch, Gem Power, A3 Torch, Hero Shakti 2025, A1
2025, A5 2025).

**Smartphone separation:** clean, first-party `parent_id` field
(`"featurephones"` vs `"smartphones"`) on every product record — stronger
evidence than itel's listing-membership-only signal. `LavaCollector` still
cross-checks this against which listing a product was actually served
from and quarantines any disagreement, rather than trusting either signal
alone.

**Sitemap:** exists at `lavamobiles.com/sitemap.xml` but is **third-party
generated** (`xml-sitemaps.com` tool signature in the file), last
regenerated 2024-12-17 — stale, and does not list individual feature-phone
product URLs at all (only the category page). **Not used for discovery.**

**API/JSON evidence:** yes — the strongest of any source investigated.
`lavamobiles.com` is Next.js; every page (listing and detail) embeds a
`<script id="__NEXT_DATA__">` tag with the full page's props as JSON,
including the entire product catalogue array
(`props.pageProps.smartphoneData.all_products` — that key name, verbatim,
even on the feature-phone listing) and, per product, a clean HTML
`<th>/<td>` spec table (`product_deatil.view_details_specs` — typo
preserved verbatim from Lava's own field name). No headless browser
needed; plain `requests.get()` + JSON parse + a small regex over the
embedded HTML table.

**Blog/newsroom:** `lavamobiles.com/press-and-media` exists but returned
essentially empty CMS content in this investigation (plus the unrelated
XSS finding noted above) — not evaluated further as a launch-signal source
this pass.

**Naming/identity collision risks:** real and observed directly in the
data. `launch_date` on several currently-live "2025"-named products (e.g.
"A1 2025") is `null` or a stale 2024 date that predates the product's own
name — Lava's launch-date field cannot be trusted as a freshness signal
(handled: retained as raw evidence only, never used by the diff/event
pipeline). No stable SKU field was found either; `LavaCollector` looks for
a "Model Number" row in the spec table when present and falls back to
`None`. Year-suffixed name reuse ("A1 2025" following presumably "A1
2024"/earlier "A1") is a real pattern to watch during the soak — the
existing content-hash/diff pipeline treats these as distinct `product_key`
values (different slugs), which is correct as long as Lava never reuses a
slug for a genuinely different device; that assumption is unverified
pending real soak evidence.

**Recommended collector design:** implemented as designed — no headless
browser, `__NEXT_DATA__` JSON extraction + a small HTML-table regex.

**Confidence:** high — this is the cleanest of the three official OEM
sources technically (better than itel, comparable to HMD in reliability,
better than HMD in that Lava's category field is first-party and explicit
rather than derived from listing membership).

## 5. Lava implementation (Stage E2)

- **Status:** experimental, not production-promoted.
- **Collector:** `src/feature_phone_clank/collectors/lava.py`,
  `source_key="lava-india"`, registered but absent from
  `config/scope.yaml`, same isolation mechanism as itel.
- **Dependency:** none beyond the existing `requests` — no new dependency
  needed.
- **Fixtures/tests:** `tests/fixtures/lava/*.html` (real-shape
  `__NEXT_DATA__` snapshots, trimmed), `tests/test_lava_collector.py` — 10
  tests covering a valid feature phone, the stale-launch-date evidence
  handling, smartphone rejection (+ logged), a cross-listing conflict, a
  `parent_id` mislabel (product's own category field disagreeing with
  which listing served it), a null-specs-block product, a product-page
  fetch failure, and baseline + repeat unchanged runs via
  `run_experimental`.
- **Live controlled reads:** yes, direct `curl` fetches of the live
  listing and a live product detail page, `__NEXT_DATA__` parsed and
  inspected by hand to confirm the exact shape fixtures now mirror.
- **Baseline established:** **yes, 2026-08-18** — see section 5a.
- **Production promoted:** NO.

## 5a. Live baseline results (2026-08-18)

Both collectors were run twice against the real live sites through
`run-experimental` (isolated DB at `/tmp/itel_lava_baseline/experimental.db`,
never the production path — see section 5b for the isolation proof).

### itel — transport re-verification

Before trusting the earlier Playwright conclusion, it was re-checked with
harder evidence this pass: live network capture across a full listing-page
load and a full product-page load, `window.__NEXT_DATA__` /
`__INITIAL_STATE__` / `__APOLLO_STATE__` presence checks (all absent),
and a service-worker registration check (none registered). Every single
network request observed across both page loads was an HTML document, a
JS/CSS asset, a font, an image, or a video — zero XHR/fetch to any
JSON/API endpoint. **Conclusion unchanged: Playwright is genuinely
required**, not a default reached for prematurely.

While re-verifying, a live timeout was hit and fixed: `wait_until=
"networkidle"` hung past 20s on `/product/ace-3-heera` because the page
keeps background network chatter going indefinitely (analytics beacons,
an autoplaying video asset) — `networkidle` never arrives. Replaced with
`wait_until="domcontentloaded"` + an explicit `wait_for_selector` on the
actual content (`a[href^="/product/"]` for listings, `#specifications`
for product pages), which is the more robust signal for this class of
page. Fixed in `collectors/itel.py`; itel's 9 (now 10) fixture tests are
unaffected (fixtures exercise the parsing logic, not the wait strategy).

### itel — live baseline run 1 (fresh/empty experimental DB)

- Run status: `ok`, duration ≈8.8s.
- Candidate slugs examined: 12 (6 feature-phone cards + 3 ambiguous
  cross-listing conflicts + 3 smartphones).
- Accepted feature phones: **6**. Rejected smartphones: **3** (`a100`,
  `a100-c`, `zeno-200`). Ambiguous/quarantined: **3** (`a100-pro`,
  `zeno-100`, `zeno-100-pro` — present on both `/featurephones` and
  `/smartphones` listings, contradictory signal, correctly held back
  rather than guessed). Incomplete: **6 of 6** (General-tab-only capture,
  as documented — expected, not a defect).

**Real bug found and fixed during this run:** the listing-card "longest
text wins" name heuristic picked up the full concatenated marketing blurb
+ price for 5 of 6 products (e.g. `model` came back as `'Ace 2
Heera1.77" Big Display | 1000mAh battery | Bluetooth | Auto Call
Recording | Wireless FM with Recording1,109'`) because itel's real DOM
concatenates name+bullets+price into a single anchor's text with no
separator — different from the cleaner multi-anchor structure seen during
manual research. Fixed in `collectors/itel.py`: `Discovery.model` now
prefers the spec table's own "Model"/"Model Name" row (clean in every
product observed) and only falls back to the raw card text when no spec
table was available at all. Re-ran after the fix — all 6 names clean.
Regression test added (`test_clean_spec_table_name_wins_over_noisy_
concatenated_card_text`).

**itel accepted products (post-fix):**

| product | model/SKU | region | canonical URL | classification evidence | completeness |
|---|---|---|---|---|---|
| Ace 2 Heera | Ace 2 Heera (no separate SKU — see identity note) | india | `itel-india.com/product/ace-2-heera` | featurephones listing | incomplete |
| Ace 3 Heera | Ace 3 Heera | india | `itel-india.com/product/ace-3-heera` | featurephones listing, "new" badge | incomplete |
| Aqua | Aqua | india | `itel-india.com/product/aqua` | featurephones listing, "new" badge | incomplete |
| Flip One | Flip One | india | `itel-india.com/product/flip-one` | featurephones listing | incomplete |
| it2165C | it2165C | india | `itel-india.com/product/it2165c` | featurephones listing, "new" badge | incomplete |
| King Signal | King Signal | india | `itel-india.com/product/king-signal` | featurephones listing | incomplete |

**Identity note:** itel exposes no field distinct from the marketing model
name — `model_number` above is literally identical to `model` for every
product (sourced from the same spec-table row). **This is weaker identity
evidence than HMD**, which has real SKU codes (e.g. `1GF999TEST01`)
independent of the display name. If itel ever recycles a marketing name
for a genuinely different device, this collector cannot currently tell —
logged as a known limitation, not fixed speculatively without a real
collision to prove it matters.

**itel — smartphone rejection audit:** 12 total candidate slugs seen across
both listings; 6 accepted as feature phones, 3 rejected as smartphones
(`a100`, `a100-c`, `zeno-200` — all correctly absent from the accepted
table), 3 correctly quarantined as ambiguous rather than guessed either
way. Rejection evidence is listing membership (`{"listing_membership":
"smartphones"}` in `classification_log`), the only signal itel exposes —
same mechanism as HMD, since itel has no URL-path or first-party category
field the way Lava does.

### itel — live baseline run 2 (repeat, unchanged source)

- Run status: `ok`. New products: **0**. Field changes: **0** (`events_
  created: 0`). Removals: **0**. Identity anomalies: **0**.
  `unchanged_observations: 6` — every one of the 6 accepted products
  hashed identically to run 1.
- **Verdict: idempotent as expected.** No false `NEW_PRODUCT` events were
  generated simply because the DB was freshly populated a second time
  (baseline semantics correct — brief section 13).

### Lava — live baseline run 1 (fresh/empty experimental DB)

- Run status: `ok`, duration ≈6.3s (no browser — plain HTTP, as designed).
- Candidate slugs examined: 70 (11 feature-phone + 59 smartphone).
- Accepted feature phones: **11**. Rejected smartphones: **59**.
  Ambiguous: **0** this run (no cross-listing conflict or `parent_id`
  mismatch occurred in the live catalogue at this point in time — the
  quarantine logic exists and is fixture-tested but wasn't exercised by
  live data today). Incomplete: **4 of 11** (`a1-vibe`, `a3-vibe`,
  `a5-23`, `a7-torch` — page loaded but `view_details_specs` was empty; a
  real data-quality gap on Lava's side, not a parser bug).

**Lava accepted products:**

| product | model/SKU/product ID | region | canonical URL | classification evidence | completeness |
|---|---|---|---|---|---|
| Hero 600+ | none exposed (catalogue id `45`, not a public SKU) | india | `lavamobiles.com/featurephones/hero600-pluse` | `parent_id: featurephones` | complete (36 fields) |
| A1 Josh 21 | none exposed | india | `lavamobiles.com/featurephones/a1-josh-21` | `parent_id: featurephones` | complete (35 fields) |
| A1 Vibe | none exposed | india | `lavamobiles.com/featurephones/a1-vibe` | `parent_id: featurephones` | **incomplete (0 fields — empty spec block)** |
| A3 Vibe | none exposed | india | `lavamobiles.com/featurephones/a3-vibe` | `parent_id: featurephones` | **incomplete (0 fields)** |
| A5 23 | none exposed | india | `lavamobiles.com/featurephones/a5-23` | `parent_id: featurephones` | **incomplete (0 fields)** |
| A7 Torch | none exposed | india | `lavamobiles.com/featurephones/a7-torch` | `parent_id: featurephones` | **incomplete (0 fields)** |
| Gem Power | none exposed | india | `lavamobiles.com/featurephones/gem-power` | `parent_id: featurephones` | complete (37 fields), **price ₹2099 (only product with a live price)** |
| A3 Torch | none exposed | india | `lavamobiles.com/featurephones/a3-torch` | `parent_id: featurephones` | complete (36 fields) |
| Hero Shakti 2025 | none exposed | india | `lavamobiles.com/featurephones/hero-shakti-2025` | `parent_id: featurephones` | complete (35 fields) |
| A1 2025 | none exposed | india | `lavamobiles.com/featurephones/a1-2025` | `parent_id: featurephones` | complete (36 fields) |
| A5 2025 | none exposed | india | `lavamobiles.com/featurephones/a5-2025` | `parent_id: featurephones` | complete (36 fields) |

Model/SKU is explicitly **not available** for any Lava product — stated
here rather than invented from the slug, per instruction. Only `Gem Power`
carries a live official price (₹2099); every other product's `price` field
is `null` in Lava's own data.

**Lava — smartphone rejection audit:** 70 total candidates, 11 accepted,
**59 rejected as smartphones** — a much larger rejection set than itel's,
correctly excluded via the first-party `parent_id: "smartphones"` field
(stronger evidence than itel's listing-membership-only signal). Spot-
checked several rejected slugs (`agni-3`, `blaze-x`, `yuva-5g`, `bold-n1`)
— all genuinely Android smartphone product lines, not misclassified
feature phones.

**Identity note — live examples of the `launch_date` caveat documented in
section 4:** `A1 2025`, `A5 2025`, and `Hero Shakti 2025` are three
currently-live, year-suffixed refresh names. None of the three's raw
`launch_date` values were checked to still be misleadingly stale at
baseline time (that inspection is deferred to the second-run diff below,
where a changing/inconsistent `launch_date` on an otherwise-unchanged
product would show up as evidence the field is unreliable, without the
diff pipeline ever treating it as a freshness signal — the field is
carried in `raw` only and is excluded from `content_hash()` by
construction, not by a runtime check). No slug reuse or duplicate
catalogue entries were observed among the 11 accepted products.

### Lava — live baseline run 2 (repeat, unchanged source)

- Run status: `ok`. New products: **0**. Field changes: **0**. Removals:
  **0**. Identity anomalies: **0**. `unchanged_observations: 11` — all 11
  hashed identically to run 1, including the 4 incomplete ones (an empty
  `fields={}` hashes consistently, as intended).
- **Verdict: idempotent as expected**, same as itel.

## 5b. Experimental isolation proof

Before/after check, not inferred from file paths alone: a synthetic
"production-shaped" database was created locally (`/tmp/itel_lava_
baseline/pretend_prod.db`, 1 fake HMD product written through the real
`run_production_collector` path, matching production's actual schema/
tables) specifically so a real before/after comparison was possible — no
production database exists on this Mac at all (production state lives
only on the Hetzner host and the Windows scheduler), which is itself a
trivial but real isolation fact: `run-experimental` cannot mutate a file
that was never opened locally.

- SHA-256 of `pretend_prod.db` **before** the itel/Lava live runs:
  `d9dae650781e79b4fda6ce68b53d93c2a4d9a0872331f020fcde4c47c10ce295`.
- SHA-256 of `pretend_prod.db` **after** both itel and Lava live runs
  (4 total experimental invocations): **identical** —
  `d9dae650781e79b4fda6ce68b53d93c2a4d9a0872331f020fcde4c47c10ce295`.
- Per-table row counts before and after (both identical): `sources=1,
  products=1, observations=1, events=0, collector_runs=1, run_errors=0,
  classification_log=0, notifications=0`.
- `config/scope.yaml` byte-for-byte unchanged (`hmd-nokia` only) —
  confirmed via `git diff` against `origin/main`, zero diff.
- No `data/` directory was ever created on this machine by any
  `run-experimental` invocation (confirmed via `ls`) — the production
  default DB path was never even opened, let alone written.
- Experimental DB: `/tmp/itel_lava_baseline/experimental.db` (ad hoc path
  for this baseline; the shipped default is `data/feature_phone_clank_
  experimental.db`). Experimental lock: `data/feature-phone-clank-
  experimental.lock` (unused this pass — `--no-lock` was passed for these
  manual runs; the lock file itself is a separate path from production's
  `data/feature-phone-clank.lock`, confirmed by reading `cli.py`, not
  exercised live).

## 5c. Lava source-safety note (press/media page)

The collector surface actually used by `LavaCollector` is
`/featurephones?subCat=all`, `/smartphones?subCat=all`, and
`/featurephones/<slug>` product pages **only** — `/press-and-media` (where
the earlier research pass found the suspicious injected `<script>` tag) is
never fetched by any code path in `collectors/lava.py`, confirmed by
reading the file: there is exactly one `FEATURE_LISTING_URL`, one
`SMARTPHONE_LISTING_URL`, and per-slug `{BASE}/featurephones/{slug}`
construction — no other URL is ever requested. No runtime logic was added
around the observation; it remains a neutral, out-of-band note for the
owner, not something FEATURE-01 reacts to. Nothing from that page was
re-fetched or copied into fixtures/docs during this pass.

## 5d. HMD Hetzner operational note (unchanged, not investigated further)

Not re-checked live this pass (out of scope per instruction — this is an
itel/Lava-only branch). As of the previous pass's finding: the last
observed scheduled HMD runs on Hetzner (2026-08-18 01:15 and 07:15 UTC)
failed with `ReadTimeout` against `www.hmd.com`; the catastrophic-zero
guard correctly held the prior 44 products rather than corrupting state.
Whether that timeout is still ongoing as of this later pass was not
re-verified (would require another Hetzner SSH session, out of this
branch's scope). Production data integrity as of the last check: intact,
stale-but-safe. **Flagged again: a separate follow-up session should
check current HMD run status on Hetzner** — this branch does not touch
HMD transport/retry logic.

## 6. Specialist-source hunt

Full matrix (▲ = additional candidates surfaced during the "expand beyond
the known list" search, not in the brief's seed list):

| Source | Active? | Feature-phone focus | Source type | Freshness | Feed/sitemap | Discovery value | Technical feasibility | Recommendation |
|---|---|---|---|---|---|---|---|---|
| Moving Offline (Jose Briones, `josebriones.substack.com`) | Yes — latest post 2026-08-15, ~biweekly cadence | High (digital minimalism / dumbphone-adjacent, not device-news-first) | Editorial essay/newsletter | Good — active RSS, real pubDates | Yes, standard Substack `/feed` RSS | Medium — strong community voice, low device-launch yield (titles are personal-essay style: "Are Dumbphone Owners Losing Their Minds?", not "X launches Y") | High — plain RSS, trivial to poll | **ACTIVE SPECIALIST — SECONDARY** |
| Dumbphone Finder / dumbphones.org (same author, Jose Briones) | Yes, live | High | Directory/finder tool + affiliate links | Unclear — no per-item timestamps found | No feed found | Medium — structured device-comparison tool, but commercially affiliate-linked (Sunbeam Wireless, Light Phone referral links found on-page) | Medium — no feed, would need scraping the finder tool's own data | **COMMERCIAL DISCOVERY** (reclassified from the brief's presumed "ACTIVE SPECIALIST DISCOVERY" — the affiliate-link evidence found during verification changes this) |
| Dumbph.com / "The Dumbphones Blog" | Unclear — fetched successfully, but no dated content found on the pages inspected | Presumed high (name-implied) | Blog + finder | Unverified | Not checked | Unverified — not deep-verified this pass | Unverified | **REFERENCE / CORROBORATION** (per the brief's own suggested fallback classification when editorial cadence can't be confirmed active) |
| BananaHackers (`wiki.bananahackers.net`, `blog.bananahackers.net`) | Largely dormant — most recent substantive community post found dates to 2023; search results describe 2026 activity as "not much happening" | Very high when active (KaiOS-specific) | Wiki + community blog | Poor — stale | Wiki exists, no evidence of active feed | Low currently — high historical/reference value | High technically (static wiki) but pointless for a live collector given inactivity | **REFERENCE / ARCHIVE** |
| Dumbwireless (`dumbwireless.com`) | Yes, live | High (dumbphone-specific storefront) | Commercial retailer + blog | Unclear | Not checked | Medium — retailer editorial copy, not independent reporting; useful for availability/pricing signals only | Medium | **COMMERCIAL DISCOVERY** |
| ▲ PauseGadget (`pausegadget.com`, incl. `/dumbphone-finder` — "Compare 70+ Minimalist Phones") | Appears active (June 2026 dated content found) | High | Buying-guide blog + structured comparison tool | Unverified beyond one dated post | Not checked | Medium-high — the 70+ device comparison table is a genuinely large structured dataset if verified live | Unverified | **ACTIVE SPECIALIST — SECONDARY (needs deeper verification before promotion)** |
| ▲ Keyphone.tech | Surfaced in search, not deep-verified | Claimed high (name-implied) | Buying-guide blog | Unverified | Not checked | Unverified | Unverified | **RESEARCH — insufficient evidence to classify yet** |
| ▲ dumbphone.in | Surfaced in search ("The Definitive Guide to the World's Best Dumb Phones, 2025–2026") | High (name-implied) | Buying guide | Unverified | Not checked | Unverified | Unverified | **RESEARCH — insufficient evidence to classify yet** |
| ▲ flipphonefinder.com | Surfaced in search | High (name-implied, flip-phone-specific) | Finder/directory | Unverified | Not checked | Unverified | Unverified | **RESEARCH — insufficient evidence to classify yet** |
| GSMArena, PhoneArena, FoneArena, Notebookcheck, Android Authority, Android Police, 91mobiles, Gizmochina, TechRadar, The Verge, Android Central, Korben | Yes | Low (general mobile/tech coverage; feature phones are an occasional topic, not the focus) | General tech publication | N/A | N/A | N/A — explicitly out of scope per brief section 4 | N/A | **REJECT — TOO GENERAL** |

**Overall conclusion:** no specialist source surfaced this pass clears the
bar for "ACTIVE SPECIALIST — HIGH PRIORITY" with verified evidence strong
enough to implement immediately. Moving Offline is the most-verified
active, genuinely feature-phone-adjacent source, but its editorial style
(personal essays, not device announcements) means its discovery *value*
for "new device" leads specifically is real but modest — it's a community-
sentiment signal more than a device-launch sensor. PauseGadget's 70+-device
comparison table is the most promising lead for actual device-discovery
volume but needs a second verification pass (activity cadence, structure,
scrape feasibility) before a collector is justified. Three additional
candidates (Keyphone.tech, dumbphone.in, flipphonefinder.com) are logged
for future research, not yet classified.

### Specialist collectors: implementation status

**Not implemented this pass — a deliberate stop, not an oversight.**

Two reasons converge:

1. No specialist candidate cleared verification to "ACTIVE SPECIALIST —
   HIGH PRIORITY" with confidence to build against (see matrix above) —
   building a collector now would mean picking the best of a
   not-fully-verified set, contrary to "two strong specialist sources are
   preferable to ten mediocre ones" (brief section E3 note) and "a source
   can be technically perfect and still be rejected for low intelligence
   value" (section 41).
2. **A concrete architecture question is unresolved and matches one of the
   brief's own stop conditions** ("specialist evidence cannot fit cleanly
   into current event model" — section 44). `core/models.py`'s `ChangeType`
   enum (`NEW_PRODUCT`, `FIELD_CHANGED`, `IDENTITY_ANOMALY`, etc.) is
   built entirely around diffing two observations of the *same* canonical
   product — there is no shape in it for "a third party mentioned
   something," and forcing a specialist mention through `NEW_PRODUCT`
   would be exactly the "do not automatically call a specialist lead
   NEW_PRODUCT" mistake the brief explicitly warns against (section 19).
   A new type (`SPECIALIST_LEAD` or similar) is the right fix, but it's a
   small `models.py` schema decision that affects `core/diff.py`,
   `core/pipeline.py`, and the SQLite `events` table shape — worth a
   deliberate one-paragraph design note reviewed with the owner rather
   than bolted on inside this already-large pass.

**Recommended next step:** once the owner picks a specialist-event design
(this doc proposes: a new `ChangeType.SPECIALIST_LEAD`, produced directly
by a specialist collector rather than through the identity-diff pipeline
at all, carrying `source`, `article_url`, `publication_date`,
`first_seen_at`, `referenced_manufacturer`, `referenced_product_guess`,
`likely_official_match` (nullable `product_key`), `confidence`, and
`why_surfaced` — matching brief section 19's required fields), implement
Moving Offline (RSS, easiest technically) and re-verify PauseGadget's
comparison table as the two candidates, per "two strong sources over ten
mediocre ones."

## 7. New OEM research queue

| OEM | Active feature-phone business? | Editorial relevance | Official collector feasibility | Specialist mentions | Priority | Decision |
|---|---|---|---|---|---|---|
| HMD/Nokia | Yes (production reference) | High | Already built | High | — | Existing production source |
| itel | Yes (this pass's target) | High | Built, experimental | High | — | Stage E1 complete |
| Lava | Yes (this pass's target) | High | Built, experimental | High | — | Stage E2 complete |
| Light (Light Phone III) | Yes, single flagship-style product line | High in minimalist-phone community | Likely feasible (small catalogue, probably simple site) | Very high — referenced repeatedly across specialist search results and dumbphones.org's own affiliate links | **CORE NEXT WAVE** | Strong candidate for the next OEM expansion pass |
| Punkt (MP02) | Yes, small catalogue | High | Likely feasible (small site) | High | **CORE NEXT WAVE** | Strong candidate |
| Mudita (Kompakt) | Yes | Medium-high | Unverified technically | Medium-high | **SECONDARY** | Worth a technical pass before Light/Punkt if either stalls |
| AGM (M9, rugged 4G) | Yes, but primarily a rugged-smartphone brand with a feature-phone line | Medium | Unverified | Medium | **SECONDARY** | Rugged-phone brand — needs care distinguishing feature-phone SKUs from Android ruggedized phones |
| Doro | Yes, senior-focused feature/basic phones, long-established | Medium | Unverified | Low-medium in this pass's search results | **SECONDARY** | Established brand, not deeply investigated this pass |
| Maxwest | Appears in 2026 dumbphone guides | Low-medium | Unverified | Low | **RESEARCH** | Thin evidence this pass |
| Sunbeam Wireless | Referenced as an affiliate partner on dumbphones.org | Unverified — commercial relationship found, not editorial coverage | Unverified | Low (mostly appears via affiliate links, not independent coverage) | **RESEARCH** | Investigate whether this is a real OEM or a reseller/rebadger |
| Jio | — | — | — | — | **OUT OF SCOPE** | INTENTIONALLY EXCLUDED — NOT RESEARCHED / NOT IMPLEMENTED, per owner instruction |

## 8. Cross-source evidence model

- **Official authority:** HMD/Nokia (production), itel, Lava (both
  experimental) — each collector's `Discovery.source_key` is the
  provenance tag; nothing merges across `source_key` values automatically
  anywhere in the existing pipeline (`core/pipeline.py` diffs within a
  `source_key`, never across).
- **Specialist authority:** none implemented yet (see section 6) — when
  built, specialist evidence is explicitly a *lead*, never a canonical
  product mutation, per brief section 3B/17.
- **Reference authority:** none implemented yet (dumbph.com and similar
  are logged as candidates, not integrated).
- **Duplicate handling:** not yet exercised in this pass — itel and Lava
  have disjoint catalogues from HMD (different manufacturers, so
  `product_key` — `f"{source_key}:{slug}"` — never collides). A real
  cross-manufacturer duplicate scenario (e.g. the same device rebadged)
  hasn't been observed in real data yet; premature to build correlation
  logic against zero real evidence, consistent with brief section 18's
  "if no safe automatic merge exists, preserve separate evidence" default,
  which is exactly what happens today by construction (nothing merges).
- **Canonical-product mutation rules:** unchanged from the existing
  architecture — a collector's own `Discovery` objects are the only thing
  that can create/update its own `source_key`'s products.
  `run_experimental` guarantees itel/Lava can never write to the
  production store no matter what they observe.

## 9. Event model

- **New event types added:** none yet. `ChangeType` in `core/models.py` is
  unchanged.
- **Why none were added:** itel and Lava reuse every existing `ChangeType`
  value directly (`NEW_PRODUCT`, `FIELD_CHANGED`, `IDENTITY_ANOMALY`, etc.)
  — nothing about their data shape required a new type. The specialist-lead
  question (section 6) is the one place a new type (`SPECIALIST_LEAD`) is
  proposed but deliberately not yet implemented.
- **Notification behaviour:** unchanged — PR #6 (Discord/outbox) merged to
  main as `73793fb` *after* this document was written (2026-08-18); the
  statement "not merged to main" below was true at authoring time and is now
  historical. This pass didn't touch it, per the owner's explicit
  instruction to leave it alone.
- **Experimental Discord behaviour:** N/A — no notification path is wired to
  itel/Lava at all (they're not in `config/scope.yaml`, and
  `run_experimental` never invokes any Discord/outbox code). No risk of
  experimental noise reaching the owner's real feed.

## 10. Experimental isolation

- **DB/state strategy:** a new `run-experimental` CLI subcommand
  (`cli.py`) writes to `--experimental-db` (default
  `data/feature_phone_clank_experimental.db`), completely separate from
  `--db` (default `data/feature_phone_clank.db`, the production path).
  Its own lock file (`data/feature-phone-clank-experimental.lock`) so an
  experimental run never blocks or is blocked by a concurrent production
  `run`.
- **Production HMD safety:** `config/scope.yaml` still lists only
  `hmd-nokia`; `run-experimental` explicitly skips (with a
  `"now_production_scoped"` result, not silent) any source_key that IS in
  production scope, so a future promotion can't accidentally leave stale
  experimental-path history running against it.
- **Evidence provenance:** every `Discovery.raw` for itel/Lava carries
  source-specific fetch/classification evidence (fetch failures, badge
  flags, catalogue IDs, the Lava launch-date caveat, etc.) — same pattern
  as `hmd.py`'s `raw` field.

## 11. Soak plan (proposed, NOT activated)

- **Cadence:** feature-phone catalogues don't need aggressive polling, and
  the two sources have meaningfully different runtime costs — different
  cadences, not forced to match:
  - **itel: 3x/day.** Headless-browser execution (~9s/run observed for 6
    products; scales roughly linearly with catalogue size and each
    product needing its own browser navigation) is the heavier of the two.
  - **Lava: 4x/day**, matching HMD's existing cadence. Plain HTTP + JSON
    parse (~6s/run observed for 70 candidate products, most of that in
    the per-accepted-product detail fetches) — cheap enough not to need a
    lighter schedule.
- **Sources:** itel-india, lava-india.
- **Duration:** brief's own target, 3-5 real days, once actually started.
- **Dependencies:** itel needs `pip install feature-phone-clank[itel]`
  (Playwright) **and** `python -m playwright install chromium` on
  whichever host runs it — a real deployment step, not just a pip install.
  Lava needs nothing beyond the base install.
- **DB/state:** `--experimental-db data/feature_phone_clank_experimental.db`
  (a new, separate SQLite file/volume from production's
  `data/feature_phone_clank.db` — same named-Docker-volume pattern as
  production if deployed via Hetzner, just a different volume name, e.g.
  `feature_phone_clank_experimental_staging_data`).
- **Lock path:** `data/feature-phone-clank-experimental.lock` — separate
  from production's `data/feature-phone-clank.lock`, confirmed by
  reading `cli.py`'s argument defaults; an experimental run cannot block
  or be blocked by a concurrent production `run`.
- **Logs:** same convention as the existing Hetzner cron logs
  (`logs/cron-YYYYMMDD.log`), a parallel `logs-experimental/` directory
  recommended to avoid interleaving with production's log stream.
- **Scheduler changes:** none made or proposed as active — this is a
  proposal for the owner to activate deliberately, likely as a second
  cron entry / Task Scheduler job pointed at the experimental compose
  service, once the owner reviews this report.
- **Rollback:** trivial — stop the experimental cron entry; the
  experimental DB/volume is fully separate from production, so there is
  nothing to roll back on the production side regardless of what happens
  to the experimental one.
- **Metrics:** the existing `collector_runs`/`run_errors`/
  `classification_log` tables already capture everything section 25 of the
  brief asks for per-OEM-source — no new soak-metrics schema was needed,
  itel/Lava are just new rows under new `source_key`s in the same tables
  HMD already uses. `cli.py report`/`status`/`events` all already accept
  a `--db` pointing at the experimental database.
- **Review labels:** `ReviewOutcome` enum (`HIT`/`INTERESTING`/`NOISE`/
  `BUG`) already exists in `core/models.py` — reused as-is.
- **Resource usage (observed, not estimated):** itel ≈9s wall-clock per
  run for 6 accepted products + Chromium's own memory footprint while the
  browser is open (a few hundred MB, released on `browser.close()` after
  each page); Lava ≈6s wall-clock, memory-light (no browser).
- **Not activated:** no cron/Task Scheduler entry was created, no Hetzner
  deployment was made, per explicit instruction for this pass.

## 12. Live baseline review gate — source readiness

**itel: NEEDS HARDENING.**
Reasons: (1) the live baseline caught and required an immediate parsing
fix (noisy concatenated card-text names) — the fix is now in and
regression-tested, but it demonstrates the DOM-scraping-via-Playwright
approach is more brittle than Lava's structured-JSON approach, and only
one real crawl's worth of evidence exists that the fix generalizes;
(2) identity evidence is weak — no field distinct from the display name
exists at all (see section 5a), which is a real, documented, unresolved
gap relative to HMD; (3) only 2 live runs total — the brief's own
promotion bar ("repeated real runs") isn't met by 2. Discovery
completeness, classifier correctness (6/6 accepted correctly, 3/3
smartphones correctly rejected, 3/3 genuine conflicts correctly
quarantined), and repeat-run idempotency all look solid on the evidence
gathered — this is "needs more soak time and identity work," not "broken."

**Lava: READY FOR EXPERIMENTAL SOAK.**
Reasons: clean discovery (11/11 accepted correctly), clean smartphone
rejection at real scale (59/70 correctly rejected), zero ambiguous
misclassifications in live data, idempotent repeat run, cheap runtime
cost, no parsing bugs found. The identity gap (no SKU field, same as
itel) and the `launch_date` unreliability are real but already
documented and already excluded from the diff pipeline by construction —
not blocking issues, just known limitations to watch during a soak.

Neither is READY FOR PRODUCTION — that's a separate, later bar (brief
section 40/41: reliable identity, owner review, multi-day soak evidence),
not evaluated here.

## 13. Tests

- Previous canonical (before this branch's work): 91 passed, 1 skipped.
- New tests added: 10 (itel, including 1 regression test for the live-
  caught bug) + 10 (Lava) + 2 (run-experimental CLI wiring) = 22.
- Canonical after this work: **113 passed, 1 skipped**, 0 warnings.
- No live network calls anywhere in the automated suite — itel/Lava tests
  use fixture data exactly like the existing HMD tests; the live
  Playwright/HTTP runs in section 5a were manual, out-of-suite baseline
  runs, not part of `pytest`.

## 14. Git

- **Branch cleanup performed this pass:** the prior session's uncommitted
  work was sitting in the working tree of `agent/fix-macos-bundle-
  identifier` (1 commit ahead of `origin/main`, an unrelated macOS
  bundle-identifier fix, PR #8). Verified via `git diff origin/main HEAD`
  that the *only* difference between that branch's HEAD and `origin/main`
  was `native/macos/build.sh` — none of the 3 tracked files this work
  modifies (`pyproject.toml`, `cli.py`, `collectors/__init__.py`) differ
  between the two, so `git checkout -b expansion/itel-lava origin/main`
  cleanly carried every uncommitted expansion change onto the new branch
  while leaving `agent/fix-macos-bundle-identifier` and PR #8 completely
  untouched (verified after the fact: `git show --stat` on that branch
  still shows only its own original commit).
- **Files changed** (verified zero UNRELATED — see full classification in
  the PR description / commit list below): `src/feature_phone_clank/
  collectors/itel.py` (new), `src/feature_phone_clank/collectors/lava.py`
  (new), `src/feature_phone_clank/collectors/__init__.py` (registration),
  `src/feature_phone_clank/cli.py` (`run-experimental` subcommand),
  `pyproject.toml` (`itel` optional extra), `tests/fixtures/itel/*`,
  `tests/fixtures/lava/*`, `tests/test_itel_collector.py`,
  `tests/test_lava_collector.py`, `tests/test_run_experimental_cli.py`,
  `docs/FEATURE_PHONE_SCOPE_EXPANSION.md`.
- `config/scope.yaml`: confirmed **unchanged** (`git diff origin/main --
  config/scope.yaml` — zero diff) — itel/Lava remain experimental.
- PR #6 (notifications/Discord): confirmed **not modified, not rebased,
  not cherry-picked, not depended upon** — grepped `itel.py`/`lava.py`/
  `cli.py` for any notification/Discord/outbox reference: none found.

## 15. Deployment

- Hetzner changed: **NO**.
- Production Discord changed: **NO**.
- NAS changed: **NO**.
- Production `scope.yaml` changed: **NO**.
- Production DB changed: **NO** (see section 5b — nothing local to touch,
  and the isolation mechanism itself was proven, not just assumed).

## 16. Findings requiring owner decision

1. **Lava's official website appears to have a stored-XSS compromise** on
   its `press-and-media` page (see section 1/5c). Outside this project's
   control; the collector never touches that page (confirmed this pass).
   Owner may want to notify Lava independently.
2. **HMD/Nokia's Hetzner `ReadTimeout` issue from the previous pass was
   not re-checked this session** (out of scope for this itel/Lava-only
   branch, per instruction) — still an open item for a separate
   follow-up; not known to be resolved or worsened.
3. **Specialist-event model decision** (section 6) — a `SPECIALIST_LEAD`
   `ChangeType` (or equivalent) needs a deliberate go-ahead before any
   specialist collector is implemented. Unchanged from the previous pass;
   explicitly not touched in this one either.
4. **itel needs Playwright + a Chromium binary** on any host that runs it
   — a real deployment step (`playwright install chromium`, ~270MB),
   beyond just a pip install, for whichever environment eventually runs
   the itel soak.
5. **itel identity is weaker than HMD's** (section 5a) — no SKU-like field
   independent of the display name. Worth a decision on whether this is
   acceptable for a soak-stage source (probably yes, to gather evidence
   on whether it ever actually causes a collision) or needs a fix first.

## 17. Verdict

**PARTIAL — HARDENING REQUIRED (itel); LAVA READY FOR EXPERIMENTAL SOAK.**

E0 (truth + research), E1/E2 (itel, Lava experimental collectors, now with
a real 2-run live baseline each), and this branch-cleanup pass are
complete. Lava clears the live-baseline review gate for an experimental
soak. itel does not yet — its transport is confirmed correct and its
classifier is confirmed correct on live data, but its identity evidence is
weak and it only has 2 real runs behind it, against a live-caught-and-
fixed parsing bug in the same session. E3 (specialist collectors) remains
deliberately not implemented — a real architecture decision (new event
type) is needed first, matching one of the brief's own stop conditions.
Neither itel nor Lava is production-promoted; the DB retirement/reset has
not been touched.

## 18. Experimental Hetzner soak deployment (2026-08-18)

A 3-5 day unattended soak is now live for both sources, deployed
completely separately from production HMD.

- **Checkout:** `/home/anilganti/feature-phone-clank-experimental/`
  (my own home directory — deliberately outside `/home/deploy/staging/`,
  the deploy user's production tree), pinned to commit `49eab25` on
  `expansion/itel-lava`.
- **Image:** `feature-phone-clank-experimental:49eab25`, built from the
  new `Dockerfile.experimental` (adds the `[itel]` extra +
  `playwright install --with-deps chromium` on top of the same base image
  and non-root-user pattern as production's `Dockerfile`).
- **Compose:** `docker-compose.experimental.yml` — two services (`itel`,
  `lava`) sharing one image, one named volume
  (`feature_phone_clank_experimental_data`, distinct from production's
  `feature_phone_clank_staging_data`), one DB
  (`data/feature_phone_clank_experimental.db`) and one lock
  (`data/feature-phone-clank-experimental.lock`) — `RunLock` serializes
  the two if their cron times ever collide.
- **Scheduler:** cron entries under **my own `anilganti` crontab**, not
  `deploy`'s (no write access there, and this keeps production cron
  physically incapable of referencing the experimental stack): itel at
  02:00/10:00/18:00 UTC (3x/day), Lava at 01:30/07:30/13:30/19:30 UTC
  (4x/day, offset 30min from HMD's own 01:15/07:15/13:15/17:15 to avoid
  resource contention on shared cron minutes).
- **Logs:** `logs-experimental/cron-itel-YYYYMMDD.log`,
  `logs-experimental/cron-lava-YYYYMMDD.log`.
- **Controlled validation (before enabling cron):** one manual run each
  via `scripts/deploy_run_experimental.sh`, both against the live sites,
  both matching the local baseline exactly — itel: 6/6 accepted products,
  clean names (the card-text fix holds inside the container too); Lava:
  11/11 accepted products. itel's Playwright/Chromium path confirmed
  working inside the container (`--with-deps` system libraries installed
  correctly at build time) — 15.7s wall clock for the containerized run
  vs ~8.8s locally, reasonable overhead.
- **Isolation proof (before/after the two validation runs):** production
  HMD volume's DB SHA-256 identical before and after
  (`fff75c93f19c733ea66197642fe007445b002c02e6a92a8757344ae66693d47c`);
  `docker volume ls` confirms two fully separate volumes exist; the
  production checkout's `git log`/`.deployed-id` (`c749df3`) unchanged;
  `config/scope.yaml` in both checkouts still lists `hmd-nokia` only.
- **Production Discord:** not touched — no notification/Discord code
  exists anywhere in the itel/Lava/runner code path (confirmed by
  earlier grep), and PR #6 (where that code lived at authoring time) was
  never merged into this branch. (It has since merged to main as
  `73793fb`; the experimental runner still wires no notifier.)
- **Monitoring during the soak:** `feature-phone-clank status --db
  data/feature_phone_clank_experimental.db` / `events` / `report` (all
  already support pointing at the experimental DB) from inside the
  experimental checkout, or via
  `docker run --rm -v feature_phone_clank_experimental_data:/app/data
  feature-phone-clank-experimental:49eab25 status`.
- **Tracked per source, per the soak's own review criteria:** product
  count, canonical URL stability, display-name stability, identity
  anomalies, false events, ambiguous count, incomplete products, parser/
  transport failures, run duration — all already captured by the existing
  `collector_runs`/`run_errors`/`classification_log`/`events` tables, no
  new schema needed.
- **Rollback:** remove the two cron lines from `anilganti`'s crontab
  (`crontab -e`); the volume/directory are fully separate from
  production, so nothing else needs to change regardless of outcome.
- **Not done, per instruction:** no identity-layer changes (only a
  concrete live collision/rename/URL-churn/duplicate would justify one),
  no specialist-source work, no production promotion, no combining with
  PR #6.
