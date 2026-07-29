from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUN_OFFLINE = ROOT / "scripts" / "run_offline.sh"
PROBE = ROOT / "tools" / "network_probe.py"


@pytest.mark.parametrize(
    "mode",
    [
        "connect",
        "connect_ex",
        "dns",
        "http",
        "https",
        "websocket",
        "ipv4",
        "ipv6",
        "child",
    ],
)
def test_python_network_paths_fail_nonzero_and_log_stack(tmp_path, mode) -> None:
    violation_log = tmp_path / f"{mode}.jsonl"
    environment = {
        **os.environ,
        "GQ_NETWORK_VIOLATION_LOG": str(violation_log),
    }

    completed = subprocess.run(
        [str(RUN_OFFLINE), str(ROOT / ".venv" / "bin" / "python"), str(PROBE), mode],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode != 0
    assert violation_log.exists()
    records = [
        json.loads(line)
        for line in violation_log.read_text(encoding="utf-8").splitlines()
    ]
    assert records
    assert all(record["call"] and record["stack"] for record in records)


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
def test_os_sandbox_blocks_raw_socket_without_python_guard(family) -> None:
    completed = subprocess.run(
        [
            str(RUN_OFFLINE),
            "--without-python-guard",
            str(ROOT / ".venv" / "bin" / "python"),
            str(PROBE),
            f"os-{family}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0
    assert "os_network_denied=PASS" in completed.stdout

