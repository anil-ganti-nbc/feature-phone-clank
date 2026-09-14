# Chronological observed state (schema v7)

Previously, `observations` deduplicated `(product_id, content_hash)` while
`latest_observation()` selected the highest canonical observation ID. A→B→A
reused old A and reported unchanged, leaving B current. Event identity based
only on the two content IDs also suppressed a later genuine A→B transition.

## Storage and transaction contract

Canonical `observations` and their uniqueness constraint remain intact. Two
small tables separate content identity from sightings:

- `observation_occurrences`: product, canonical observation, collector run and
  the Discovery's actual observation timestamp. One lightweight row per accepted
  product per successful run, including unchanged sightings; no duplicate payload.
- `current_product_observations`: one current content/occurrence pointer per
  product, with its evidence basis. Both pipeline and dashboard current-state
  queries use this pointer. Canonical `observations.observed_at` still means the
  content row's first insertion; `last_sighted_at` exposes the occurrence time.

`record_observation_get_id()` now returns `(content_id, state_changed)`. Its
boolean compares against the immediately previous current state, rather than
whether INSERT added unique content. Content changes with no meaningful diff
still advance the pointer without producing editorial novelty.

`process_run()` commits product touches/removal counters, canonical content,
sightings, current pointers, events, notification enqueue and a successful
persistence receipt together. `collector_runs.persistence_stats_json` records
that receipt separately from terminal run telemetry. A process can die before
terminal telemetry is written without losing the committed persistence receipt.
Exact replay of that run returns its original statistics and performs no writes,
even after newer runs. Replay statistics describe the original committed result,
not newly created events. An uncommitted crash rolls back the entire transition.

Runner supplies the durable `run_id`. Direct callers may omit it for independent
operations, but exact historical-run replay protection requires retaining and
passing that ID. Reusing an already persisted per-product/run sighting with
different content is rejected by the storage primitive.

Chronology events persist `events.transition_occurrence_id`. Their deterministic
dedup key includes that durable sighting ID, so recurring A→B changes remain
distinct while the same transition deduplicates. Existing events/keys are never
rewritten. Legacy events constructed without an occurrence retain the original
dedup algorithm. Removal events use the last successful sighting to distinguish
removal after an identical reappearance; reappearance continues to reactivate
the existing product without claiming NEW_PRODUCT. Classification-only events
retain their existing identity policy.

## Migration and historical uncertainty

Version authority is `compatibility.EXPECTED_SCHEMA_VERSION = 7`, re-exported
as `SCHEMA_VERSION`. Fresh schema and canonical incremental migration agree.
The v6→v7 migration adds the two tables and the two nullable columns. Existing
observation, event, notification and delivery-policy values are preserved.

Migration initializes each pointer to the highest existing unique-content ID,
the old software's last-known interpretation, explicitly labelled
`LEGACY_LAST_UNIQUE_CONTENT`. This is an assumption, **not reconstructed
chronology**. Its occurrence ID and sighting timestamp are NULL. No historical
occurrences or resighting timestamps are fabricated. A product without prior
observations has no pointer until its next accepted sighting.

The next successful sighting compares against that labelled assumption, updates
current state and may produce an ordinary recovery event if it finds complete
specs where the assumed state was incomplete. Its exact historical recovery
time is unknown; detection is current reconciliation, not a backdated event.
Further runs have explicit chronology. A→A produces a sighting and no event.

The migration is forward-only: a v6 binary must refuse v7 as
INCOMPATIBLE_NEWER. A v7 marker missing chronology tables/columns fails PARTIAL.
There is no downgrade procedure. Before a future operator-authorised upgrade,
take and integrity-check a SQLite online backup and validate compatible code
against a copy. This PR performs no production migration or deployment.

## Copied Windows acceptance proof

The accepted recon snapshot was opened read-only and backed up into a new
disposable DB. Source snapshot SHA-256 before and after:
`ba0509c1c66b0fd69b4a1447f307f8eb421db33d4d57a01d1a024c382a4d02a1`.
All existing observations, events and notifications survived migration exactly;
zero historical occurrences were created.

| Captured handset | Assumed incomplete content ID | Current historical complete content ID | New event |
|---|---:|---:|---|
| HMD 105 4G | 46 | 7 | SPECS_BECAME_AVAILABLE |
| HMD 101 | 101 | 2 | SPECS_BECAME_AVAILABLE |
| Nokia 215 4G 2nd Edition | 140 | 34 | SPECS_BECAME_AVAILABLE |

All 44 captured HMD discoveries were reconciled: hash mismatches 37→0;
36 availability recovery events; zero NEW_PRODUCT or identity anomaly events.
Canonical content stayed at 152 rows. Event history grew 41→77, and the
disposable fake outbox grew 1→37 (36 newly derived pending events). A fresh
repeat recorded 44 unchanged sightings and zero events/outbox inserts. Exact
successful-run replay performed no mutation. No delivery was attempted.

Reproduce offline with `scripts/validate_hmd_chronology_copy.py`, passing the
recon v6 `windows-snapshot.db`, captured HMD `result.json`, new disposable
`--output` and new JSON `--report` paths. The script refuses existing outputs,
uses SQLite backup, checks integrity and verifies the input hash is unchanged.
Tests also commit three capture-derived handset histories and their complete
current discoveries; the full snapshot and real notification payload stay out
of Git.

## Delivery ownership remains separate

GUI manual collection deliberately provides no notifier. This behaviour stays
unchanged. Three push-worthy historical Windows events lacking outbox rows are
an observed consequence, not authorisation to enqueue them. Migration does not
replay events, drain/requeue backlog, change activation cutoff or install any
delivery policy. Cloud runtime/delivery ownership remains unverified.
