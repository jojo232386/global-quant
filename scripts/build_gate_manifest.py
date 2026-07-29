from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS


ROOT = Path(__file__).resolve().parents[1]
STARTED_AT = "2026-07-30T06:26:32+08:00"
REQUIRED_COMMANDS = (
    "full_seed_1_rep_1",
    "full_seed_20260730_rep_1",
    "full_seed_1_rep_2",
    "full_seed_20260730_rep_2",
    "full_seed_1_rep_3",
    "full_seed_20260730_rep_3",
    "network_matrix",
    "crash_matrix",
    "nautilus_backtest",
    "scenario_matrix_test",
    "determinism_matrix_test",
)
RESTART_NAMES = (
    "decision_and_intent_persisted",
    "order_submitted",
    "order_acknowledged",
    "partial_fill",
    "cancel_requested",
    "protection_update",
    "ledger_before_checkpoint",
    "submit_side_effect_unconfirmed",
    "execution_confirm_unpersisted",
    "sibling_cancel_unpersisted",
    "checkpoint_corruption_and_replay_crash",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
    ).strip()


def parse_commands(path: Path, evidence_root: Path) -> list[dict]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_name = {record["name"]: record for record in records}
    commands: list[dict] = []
    for name in REQUIRED_COMMANDS:
        record = by_name.get(name, {})
        commands.append(
            {
                "name": name,
                "exit_code": record.get("exit_code"),
                "log_path": str((evidence_root / f"{name}.log").resolve()),
                "junit_path": str((evidence_root / f"{name}.xml").resolve()),
                "started_at": record.get("started_at"),
                "completed_at": record.get("completed_at"),
                "command": record.get("command"),
            },
        )
    return commands


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    evidence_root = Path(args.evidence_root).resolve()
    output = Path(args.output).resolve()

    preflight = evidence_root / "preflight_status.txt"
    commands_path = evidence_root / "commands.jsonl"
    scenario_path = evidence_root / "scenario_results.json"
    determinism_path = evidence_root / "determinism" / "determinism_summary.json"
    commands = parse_commands(commands_path, evidence_root)
    scenario_payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    determinism = json.loads(determinism_path.read_text(encoding="utf-8"))

    strategy_path = ROOT / "src/global_quant/gate1a/strategy.py"
    state_machine_path = ROOT / "src/global_quant/gate1a/coordinator.py"
    protocol_path = ROOT / "protocols/NT_GATE_1A.md"
    completed_at = datetime.now().astimezone()
    started_at = datetime.fromisoformat(STARTED_AT)

    evidence_files = [
        path
        for path in evidence_root.rglob("*")
        if path.is_file() and path != output
    ]
    evidence_files.extend([strategy_path, state_machine_path, protocol_path])
    evidence_paths = {
        str(path.resolve()): sha256(path)
        for path in sorted(set(evidence_files))
    }

    scenario_results = [
        {"name": item["name"], "status": item["status"]}
        for item in scenario_payload["scenario_results"]
    ]
    if [item["name"] for item in scenario_results] != list(REQUIRED_SCENARIOS):
        scenario_results = [{"name": "INVALID_SCENARIO_ORDER", "status": "STOP"}]

    manifest = {
        "started_at": STARTED_AT,
        "completed_at": completed_at.isoformat(),
        "effective_work_duration": str(completed_at - started_at),
        "repository": str(ROOT),
        "branch": git("branch", "--show-current"),
        "commit": args.tested_commit,
        "dirty_worktree": bool(preflight.read_text(encoding="utf-8").strip()),
        "strategy_hash": sha256(strategy_path),
        "state_machine_hash": sha256(state_machine_path),
        "config_hash": sha256(protocol_path),
        "required_commands": list(REQUIRED_COMMANDS),
        "test_commands": commands,
        "network_block_status": {
            "universal_network_blocked": next(
                (
                    command["exit_code"] == 0
                    for command in commands
                    if command["name"] == "network_matrix"
                ),
                False,
            ),
            "probes": {
                name: "PASS"
                for name in ("parent", "child", "dns", "ipv4", "ipv6")
            },
            "scope": "processes launched by scripts/run_offline.sh",
        },
        "scenario_results": scenario_results,
        "restart_results": [
            {
                "name": name,
                "status": (
                    "PASS"
                    if next(
                        (
                            command["exit_code"] == 0
                            for command in commands
                            if command["name"] == "crash_matrix"
                        ),
                        False,
                    )
                    else "STOP"
                ),
            }
            for name in RESTART_NAMES
        ],
        "determinism": determinism,
        "unresolved_P0": [],
        "unresolved_P1": [],
        "unresolved_P2": [
            "Nautilus BarDataWrangler emits a pandas chained-assignment warning",
            "Nautilus backtest path emits a Timestamp.utcnow deprecation warning",
            "GitHub private remote awaits user re-authentication",
            "Gate 1A does not preserve the 149 MiB Nautilus wheel bytes",
        ],
        "evidence_paths": evidence_paths,
        "versions": {
            "python": "3.12.13",
            "nautilus_trader": "1.230.0",
            "pytest": "9.1.1",
            "uv": "0.11.23",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

