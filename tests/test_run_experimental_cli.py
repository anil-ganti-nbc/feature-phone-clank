"""`run-experimental` CLI command tests. Verifies the isolation contract in
cli.py's module docstring for cmd_run_experimental: itel/Lava run against
the --experimental-db path, never --db, and are invisible to config/scope.yaml.
No live network — the registered itel/Lava collectors default to their real
fetchers, so this suite only exercises the argument-parsing/wiring path via
--sources selecting nothing runnable is avoided by using a scope.yaml with
no production collectors and asserting on the "unregistered"/wiring-level
JSON shape, not by actually invoking a network fetch.
"""

from __future__ import annotations

import json

import pytest

from feature_phone_clank.cli import main


def test_run_experimental_rejects_unregistered_source(tmp_path, capsys):
    scope_path = tmp_path / "scope.yaml"
    scope_path.write_text("production_collectors: []\n")
    db_path = tmp_path / "prod.db"
    exp_db_path = tmp_path / "experimental.db"

    rc = main([
        "--db", str(db_path), "--scope", str(scope_path),
        "run-experimental", "--experimental-db", str(exp_db_path),
        "--sources", "not-a-real-collector", "--no-lock",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experimental_db"] == str(exp_db_path)
    assert payload["results"] == [{"source_key": "not-a-real-collector", "status": "unregistered"}]
    # the production --db path must never be created by an experimental run
    assert not db_path.exists()


def test_run_experimental_skips_a_now_production_scoped_source(tmp_path, capsys):
    scope_path = tmp_path / "scope.yaml"
    scope_path.write_text("production_collectors: [hmd-nokia]\n")
    exp_db_path = tmp_path / "experimental.db"

    rc = main([
        "--scope", str(scope_path),
        "run-experimental", "--experimental-db", str(exp_db_path),
        "--sources", "hmd-nokia", "--no-lock",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == [{"source_key": "hmd-nokia", "status": "now_production_scoped"}]
