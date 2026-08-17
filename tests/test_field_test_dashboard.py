import threading
from urllib.request import urlopen

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


def test_dashboard_exposes_hmd_collection_only_in_field_test(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_PHONE_CLANK_DATA_DIR", str(tmp_path / "field-test"))
    database = resolve_data_path("data/feature_phone_clank.db")
    page = render(database, controller=object())
    assert "Collect HMD/Nokia now" in page
    assert "/api/local-collection/run" in page
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


def test_macos_build_replaces_the_app_bundle_and_imports_dashboard_statically():
    root = __file__.replace("/tests/test_field_test_dashboard.py", "")
    with open(f"{root}/native/macos/build.sh", encoding="utf-8") as build:
        assert "--windowed" in build.read()
    with open(f"{root}/native/macos/launcher.py", encoding="utf-8") as launcher:
        assert "from feature_phone_clank.dashboard import serve" in launcher.read()
