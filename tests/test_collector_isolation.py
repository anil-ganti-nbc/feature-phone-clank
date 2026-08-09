from __future__ import annotations

from feature_phone_clank.core.collector_base import BaseCollector
from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.runner import run_experimental


class WorkingCollector(BaseCollector):
    source_key = "test-working"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        return [
            Discovery(
                source_key=self.source_key, product_key="p1",
                manufacturer="TestCo", model="Model One",
                url="https://example.test/p1",
            )
        ]


class ExplodingCollector(BaseCollector):
    source_key = "test-exploding"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        raise RuntimeError("parser blew up")


class EmptyButValidCollector(BaseCollector):
    """A collector whose collect() legitimately returns nothing — not an
    exception, just an empty catalogue."""

    source_key = "test-empty"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        return []


def test_collector_exception_does_not_propagate():
    result, discoveries = ExplodingCollector().run()
    assert result.status == "failed"
    assert discoveries == []
    assert "parser blew up" in result.errors[0]


def test_collector_success_reports_ok():
    result, discoveries = WorkingCollector().run()
    assert result.status == "ok"
    assert result.discovered == 1
    assert result.errors == []


def test_empty_result_is_not_treated_as_failure():
    result, discoveries = EmptyButValidCollector().run()
    assert result.status == "ok"
    assert result.discovered == 0


def test_run_lifecycle_records_failure_and_cleanup_still_happens(store):
    """A collector exception must not abort run finalisation: the run row
    must reach a terminal state ('failed'), not be left stuck 'running'."""
    result, stats = run_experimental(
        ExplodingCollector(), store,
        manufacturer="TestCo", source_type="catalogue", region=None,
        base_url="https://example.test",
    )
    assert stats["status"] == "failed"
    row = store.db.execute(
        "SELECT * FROM collector_runs WHERE source_key=?", ("test-exploding",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["finished_at"] is not None  # run was finalised, not left dangling

    errors = store.db.execute(
        "SELECT message FROM run_errors WHERE run_id=?", (row["id"],)
    ).fetchall()
    assert any("parser blew up" in e["message"] for e in errors)


def test_run_lifecycle_records_success(store):
    result, stats = run_experimental(
        WorkingCollector(), store,
        manufacturer="TestCo", source_type="catalogue", region=None,
        base_url="https://example.test",
    )
    row = store.db.execute(
        "SELECT * FROM collector_runs WHERE source_key=?", ("test-working",)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["products_observed"] == 1
    assert row["finished_at"] is not None
