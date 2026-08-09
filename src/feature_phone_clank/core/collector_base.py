"""Collector error isolation (borrowed from Smartphone Clank's
`BaseCollector`, simplified). A collector's `collect()` may raise for a hard
failure (network error, parser exception); `run()` catches it and always
returns a result — a collector exception must never abort run finalisation
or crawl-wide bookkeeping.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from .models import CollectorRunResult, Discovery


class BaseCollector(ABC):
    source_key: str
    source_type: str

    def __init__(self) -> None:
        # A collector MAY append here during collect(): dicts shaped like
        # {"slug", "url", "classification", "evidence"} for candidates it
        # examined but did NOT turn into a Discovery (rejected or
        # quarantined). The runner persists these to `classification_log`
        # after a successful run so quarantine/rejection evidence survives
        # even though no Discovery/product exists for that URL. Optional —
        # a collector with no classification step just leaves this empty.
        self.classification_log: list[dict] = []

    @abstractmethod
    def collect(self) -> list[Discovery]:
        """Return discoveries for this source. Must not raise on a merely
        empty result (an empty catalogue page is a valid outcome); raise
        only on a genuine collection failure (fetch/parse error)."""

    def run(self) -> tuple[CollectorRunResult, list[Discovery]]:
        started = time.monotonic()
        try:
            discoveries = self.collect()
            result = CollectorRunResult(
                source_key=self.source_key,
                status="ok",
                discovered=len(discoveries),
                duration_s=time.monotonic() - started,
            )
            return result, discoveries
        except Exception as exc:  # noqa: BLE001 — a collector failure must never propagate
            result = CollectorRunResult(
                source_key=self.source_key,
                status="failed",
                discovered=0,
                errors=[repr(exc)],
                duration_s=time.monotonic() - started,
            )
            return result, []
