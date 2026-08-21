import threading
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from feature_phone_clank.dashboard import render, serve
from feature_phone_clank.paths import resolve_data_path
from feature_phone_clank.providers.sqlite import SqliteStore


def test_dashboard_first_render_succeeds_for_fresh_isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    database = resolve_data_path("data/feature_phone_clank.db")
    store = SqliteStore(str(database))
    store.close()
    page = render(database)
    assert "FIELD TEST MODE" in page
    assert "No accepted feature phones" in page
    assert "local development build" in page


def test_dashboard_disables_hmd_collection_in_field_test(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    database = resolve_data_path("data/feature_phone_clank.db")
    page = render(database, controller=object())
    assert "Collect HMD/Nokia now" not in page
    assert "Collection disabled" in page
    assert "/api/local-collection/run" not in page
    assert "External delivery is disabled" in page


def test_dashboard_http_root_dispatches_from_isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    server = serve(port=0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3) as response:
            body = response.read().decode()
        assert response.status == 200
        assert "Feature Phone Clank" in body
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_dashboard_rejects_non_loopback_bind(host):
    with pytest.raises(ValueError, match="must be loopback"):
        serve(host=host, port=0)


def test_dashboard_rejects_unauthenticated_collection_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    server = serve(port=0, controller=object())
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/local-collection/run",
            data=b"", method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urlopen(request, timeout=3)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_macos_build_replaces_the_app_bundle_and_imports_dashboard_statically():
    root = Path(__file__).resolve().parents[1]
    with (root / "native/macos/build.sh").open(encoding="utf-8") as build:
        assert "--windowed" in build.read()
    with (root / "native/macos/launcher.py").open(encoding="utf-8") as launcher:
        assert "from feature_phone_clank.dashboard import serve" in launcher.read()
