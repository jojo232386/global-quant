from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOGGER = ROOT / "scripts" / "run_logged.py"
RUN_OFFLINE = ROOT / "scripts" / "run_offline.sh"


def test_logger_records_command_timestamps_output_and_numeric_exit(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    stdout_log = tmp_path / "success.log"
    completed = subprocess.run(
        [
            sys.executable,
            str(LOGGER),
            "--name",
            "success",
            "--command-log",
            str(command_log),
            "--output-log",
            str(stdout_log),
            "--",
            str(RUN_OFFLINE),
            sys.executable,
            "-c",
            "print('ok')",
        ],
        cwd=ROOT,
        check=False,
    )

    record = json.loads(command_log.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert record["name"] == "success"
    assert record["exit_code"] == 0
    assert record["started_at"].endswith("+00:00")
    assert record["completed_at"].endswith("+00:00")
    assert record["command"][-2:] == ["-c", "print('ok')"]
    assert record["network_controls"] == {
        "macos_sandbox": {
            "status": "ENABLED",
            "launcher": str(RUN_OFFLINE),
        },
        "python_guard": {"status": "ENABLED"},
    }
    assert stdout_log.read_text(encoding="utf-8") == "ok\n"


def test_logger_preserves_nonzero_exit(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    stdout_log = tmp_path / "failure.log"
    completed = subprocess.run(
        [
            sys.executable,
            str(LOGGER),
            "--name",
            "failure",
            "--command-log",
            str(command_log),
            "--output-log",
            str(stdout_log),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(3)",
        ],
        cwd=ROOT,
        check=False,
    )

    record = json.loads(command_log.read_text(encoding="utf-8"))
    assert completed.returncode == 3
    assert record["exit_code"] == 3


def test_logger_records_explicit_os_probe_without_python_guard(tmp_path) -> None:
    command_log = tmp_path / "commands.jsonl"
    stdout_log = tmp_path / "probe.log"
    completed = subprocess.run(
        [
            sys.executable,
            str(LOGGER),
            "--name",
            "network_os_probe",
            "--command-log",
            str(command_log),
            "--output-log",
            str(stdout_log),
            "--",
            str(RUN_OFFLINE),
            "--without-python-guard",
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ['GLOBAL_QUANT_MACOS_SANDBOX']); "
                "print(os.environ['GLOBAL_QUANT_PYTHON_GUARD']); "
                "print(os.environ.get('GLOBAL_QUANT_OFFLINE', 'unset'))"
            ),
        ],
        cwd=ROOT,
        check=False,
    )

    record = json.loads(command_log.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert record["network_controls"] == {
        "macos_sandbox": {
            "status": "ENABLED",
            "launcher": str(RUN_OFFLINE),
        },
        "python_guard": {"status": "DISABLED_FOR_OS_PROBE"},
    }
    assert stdout_log.read_text(encoding="utf-8") == (
        "network-deny\n"
        "disabled-for-os-probe\n"
        "unset\n"
    )
