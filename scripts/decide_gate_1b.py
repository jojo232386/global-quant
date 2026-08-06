#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path

from global_quant.gate1b.arbiter import MANDATORY_RESTARTS
from global_quant.gate1b.arbiter import MANDATORY_SCENARIOS
from global_quant.gate1b.arbiter import decide_gate1b


FROZEN_START = datetime.fromisoformat("2026-08-06T08:20:00+08:00")
PROTOCOL_COMMIT = "35e849d"
PROTOCOL_TAG = "nt-gate-1b-v1.2-protocol"
EXPECTED_ENDPOINTS = frozenset(
    {
        "https://demo-fapi.binance.com",
        "wss://demo-fstream.binance.com",
        "wss://testnet.binancefuture.com/ws-fapi/v1",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--external-blocker", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    evidence_root = args.evidence_root.resolve()
    preflight = _read_json(evidence_root / "missing-credentials" / "preflight.json")
    public_probe = _read_json(evidence_root / "public_probe.json")
    endpoint_status, resolved_endpoints = _endpoint_evidence(
        evidence_root / "logs" / "build-only.log",
    )
    completed = datetime.now(UTC)
    conservative_duration = max(
        0.0,
        (completed - FROZEN_START.astimezone(UTC)).total_seconds(),
    )
    candidate = {
        "external_blockers": [args.external_blocker],
        "engineering_failures": [],
        "scenario_results": {},
        "restart_results": {},
        "endpoint_allowlist_status": endpoint_status,
        "credential_redaction_status": preflight.get("credential_redaction", "FAIL"),
        "final_flat_status": "SKIPPED_NO_CONNECTION",
        "ledger_replay_status": "SKIPPED_NO_CONNECTION",
        "workbuddy_review": "NOT_OBTAINED",
        "unresolved_P0": 0,
        "unresolved_P1": 0,
        "effective_work_seconds": conservative_duration,
    }
    decision = decide_gate1b(candidate)
    git = _git_state()
    payload = {
        **decision,
        "gate": "NT-GATE-1B",
        "protocol_version": "1.2",
        "frozen_start": FROZEN_START.isoformat(),
        "completed_at": completed.isoformat(),
        "effective_work_duration_seconds": round(conservative_duration, 3),
        "duration_basis": "conservative_wall_clock_from_frozen_start",
        "repository": git["repository"],
        "branch": git["branch"],
        "protocol_commit": PROTOCOL_COMMIT,
        "protocol_tag": PROTOCOL_TAG,
        "tested_commit": args.tested_commit,
        "verdict_commit": git["commit"],
        "dirty_worktree": git["dirty_worktree"],
        "software_versions": _software_versions(),
        "source_hashes": _source_hashes(),
        "credential_presence": preflight.get("credential_presence", {}),
        "credential_redaction_status": candidate["credential_redaction_status"],
        "endpoint_allowlist_status": endpoint_status,
        "resolved_endpoints": resolved_endpoints,
        "public_demo_probe": {
            "status": public_probe.get("status"),
            "credential_used": public_probe.get("credential_used"),
            "absolute_time_skew_ms": public_probe.get("absolute_time_skew_ms"),
            "instruments": public_probe.get("instruments", []),
        },
        "account_preflight": preflight,
        "scenario_results": {
            name: "SKIPPED_EXTERNAL_BLOCKER" for name in MANDATORY_SCENARIOS
        },
        "restart_results": {
            name: "SKIPPED_EXTERNAL_BLOCKER" for name in MANDATORY_RESTARTS
        },
        "order_summary": {"submitted": 0, "filled": 0, "canceled": 0},
        "economic_event_summary": {"fills": 0, "fees": 0, "funding": 0},
        "position_summary": {"demo_connection_opened": False},
        "balance_summary": {"demo_account_queried": False},
        "protection_summary": {"submitted": 0, "triggered": 0},
        "ledger_replay_hash": None,
        "final_flat_proof": "NOT_APPLICABLE_NO_DEMO_CONNECTION",
        "test_commands": _read_jsonl(evidence_root / "commands.jsonl"),
        "unresolved_P0": 0,
        "unresolved_P1": 0,
        "unresolved_P2": [
            "WORKBUDDY_REVIEW_NOT_OBTAINED",
            "MANDATORY_DEMO_MATRIX_NOT_RUN",
        ],
        "workbuddy_review": {
            "status": "NOT_OBTAINED",
            "required_for_pass": True,
        },
        "secondary_review": _secondary_review(evidence_root / "qwen_review.txt"),
        "evidence_paths": sorted(
            str(path.relative_to(evidence_root))
            for path in evidence_root.rglob("*")
            if path.is_file() and path.resolve() != args.output.resolve()
        ),
        "action": (
            "DO_NOT_ENTER_GATE_2; retry only under a separately authorized and "
            "frozen gate after Demo-only credentials are available"
        ),
        "scope_statement": (
            "Execution engineering only; no alpha, profitability, or live-readiness claim"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    args.output.write_text(encoded, encoding="utf-8")
    checksum = hashlib.sha256(encoded.encode()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{checksum}  {args.output.name}\n",
        encoding="utf-8",
    )
    print(json.dumps({"verdict": decision["verdict"], "output": str(args.output)}))
    return 0 if decision["verdict"] == "PASS" else 2


def _endpoint_evidence(path: Path) -> tuple[str, list[str]]:
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = "Base url "
        if marker not in line:
            continue
        value = line.rsplit(" ", 1)[-1]
        if value.startswith(("https://", "wss://")):
            found.add(value)
    return ("PASS" if found == EXPECTED_ENDPOINTS else "FAIL"), sorted(found)


def _git_state() -> dict[str, object]:
    repository = _git("rev-parse", "--show-toplevel")
    return {
        "repository": str(Path(repository).resolve()),
        "branch": _git("branch", "--show-current"),
        "commit": _git("rev-parse", "HEAD"),
        "dirty_worktree": bool(_git("status", "--porcelain=v1", "--untracked-files=all")),
    }


def _source_hashes() -> dict[str, str]:
    paths = (
        Path("protocols/NT_GATE_1B_V1_2.md"),
        Path("src/global_quant/gate1a/strategy.py"),
        Path("src/global_quant/gate1a/coordinator.py"),
        Path("src/global_quant/gate1a/ledger.py"),
        Path("src/global_quant/gate1a/recovery.py"),
        Path("src/global_quant/gate1b/config.py"),
        Path("src/global_quant/gate1b/safety.py"),
        Path("src/global_quant/gate1b/runtime.py"),
        Path("src/global_quant/gate1b/demo_preflight.py"),
        Path("src/global_quant/gate1b/runner.py"),
        Path("src/global_quant/gate1b/arbiter.py"),
    )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _software_versions() -> dict[str, str]:
    return {
        "python": _command("uv", "run", "python", "--version"),
        "uv": _command("uv", "--version"),
        "nautilus_trader": _command(
            "uv",
            "run",
            "python",
            "-c",
            "import nautilus_trader; print(nautilus_trader.__version__)",
        ),
    }


def _secondary_review(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"reviewer": "Qwen Code via ACP", "status": "NOT_OBTAINED"}
    return {
        "reviewer": "Qwen Code via ACP",
        "status": "RECORDED",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "path": str(path),
    }


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _read_jsonl(path: Path) -> list[object]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


if __name__ == "__main__":
    raise SystemExit(main())
