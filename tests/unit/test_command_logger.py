from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOGGER = ROOT / "scripts" / "run_logged.py"


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

