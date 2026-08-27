"""Phase 1 local-operator dashboard surface: manual run controls (per-
collector + "run all production only") and the QC review endpoint. The
legacy Phase 0 `/api/local-collection/run` contract (tests in
test_field_test_dashboard.py) is untouched by any of this -- these tests
only exercise the new, separate `/operations/*` and `/api/qc/review/*`
routes, which are authorized ONLY for a real LocalCollectionController and
only from a loopback client+Host (mirroring Watch Clank's
local_operator.py). No real network collection ever happens here --
consistent with test_run_experimental_cli.py's own no-live-network
convention, wiring-level behavior is exercised via unregistered/empty-scope
source keys, never a real fetch.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from feature_phone_clank.core.models import ChangeType, AlertLevel, Confidence, Event
from feature_phone_clank.dashboard import render, serve
from feature_phone_clank.local_collection import LocalCollectionController
from feature_phone_clank.providers.qc_store import QcArchiveStore
from tests.conftest import make_discovery


@pytest.fixture
def running_server(tmp_path):
    scope_path = tmp_path / "scope.yaml"
    scope_path.write_text("production_collectors: []\n")
    db_path = tmp_path / "feature_phone_clank.db"
    qc_path = db_path.with_name("feature_phone_clank_qc.db")  # dashboard.serve()'s own derivation
    controller = LocalCollectionController(
        db_path, tmp_path / "prod.lock", scope_path, tmp_path / "overrides.yaml",
        experimental_db=tmp_path / "experimental.db", experimental_lock_path=tmp_path / "exp.lock",
    )
    server = serve(port=0, controller=controller)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server, db_path, qc_path
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _post(server, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    request = Request(f"http://127.0.0.1:{server.server_port}{path}", data=data, method="POST")
    try:
        with urlopen(request, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, {}


def test_legacy_phase0_path_still_unconditionally_403s(running_server):
    server, _, _ = running_server
    status, _ = _post(server, "/api/local-collection/run")
    assert status == 403


def test_run_all_with_empty_scope_reports_no_production_collectors(running_server):
    server, _, _ = running_server
    status, payload = _post(server, "/operations/run-all")
    assert status == 409
    assert payload["error"] == "no_production_collectors"


def test_run_one_unregistered_collector_is_rejected(running_server):
    server, _, _ = running_server
    status, payload = _post(server, "/operations/run/not-a-real-collector")
    assert status == 409
    assert payload["error"] == "unregistered_collector"


def test_run_experimental_without_configured_store_is_rejected(tmp_path):
    scope_path = tmp_path / "scope.yaml"
    scope_path.write_text("production_collectors: []\n")
    controller = LocalCollectionController(
        tmp_path / "db.db", tmp_path / "l.lock", scope_path, tmp_path / "o.yaml",
    )  # no experimental_db/lock configured
    ok, payload = controller.start("itel-india", mode="experimental")
    assert ok is False
    assert payload["error"] == "experimental_store_not_configured"


def test_non_operator_controller_gets_404_on_new_routes(tmp_path):
    import threading as th
    server = serve(port=0, controller=object())
    thread = th.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/operations/run-all", data=b"", method="POST")
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=3)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _seed_event(db_path) -> int:
    from feature_phone_clank.providers.sqlite import SqliteStore

    store = SqliteStore(str(db_path))
    try:
        source_id = store.ensure_source("hmd-nokia", "HMD", "catalogue", "global", "https://example.test", {})
        discovery = make_discovery("hmd-nokia:widget-1", model="Widget 1", price="10")
        product_id = store.create_product(source_id, discovery)
        obs_id, _ = store.record_observation_get_id(product_id, discovery)
        event = Event(
            source_key="hmd-nokia", product_key=discovery.product_key, manufacturer="HMD",
            model="Widget 1", url=discovery.url, event_type=ChangeType.NEW_PRODUCT,
            current_observation_id=obs_id, alert_level=AlertLevel.HIGH, confidence=Confidence.HIGH,
            detected_at=datetime.now(timezone.utc),
        )
        event_id = store.record_event(event)
        assert event_id is not None
        return event_id
    finally:
        store.close()


def test_qc_review_archives_and_removes_from_active_queue(running_server):
    server, db_path, qc_path = running_server
    event_id = _seed_event(db_path)

    from feature_phone_clank.paths import resolve_data_path
    import feature_phone_clank.dashboard as dashboard_mod

    page_before = render(db_path, controller=object(), qc_db=qc_path)
    assert "Widget 1" in page_before

    status, payload = _post(server, f"/api/qc/review/{event_id}", {"decision": "USEFUL", "reason": "confirmed"})
    assert status == 200
    assert payload["review"]["decision"] == "USEFUL"
    assert payload["review"]["product_key"] == "hmd-nokia:widget-1"

    qc_store = QcArchiveStore(str(qc_path))
    try:
        assert event_id in qc_store.reviewed_event_ids()
        recent = qc_store.recent_reviews()
        assert recent[0]["decision"] == "USEFUL"
        assert recent[0]["url"] == "https://example.test/hmd-nokia:widget-1"
    finally:
        qc_store.close()

    page_after = render(db_path, controller=object(), qc_db=qc_path)

    def _section(page, section_id, next_id):
        start = page.index(f'id={section_id}')
        end = page.index(f'id={next_id}', start)
        return page[start:end]

    events_section_before = _section(page_before, "events", "qc-history")
    events_section_after = _section(page_after, "events", "qc-history")
    qc_section_after = _section(page_after, "qc-history", "products")

    assert "Widget 1" in events_section_before
    assert "Widget 1" not in events_section_after  # removed from the default/active queue immediately
    assert "Widget 1" in qc_section_after  # and now shows up, with provenance, in Recently QCed


def test_qc_review_rejects_invalid_decision(running_server):
    server, db_path, _ = running_server
    event_id = _seed_event(db_path)
    status, payload = _post(server, f"/api/qc/review/{event_id}", {"decision": "MAYBE"})
    assert status == 400
    assert payload["error"] == "invalid_decision"


def test_qc_review_unknown_event_404s(running_server):
    server, db_path, _ = running_server
    _seed_event(db_path)
    status, payload = _post(server, "/api/qc/review/999999", {"decision": "USEFUL"})
    assert status == 404


def test_second_qc_decision_corrects_rather_than_duplicates(running_server):
    server, db_path, qc_path = running_server
    event_id = _seed_event(db_path)
    _post(server, f"/api/qc/review/{event_id}", {"decision": "USEFUL"})
    _post(server, f"/api/qc/review/{event_id}", {"decision": "NOT_USEFUL"})
    qc_store = QcArchiveStore(str(qc_path))
    try:
        rows = qc_store.db.execute("SELECT * FROM qc_reviews WHERE event_id=?", (event_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["decision"] == "NOT_USEFUL"
        assert rows[0]["is_corrected"] == 1
    finally:
        qc_store.close()
