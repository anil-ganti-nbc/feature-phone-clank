from __future__ import annotations

from feature_phone_clank.core.collector_base import BaseCollector
from feature_phone_clank.core.models import Discovery
from feature_phone_clank.core.runner import run_experimental


class ThreeProductCollector(BaseCollector):
    source_key = "test-catalogue"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        return [
            Discovery(
                source_key=self.source_key, product_key=f"p{i}",
                manufacturer="TestCo", model=f"Model {i}",
                url=f"https://example.test/p{i}",
            )
            for i in range(3)
        ]


class ZeroProductCollector(BaseCollector):
    source_key = "test-catalogue"
    source_type = "catalogue"

    def collect(self) -> list[Discovery]:
        return []  # legitimate empty result, not an exception


def _run(collector, store):
    return run_experimental(
        collector, store,
        manufacturer="TestCo", source_type="catalogue", region=None,
        base_url="https://example.test",
    )


def test_first_run_with_zero_products_is_not_catastrophic(store):
    """No prior baseline exists, so zero products on the very first run is
    just an empty catalogue, not a collapse — must persist normally."""
    result, stats = _run(ZeroProductCollector(), store)
    row = store.db.execute(
        "SELECT status FROM collector_runs WHERE source_key='test-catalogue'"
    ).fetchone()
    assert row["status"] == "ok"


def test_catastrophic_zero_after_healthy_catalogue_blocks_persistence(store):
    _run(ThreeProductCollector(), store)
    assert store.active_product_count("test-catalogue") == 3

    result, stats = _run(ZeroProductCollector(), store)

    assert stats["status"] == "blocked_zero_result"
    # The known catalogue must be completely untouched.
    assert store.active_product_count("test-catalogue") == 3
    products = store.db.execute("SELECT * FROM products").fetchall()
    assert len(products) == 3

    run_row = store.db.execute(
        "SELECT * FROM collector_runs WHERE source_key='test-catalogue' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run_row["status"] == "blocked_zero_result"
    assert run_row["previous_products_observed"] == 3
    assert run_row["products_observed"] == 0


def test_healthy_recrawl_after_catastrophic_block_recovers(store):
    """A genuine recovery (catalogue reappears) must still work — the guard
    should only block the anomalous zero-result run, not every run after it."""
    _run(ThreeProductCollector(), store)
    _run(ZeroProductCollector(), store)  # blocked
    result, stats = _run(ThreeProductCollector(), store)  # recovers
    row = store.db.execute(
        "SELECT status FROM collector_runs WHERE source_key='test-catalogue' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "ok"
    assert store.active_product_count("test-catalogue") == 3
