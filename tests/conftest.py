from __future__ import annotations

import pytest

from feature_phone_clank.core.models import Discovery
from feature_phone_clank.providers.sqlite import SqliteStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    s = SqliteStore(str(db_path))
    yield s
    s.close()


def make_discovery(product_key: str, model: str = "Test Phone", **fields) -> Discovery:
    return Discovery(
        source_key="test-source",
        product_key=product_key,
        manufacturer="TestCo",
        model=model,
        url=f"https://example.test/{product_key}",
        fields=fields,
    )
