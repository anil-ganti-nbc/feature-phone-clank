# Ticket: lava-india — port the resilient fetch pattern (transport repair)

Status: BLOCKED from production promotion (2026-08-30 final review). Planning artefact — no
implementation yet.

## Problem statement

`lava-india`'s collector carries its own `HttpFetcher` that predates the hmd fetcher repair
(`915f908`): single attempt, fixed 15s timeout, response body read **outside** the protected
error path. One mid-transfer stall escapes as `ReadTimeout`, propagates through `collect()`,
and fails the whole run, losing every successful fetch made earlier in the run.

## Observed evidence

- 3 whole-run `ReadTimeout` failures across ~50 natural cycles in the anilganti experimental lane
  (Aug 22 ×2, Aug 28 07:30Z); 47/50 otherwise healthy. Production DB `run_errors` shows the same
  failure family for hmd-nokia before its repair (24/27 runs, Aug 23–29).
- The hmd repair (`915f908`) demonstrably fixed the identical failure mode: the 2026-08-30 17:15Z-
  era production runs survived transient 503s and a hard stall via retry + graceful fallback.

## Root cause

Lava's fetcher was forked from the same early pattern hmd had, and never received the repair.
Not shared code: `collectors/lava.py` defines its own `Fetcher`/`HttpFetcher` (~lines 78–135),
separate from `collectors/hmd.py`.

## Exact affected code/config

- `src/feature_phone_clank/collectors/lava.py` — `HttpFetcher.get()` and `FetchResult`.
- Reference implementation: `src/feature_phone_clank/collectors/hmd.py` post-`915f908`
  (`DEFAULT_HTTP_TIMEOUT = (10.0, 30.0)`, `max_attempts = 3`, `backoff_s = 2.0` doubling,
  `_classify_network_error`, body download inside the `try`, `FetchResult.error` classification,
  exhausted-retry `status=0` routing into existing fallback paths).

## Minimum viable repair

Replicate the `915f908` behaviour inside `lava.py`'s `HttpFetcher`:

- bounded retries (3 attempts, exponential backoff 2s/4s) on transport failures and transient
  429/5xx; a final 429/5xx is returned verbatim (status remains the evidence);
- `(connect, read) = (10, 30)` hard timeout ceiling informed by the 2026-08-29 host measurements;
- response body downloaded inside the same `try` as the request;
- `FetchResult.error` transport classification; every existing `status == 200` call site keeps its
  fallback semantics (status=0 routes into the same skip/fallback paths).

Do NOT extract a shared fetcher module for all collectors in this ticket (sunbeam/doro/etc. share
the same pre-repair pattern — recorded as backlog; dedupe is a separate reviewed change).

## Explicit non-goals

- No Lava parser/catalogue changes; no cadence/schedule change; no scope.yaml change in this
  ticket; no behaviour change for other collectors.

## Tests required

- Mocked-transport regressions mirroring `tests/test_http_fetcher.py`: mid-body stall → retry →
  success; exhausted retries → classified `status=0` failure that `run()` records without raising;
  transient 503 → retry → success.
- Full existing suite green (production-image container run is the authoritative environment).

## Soak required after repair

Fresh soak clock at the first post-fix natural run (experimental lane or production-lane soak per
operator choice); **preferably ≥20 natural cycles**; zero whole-run transport failures in the
trailing qualification window; catalogue count stable (~11 products); zero false novelty.

## Production exit condition

Standard re-review under the existing policy, then add `lava-india` to `config/scope.yaml`
`production_collectors` via the normal reviewed commit + image build + `.deployed-id` path.

## Rollback considerations

Single-commit revert; no DB migration; soak evidence pre-fix is preserved and stays attributable
to the old fetcher (run_errors rows already classify the failures).

## Risk level

LOW — proven pattern, isolated collector, no state semantics touched.
