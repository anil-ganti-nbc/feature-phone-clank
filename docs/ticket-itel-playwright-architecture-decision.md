# Ticket: itel-india — production image architecture decision (Playwright)

Status: BLOCKED from production promotion (2026-08-30 final review) on an **architectural /
deployment-policy** decision, not collector quality. Planning artefact — decision required, no
implementation yet.

## Problem statement

`itel-india` completed a clean soak (38/38 natural cycles, 6 products, clean quarantine) in the
experimental lane, but the production image **cannot execute the collector**: it requires a
Playwright/Chromium browser dependency, and by design only `Dockerfile.experimental` carries it.
The production image is deliberately lean.

## Observed evidence

- Experimental soak: 38/38 ok (anilganti lane, `feature-phone-clank-experimental` image with the
  browser extra), 6 products persisted, smartphone/accessory quarantine behaving.
- Production image (`feature-phone-clank:<sha>`, built from the production `Dockerfile`) lacks the
  browser extra; `collectors/itel.py` imports the Playwright-backed fetch path at collector start.

## Root cause

Dependency-set asymmetry between the experimental and production images, applied deliberately when
the production image was kept lean — before itel (the only browser-dependent collector) existed.

## Decision required (pick one)

### OPTION A — add Playwright/Chromium to the production Feature Phone image

Evaluate before choosing:

- image-size impact (browser + deps: typically +300–500 MB);
- dependency/attack surface (Chromium CVE cadence — a recurring patch burden on a production image);
- memory overhead at run time (headless Chromium alongside the crawler);
- cold-start cost for the 4×/day scheduled runs;
- whether any other production collector benefits today (currently: none — itel is the only
  browser-dependent collector in the fleet's feature-phone scope);
- isolation implications (browser in the same container as the DB-writing pipeline vs a separate
  worker).

### OPTION B — keep the production image lean; itel remains experimental/non-production

Evaluate:

- itel's editorial value vs the overhead of Option A (6-product catalogue so far);
- whether a separate Playwright-enabled worker image is an accepted pattern in this repo/fleet —
  precedent exists in spirit (`Dockerfile.experimental` vs production split; smartwatch's
  macOS/Windows packaging split), but a *third production-adjacent image* is a new deployment
  surface: its own build/deploy/backup wiring;
- the option to revisit later without prejudice: the collector code, soak evidence and scope-gate
  path all remain valid whenever the decision lands.

## Recommendation (smallest architecture consistent with fleet principles)

**Option B**, unless itel's editorial yield becomes material: the production image stays lean, the
experimental lane remains itel's home (it is already stable there), and no new deployment surface is
created for a single 6-product source. If a second browser-dependent source ever becomes production-
interesting, revisit Option A or the dedicated-worker pattern with real justification.

This ticket does **not** select Option A merely because it enables promotion.

## Explicit non-goals

- No image changes, no dependency additions, no scope.yaml change, no collector changes.

## Tests required (if/when a decision is implemented)

- Option A: image build succeeds with the extra; itel collector smoke-passes in the production
  image; image-size/CI budget documented.
- Option B: none (document the decision in this file and/or the repo ADR set).

## Soak required after repair

Option A: itel must soak under the **production image** (not the experimental one) before promotion
— the current experimental-lane soak validated the collector logic but not the production runtime
dependency set. Prefer ≥20 natural cycles under the production image, then re-review.
Option B: none — the source simply remains experimental by documented decision.

## Production exit condition

Decision recorded → (A: production-image soak + re-review, then scope.yaml) or (B: itel documented
as permanently experimental in scope.yaml comments/SOURCE inventory).

## Rollback considerations

Option A is image-level; rollback = previous image tag via `.deployed-id`. Option B is
documentation-only.

## Risk level

LOW to implement either way; the real cost of Option A is ongoing maintenance, which is why the
decision is explicit rather than accidental.
