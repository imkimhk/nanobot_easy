"""Exercise launch wrappers with stub CLIs; never start or stop a real gateway."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("action", ["start", "restart"])
def test_main_wrapper_keeps_personas_disabled_and_targets_main_only(tmp_path, action):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    wrapper = f"{action}-main-gateway.sh"
    shutil.copy2(ROOT / "scripts" / wrapper, scripts / wrapper)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    output = tmp_path / "calls.jsonl"
    recorder = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["SCOPE_TEST_OUTPUT"], "a") as handle:
    handle.write(json.dumps({"args": sys.argv[1:], "config": os.environ.get("NANOBOT_CONFIG"),
        "workspace": os.environ.get("NANOBOT_WORKSPACE"),
        "personas": os.environ.get("NANOBOT_START_PERSONAS")}) + "\\n")
'''
    for path in [tmp_path / "start-nanobot-easy.sh", tmp_path / ".venv/bin/nanobot"]:
        path.write_text(recorder)
        path.chmod(0o755)
    env = os.environ.copy()
    env.update(SCOPE_TEST_OUTPUT=str(output), NANOBOT_START_PERSONAS="1")
    result = subprocess.run(["bash", str(scripts / wrapper)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert all(row["personas"] == "0" for row in rows)
    assert all(row["config"] == str(tmp_path / ".local/config.json") for row in rows)
    assert all(row["workspace"] == str(tmp_path / ".local/workspace") for row in rows)
    assert len(rows) == (2 if action == "restart" else 1)
    if action == "restart":
        assert rows[0]["args"] == ["down", "--config", str(tmp_path / ".local/config.json"),
                                  "--workspace", str(tmp_path / ".local/workspace")]
    assert rows[-1]["args"] == []


@pytest.mark.parametrize("action", ["start", "restart"])
def test_all_wrapper_checks_persona_helper_before_changing_main(tmp_path, action):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    wrapper = f"{action}-all-gateways.sh"
    shutil.copy2(ROOT / "scripts" / wrapper, scripts / wrapper)
    marker = tmp_path / "main-was-called"
    for name in ["start-main-gateway.sh", "restart-main-gateway.sh"]:
        stub = scripts / name
        stub.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        stub.chmod(0o755)
    result = subprocess.run(["bash", str(scripts / wrapper)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Persona helper not found" in result.stderr
    assert not marker.exists()
