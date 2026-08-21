"""Single entry point for running one collector against the store.

Two guardrails live here, both learned from Smartphone Clank's incident
history rather than invented speculatively:

1. Production-scope lock (brief section 8 / user constraint 8): a collector
   not listed in `config/scope.yaml` never touches the production database.
   `run_production_collector` refuses outright; only `run_experimental`
   (an explicitly separate function, never the default CLI path) may run an
   unapproved collector, and only against a caller-supplied throwaway store.

2. Catastrophic zero-result guard (brief section 16): a source that
   previously had an active catalogue and suddenly reports zero products is
   treated as a collector/source-health problem, not as "every product was
   discontinued." The run is recorded as `blocked_zero_result` and nothing
   is written to `products`/`observations`.
"""

from __future__ import annotations

import logging

from .collector_base import BaseCollector
from .models import CollectorRunResult
from .pipeline import compute_classification_transitions, process_run
from .scope import ScopeConfig, is_production

log = logging.getLogger("feature_phone_clank.runner")


class ScopeError(Exception):
    """Raised when a collector outside the production scope is run through
    the production path."""


def run_production_collector(
    collector: BaseCollector, store, scope: ScopeConfig,
    *, manufacturer: str, source_type: str, region: str | None, base_url: str,
    notifier=None,
) -> tuple[CollectorRunResult, dict]:
    if not is_production(collector.source_key, scope):
        raise ScopeError(
            f"collector {collector.source_key!r} is not in config/scope.yaml "
            f"production_collectors; refusing to run it against the production store. "
            f"Use run_experimental() for unapproved collectors."
        )
    return _run(collector, store, manufacturer=manufacturer, source_type=source_type,
                region=region, base_url=base_url, notifier=notifier)


def run_experimental(
    collector: BaseCollector, store,
    *, manufacturer: str, source_type: str, region: str | None, base_url: str,
    notifier=None,
) -> tuple[CollectorRunResult, dict]:
    """Run a collector not (yet) approved for production. Caller is
    responsible for passing a throwaway store (e.g. `SqliteStore(":memory:")`)
    — this function does not check scope, because its entire purpose is to
    let an experimental collector run somewhere that isn't the production
    database."""
    return _run(collector, store, manufacturer=manufacturer, source_type=source_type,
                region=region, base_url=base_url, notifier=notifier)


def _run(
    collector: BaseCollector, store,
    *, manufacturer: str, source_type: str, region: str | None, base_url: str,
    notifier=None,
) -> tuple[CollectorRunResult, dict]:
    source_id = store.ensure_source(
        collector.source_key, manufacturer, source_type, region, base_url, {}
    )
    previous_row = store.last_ok_run(collector.source_key)
    previous_count = previous_row["products_observed"] if previous_row else None

    is_baseline = previous_count is None

    run_id = store.run_started(collector.source_key)
    result, discoveries = collector.run()  # never raises — see BaseCollector.run()

    # Must run BEFORE classification_log is upserted below — it reads the
    # PRIOR classification for each examined slug, which the upsert would
    # otherwise overwrite first.
    classification_transitions = compute_classification_transitions(
        store, collector.source_key, collector
    )

    def _persist_classification_log() -> None:
        # Quarantine/rejection evidence is retained even on a failed or
        # blocked run — it describes candidates the collector *examined*,
        # independent of whether the catalogue-write path was allowed to
        # proceed this time.
        for entry in getattr(collector, "classification_log", []):
            try:
                store.record_classification(
                    collector.source_key, entry["slug"], entry["url"],
                    entry["classification"], entry.get("evidence", {}),
                )
            except Exception:  # noqa: BLE001 — evidence logging must not break a run
                log.warning(
                    "%s: failed to persist classification_log entry %r",
                    collector.source_key, entry.get("slug"), exc_info=True,
                )

    if result.status == "failed":
        _persist_classification_log()
        store.run_finished(
            run_id, "failed", {"discovered": 0}, result.errors,
            products_observed=None, previous_products_observed=previous_count,
        )
        return result, {"status": "failed"}

    _persist_classification_log()
    n = len(discoveries)
    if previous_count is not None and previous_count > 0 and n == 0:
        reason = (
            f"catastrophic zero-result: previous run observed "
            f"{previous_count} product(s), this run observed 0"
        )
        log.warning("%s: %s — blocking persistence", collector.source_key, reason)
        store.run_finished(
            run_id, "blocked_zero_result", {"discovered": 0}, [reason],
            products_observed=0, previous_products_observed=previous_count,
        )
        return result, {"status": "blocked_zero_result", "reason": reason}

    stats = process_run(
        store, collector.source_key, source_id, discoveries,
        classification_transitions, is_baseline,
        notify=notifier.enqueue if notifier is not None else None,
    )
    store.run_finished(
        run_id, "ok", stats, result.errors,
        products_observed=n, previous_products_observed=previous_count,
    )
    return result, stats
