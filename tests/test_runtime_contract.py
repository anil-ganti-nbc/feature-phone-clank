from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from feature_phone_clank import runtime_bridge


def test_version_info_has_required_keys():
    info = runtime_bridge.get_version_info()
    required = {
        "clank_id", "clank_version", "runtime_version",
        "runtime_contract_version", "health_contract_version", "release_channel",
    }
    assert required <= info.keys()
    assert info["clank_id"] == "feature-phone-clank"


def test_clank_id_matches_unified_clank_pattern():
    """RuntimeIdentity's clank_id validator requires ^[a-z][a-z0-9-]*$ and
    forbids reserved names — assert both without needing clank_runtime
    importable, since it's optional."""
    import re
    assert re.match(r"^[a-z][a-z0-9-]*$", runtime_bridge.CLANK_ID)
    reserved = {"fleet", "runtime", "desktop", "central", "ingestion"}
    assert runtime_bridge.CLANK_ID not in reserved


def test_identity_is_jsonable_and_has_required_fields():
    identity = runtime_bridge.as_jsonable(runtime_bridge.get_identity())
    for key in ("clank_id", "clank_version", "runtime_version", "release_channel"):
        assert key in identity


def test_health_never_raises_on_missing_database(tmp_path):
    missing_db = tmp_path / "does_not_exist.db"
    payload = runtime_bridge.as_jsonable(runtime_bridge.get_health(missing_db))
    assert "operational_state" in payload
    assert payload["operational_state"] in ("degraded", "failed", "unknown")
    assert "status_reasons" in payload
    assert payload["status_reasons"]  # explains why, doesn't silently claim healthy


def test_health_healthy_after_a_successful_run(store):
    from feature_phone_clank.core.collector_base import BaseCollector
    from feature_phone_clank.core.models import Discovery
    from feature_phone_clank.core.runner import run_experimental

    class OkCollector(BaseCollector):
        source_key = "test-health"
        source_type = "catalogue"

        def collect(self):
            return [Discovery(
                source_key="test-health", product_key="p1",
                manufacturer="TestCo", model="M1", url="https://example.test/p1",
            )]

    run_experimental(
        OkCollector(), store, manufacturer="TestCo", source_type="catalogue",
        region=None, base_url="https://example.test",
    )
    payload = runtime_bridge.as_jsonable(runtime_bridge.get_health(Path(store.db_path)))
    assert payload["operational_state"] == "healthy"
    assert payload["last_successful_run"] is not None


def test_cli_version_identity_health_are_valid_json(tmp_path):
    """End-to-end: the actual CLI entry point, not just the bridge module —
    the machine contract is the CLI's stdout, not an internal function."""
    env_db = tmp_path / "cli_test.db"
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    for cmd in ("version", "identity", "health"):
        proc = subprocess.run(
            [sys.executable, "-m", "feature_phone_clank.cli", "--db", str(env_db), cmd],
            capture_output=True, text=True, cwd=repo_root, env=env,
        )
        assert proc.returncode in (0, 1), f"{cmd} stderr: {proc.stderr}"
        data = json.loads(proc.stdout)
        assert isinstance(data, dict)
