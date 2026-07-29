from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path


RUN_OFFLINE = (Path(__file__).resolve().parent / "run_offline.sh").resolve()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def network_controls(command: list[str]) -> dict[str, dict[str, str]]:
    launcher = Path(command[0]).resolve() if command else None
    if launcher != RUN_OFFLINE:
        return {
            "macos_sandbox": {"status": "MISSING", "launcher": ""},
            "python_guard": {"status": "MISSING"},
        }
    no_guard_probe = len(command) > 1 and command[1] == "--without-python-guard"
    return {
        "macos_sandbox": {
            "status": "ENABLED",
            "launcher": str(RUN_OFFLINE),
        },
        "python_guard": {
            "status": (
                "DISABLED_FOR_OS_PROBE"
                if no_guard_probe
                else "ENABLED"
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--command-log", required=True)
    parser.add_argument("--output-log", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")

    started_at = utc_now()
    started_monotonic = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=os.environ,
        capture_output=True,
        text=True,
        check=False,
    )
    completed_at = utc_now()
    output = completed.stdout + completed.stderr

    output_path = Path(args.output_log)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")

    record = {
        "name": args.name,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - started_monotonic, 6),
        "cwd": str(Path.cwd()),
        "command": command,
        "exit_code": completed.returncode,
        "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        "output_log": str(output_path.resolve()),
        "network_controls": network_controls(command),
    }
    command_path = Path(args.command_log)
    command_path.parent.mkdir(parents=True, exist_ok=True)
    with command_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    sys.stdout.write(output)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
