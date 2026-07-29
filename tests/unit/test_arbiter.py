from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from global_quant.gate1a.arbiter import GateArbiter


def write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def passing_manifest(tmp_path: Path) -> dict:
    junit = tmp_path / "full.xml"
    junit_hash = write(
        junit,
        '<testsuites tests="48" failures="0" errors="0" skipped="0"></testsuites>',
    )
    log = tmp_path / "full.log"
    log_hash = write(log, "48 passed\n")
    scenario = tmp_path / "scenarios.json"
    scenario_hash = write(scenario, "{}\n")
    restart = tmp_path / "restart.json"
    restart_hash = write(restart, "{}\n")
    network = tmp_path / "network.json"
    network_hash = write(network, "{}\n")
    source = tmp_path / "strategy.py"
    source_hash = write(source, "class Strategy: pass\n")
    state_machine = tmp_path / "coordinator.py"
    state_machine_hash = write(state_machine, "class Coordinator: pass\n")
    config = tmp_path / "protocol.md"
    config_hash = write(config, "frozen\n")

    evidence = {
        str(path): digest
        for path, digest in [
            (junit, junit_hash),
            (log, log_hash),
            (scenario, scenario_hash),
            (restart, restart_hash),
            (network, network_hash),
            (source, source_hash),
            (state_machine, state_machine_hash),
            (config, config_hash),
        ]
    }
    return {
        "started_at": "2026-07-30T06:26:32+08:00",
        "completed_at": "2026-07-30T07:26:32+08:00",
        "effective_work_duration": "1:00:00",
        "repository": "/tmp/global-quant",
        "branch": "codex/nt-gate1a",
        "commit": "a" * 40,
        "dirty_worktree": False,
        "strategy_hash": source_hash,
        "state_machine_hash": state_machine_hash,
        "config_hash": config_hash,
        "required_commands": ["full"],
        "test_commands": [
            {
                "name": "full",
                "exit_code": 0,
                "log_path": str(log),
                "junit_path": str(junit),
                "minimum_tests": 48,
            },
        ],
        "network_block_status": {
            "universal_network_blocked": True,
            "probes": {
                name: "PASS"
                for name in ("parent", "child", "dns", "ipv4", "ipv6")
            },
        },
        "scenario_results": [
            {"name": name, "status": "PASS"}
            for name in (
                "new_order_rejected",
                "submitted_unacknowledged",
                "partial_then_complete",
                "partial_then_cancel",
                "cancel_reject_fill_race",
                "reversal_before_old_close",
                "protection_fill_cancels_sibling",
                "main_close_cancels_protection",
                "duplicate_events",
                "out_of_order_events",
                "unknown_external_event",
                "snapshot_replay_mismatch",
            )
        ],
        "restart_results": [
            {"name": f"crash-{index}", "status": "PASS"}
            for index in range(1, 12)
        ],
        "determinism": {
            "status": "PASS",
            "hash_seeds": ["1", "20260730"],
            "repetitions": 3,
            "ledger_replay_hash": "b" * 64,
        },
        "unresolved_P0": [],
        "unresolved_P1": [],
        "unresolved_P2": [],
        "evidence_paths": evidence,
        "workbuddy_review": {"verdict": "PASS", "P0": 0, "P1": 0},
    }


def test_complete_evidence_produces_pass(tmp_path) -> None:
    verdict = GateArbiter(require_workbuddy=True).decide(passing_manifest(tmp_path))

    assert verdict["verdict"] == "PASS"
    assert verdict["failures"] == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.update(dirty_worktree=True), "dirty worktree"),
        (
            lambda data: data["test_commands"][0].update(exit_code=1),
            "non-zero command",
        ),
        (
            lambda data: data["network_block_status"].update(
                universal_network_blocked=False,
            ),
            "network isolation",
        ),
        (
            lambda data: data["scenario_results"][0].update(status="STOP"),
            "scenario",
        ),
        (
            lambda data: data["restart_results"].pop(),
            "restart matrix",
        ),
        (
            lambda data: data["determinism"].update(repetitions=2),
            "determinism",
        ),
        (
            lambda data: data.update(unresolved_P1=["p1"]),
            "unresolved P1",
        ),
        (
            lambda data: data["workbuddy_review"].update(P1=1),
            "WorkBuddy",
        ),
    ],
)
def test_any_missing_or_failed_required_evidence_stops(
    tmp_path,
    mutation,
    expected,
) -> None:
    manifest = passing_manifest(tmp_path)
    mutation(manifest)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any(expected in failure for failure in verdict["failures"])


def test_skipped_junit_test_is_stop(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    junit = Path(manifest["test_commands"][0]["junit_path"])
    junit.write_text(
        '<testsuites tests="48" failures="0" errors="0" skipped="1"></testsuites>',
        encoding="utf-8",
    )
    manifest["evidence_paths"][str(junit)] = hashlib.sha256(junit.read_bytes()).hexdigest()

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("skipped" in failure for failure in verdict["failures"])


def test_fewer_tests_than_frozen_minimum_is_stop(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    junit = Path(manifest["test_commands"][0]["junit_path"])
    junit.write_text(
        '<testsuites tests="47" failures="0" errors="0" skipped="0"></testsuites>',
        encoding="utf-8",
    )
    manifest["evidence_paths"][str(junit)] = hashlib.sha256(junit.read_bytes()).hexdigest()

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("fewer tests" in failure for failure in verdict["failures"])


def test_checksum_mismatch_is_stop(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    evidence_path = next(iter(manifest["evidence_paths"]))
    Path(evidence_path).write_text("tampered", encoding="utf-8")

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("checksum" in failure for failure in verdict["failures"])


def test_candidate_mode_does_not_require_workbuddy(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    manifest.pop("workbuddy_review")

    verdict = GateArbiter(require_workbuddy=False).decide(manifest)

    assert verdict["verdict"] == "PASS"
