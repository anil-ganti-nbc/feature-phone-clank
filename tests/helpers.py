"""Shared test utilities for Stage 3 pipeline/diff tests."""

from __future__ import annotations

from feature_phone_clank.core.collector_base import BaseCollector
from feature_phone_clank.core.models import Discovery


def make_discovery(
    slug: str, *, model: str = "Test Phone", model_number: str | None = "SKU1",
    fields: dict | None = None, spec_completeness: str = "complete",
    manufacturer: str = "TestCo", source_key: str = "test-scripted",
    region: str = "en_int",
) -> Discovery:
    return Discovery(
        source_key=source_key, product_key=f"{source_key}:{slug}",
        manufacturer=manufacturer, model=model, model_number=model_number,
        region=region, url=f"https://example.test/{slug}",
        fields=fields or {}, spec_completeness=spec_completeness,
    )


class ScriptedCollector(BaseCollector):
    """A collector whose `collect()` returns pre-scripted results, one
    script entry per call — lets a test drive successive crawls
    deterministically without any network. Each entry is
    `(discoveries, classification_log_entries)`; calls past the end of the
    script return `([], [])`."""

    source_key = "test-scripted"
    source_type = "catalogue"

    def __init__(self, script: list[tuple[list[Discovery], list[dict]]]) -> None:
        super().__init__()
        self._script = list(script)
        self._call = 0

    def collect(self) -> list[Discovery]:
        if self._call < len(self._script):
            discoveries, class_entries = self._script[self._call]
        else:
            discoveries, class_entries = [], []
        self._call += 1
        self.classification_log = list(class_entries)
        return list(discoveries)


def classification_entry(slug: str, classification: str, source_key: str = "test-scripted") -> dict:
    return {
        "slug": slug, "url": f"https://example.test/{slug}",
        "classification": classification, "evidence": {"reason": "test"},
    }
