from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from global_quant.gate1a.arbiter import GateArbiter
from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS


REQUIRED_COMMANDS = {
    "full_seed_1_rep_1": 145,
    "full_seed_20260730_rep_1": 145,
    "full_seed_1_rep_2": 145,
    "full_seed_20260730_rep_2": 145,
    "full_seed_1_rep_3": 145,
    "full_seed_20260730_rep_3": 145,
    "network_matrix": 11,
    "crash_matrix": 17,
    "nautilus_backtest": 3,
    "scenario_matrix_test": 12,
    "determinism_matrix_test": 1,
}
NETWORK_CASES = (
    "test_python_network_paths_fail_nonzero_and_log_stack[connect]",
    "test_python_network_paths_fail_nonzero_and_log_stack[child]",
    "test_python_network_paths_fail_nonzero_and_log_stack[dns]",
    "test_os_sandbox_blocks_raw_socket_without_python_guard[ipv4]",
    "test_os_sandbox_blocks_raw_socket_without_python_guard[ipv6]",
)
CRASH_CASES = (
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[decision_and_intent_persisted]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[order_submitted]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[order_acknowledged]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[partial_fill]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[cancel_requested]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[protection_update]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[ledger_before_checkpoint]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[submit_side_effect_unconfirmed]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[execution_confirm_unpersisted]",
    "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
    "[sibling_cancel_unpersisted]",
    "test_corrupted_checkpoint_is_fatal_and_never_silently_rebuilt",
    "test_second_crash_during_replay_does_not_change_durable_ledger",
    "test_checkpoint_matches_replay_or_fails_closed",
    "test_reversal_target_survives_restart_during_partial_close",
)
SOURCE_PATHS = {
    "strategy": "src/global_quant/gate1a/strategy.py",
    "state_machine": "src/global_quant/gate1a/coordinator.py",
    "ledger": "src/global_quant/gate1a/ledger.py",
    "recovery": "src/global_quant/gate1a/recovery.py",
    "scenario_runner": "src/global_quant/gate1a/scenarios.py",
    "scenario_oracle": "src/global_quant/gate1a/scenario_oracle.py",
    "scenario_fixture": (
        "src/global_quant/gate1a/fixtures/nt_gate_1a_scenario_oracle_v1.json"
    ),
    "arbiter": "src/global_quant/gate1a/arbiter.py",
    "manifest_builder": "scripts/build_gate_manifest.py",
    "command_logger": "scripts/run_logged.py",
    "offline_launcher": "scripts/run_offline.sh",
    "evidence_runner": "scripts/run_gate_1a_evidence.sh",
    "config": "protocols/NT_GATE_1A.md",
}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return sha256_bytes(path.read_bytes())


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=repo,
        text=True,
    ).strip()


def make_tested_repository(root: Path) -> tuple[Path, str, dict[str, dict[str, str]]]:
    repo = root / "repository"
    for key, relative_path in SOURCE_PATHS.items():
        if key == "scenario_fixture":
            scenarios = {}
            for name in REQUIRED_SCENARIOS:
                business_hash = hashlib.sha256(
                    f"business:{name}".encode(),
                ).hexdigest()
                scenarios[name] = {
                    "business_hash": business_hash,
                    "exit_code": 0,
                    "fills": [],
                    "final_positions": {},
                    "final_wallet": "10000",
                    "orders": [],
                    "protection_state": {},
                }
            content = json.dumps(
                {
                    "oracle_version": "TEST-ORACLE-1",
                    "protocol_version": "1.1",
                    "scenarios": scenarios,
                },
                sort_keys=True,
            )
        else:
            content = f"{key} frozen content\n"
        write(repo / relative_path, content)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "gate1a@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Gate1A Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "frozen tested source"],
        cwd=repo,
        check=True,
    )
    commit = git(repo, "rev-parse", "HEAD")
    source_objects = {}
    for key, relative_path in SOURCE_PATHS.items():
        content = subprocess.check_output(
            ["git", "show", f"{commit}:{relative_path}"],
            cwd=repo,
        )
        source_objects[key] = {
            "path": relative_path,
            "blob_hash": git(repo, "rev-parse", f"{commit}:{relative_path}"),
            "sha256": sha256_bytes(content),
        }
    return repo, commit, source_objects


def scenario_item(name: str) -> dict:
    expected_failure = None
    if name == "unknown_external_event":
        expected_failure = "UnexplainedEventError"
    elif name == "snapshot_replay_mismatch":
        expected_failure = "CheckpointIntegrityError"
    business_hash = hashlib.sha256(f"business:{name}".encode()).hexdigest()
    return {
        "name": name,
        "status": "PASS",
        "initial_state": {"wallet": "10000"},
        "input_events": ["event"],
        "observed_orders": [],
        "observed_fills": [],
        "expected_orders": [],
        "expected_fills": [],
        "final_positions": {},
        "expected_final_positions": {},
        "final_wallet": "10000",
        "expected_final_wallet": "10000",
        "protection_state": {},
        "expected_protection_state": {},
        "exit_code": 0,
        "expected_exit_code": 0,
        "fail_closed": name == "unknown_external_event",
        "expected_failure": expected_failure,
        "observed_events": ["DECISION_RECORDED"],
        "ledger_hash": hashlib.sha256(f"ledger:{name}".encode()).hexdigest(),
        "business_hash": business_hash,
        "expected_business_hash": business_hash,
        "oracle_version": "TEST-ORACLE-1",
        "validation_errors": [],
        "error": None,
    }


def canonical_scenarios(results: list[dict]) -> list[dict]:
    return [dict(item) for item in results]


def scenario_payload(seed: str, repetition: int) -> dict:
    results = [scenario_item(name) for name in REQUIRED_SCENARIOS]
    canonical = canonical_scenarios(results)
    return {
        "status": "PASS",
        "repetition": repetition,
        "python_hash_seed": seed,
        "matrix_business_hash": sha256_bytes(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        "scenario_results": results,
    }


def junit_xml(test_count: int, cases: tuple[str, ...] = ()) -> str:
    testcases = "".join(
        f'<testcase classname="gate1a" name="{name}" />'
        for name in cases
    )
    return (
        f'<testsuite tests="{test_count}" failures="0" errors="0" skipped="0">'
        f"{testcases}</testsuite>"
    )


def passing_manifest(tmp_path: Path) -> dict:
    repo, commit, source_objects = make_tested_repository(tmp_path)
    evidence_root = tmp_path / "evidence"
    evidence: dict[str, str] = {}
    command_records = []
    commands = []
    launcher = str((repo / "scripts" / "run_offline.sh").resolve())

    for name, minimum_tests in REQUIRED_COMMANDS.items():
        cases: tuple[str, ...] = ()
        if name == "network_matrix":
            cases = NETWORK_CASES
        elif name == "crash_matrix":
            cases = CRASH_CASES
        junit = evidence_root / f"{name}.xml"
        log = evidence_root / f"{name}.log"
        evidence[str(junit)] = write(junit, junit_xml(minimum_tests, cases))
        evidence[str(log)] = write(log, f"{minimum_tests} passed\n")
        record = {
            "name": name,
            "started_at": "2026-07-30T00:30:00+00:00",
            "completed_at": "2026-07-30T00:31:00+00:00",
            "duration_seconds": 60.0,
            "cwd": str(repo),
            "command": [launcher, "python", "-m", "pytest"],
            "exit_code": 0,
            "python_hash_seed": "1",
            "output_log": str(log),
            "network_controls": {
                "macos_sandbox": {
                    "status": "ENABLED",
                    "launcher": launcher,
                },
                "python_guard": {"status": "ENABLED"},
            },
            "repository": str(repo),
            "branch": "gate1a-test",
            "commit": commit,
            "dirty_worktree": False,
        }
        command_records.append(record)
        commands.append(
            {
                "name": name,
                "exit_code": 0,
                "log_path": str(log),
                "junit_path": str(junit),
                "minimum_tests": minimum_tests,
                "started_at": record["started_at"],
                "completed_at": record["completed_at"],
                "command": record["command"],
                "network_controls": record["network_controls"],
                "repository": record["repository"],
                "branch": record["branch"],
                "commit": record["commit"],
                "dirty_worktree": record["dirty_worktree"],
            },
        )

    command_log = evidence_root / "commands.jsonl"
    command_text = "".join(
        json.dumps(record, sort_keys=True) + "\n"
        for record in command_records
    )
    evidence[str(command_log)] = write(command_log, command_text)

    primary_scenarios = evidence_root / "scenario_results.json"
    evidence[str(primary_scenarios)] = write(
        primary_scenarios,
        json.dumps(scenario_payload("1", 1), sort_keys=True) + "\n",
    )

    run_paths = []
    digest = ""
    for repetition in range(1, 4):
        for seed in ("1", "20260730"):
            run_path = (
                evidence_root
                / "determinism"
                / f"seed-{seed}-rep-{repetition}.json"
            )
            payload = scenario_payload(seed, repetition)
            digest = payload["matrix_business_hash"]
            evidence[str(run_path)] = write(
                run_path,
                json.dumps(payload, sort_keys=True) + "\n",
            )
            run_paths.append(str(run_path))
    determinism_summary = evidence_root / "determinism" / "determinism_summary.json"
    summary = {
        "status": "PASS",
        "hash_seeds": ["1", "20260730"],
        "repetitions": 3,
        "independent_processes": 6,
        "ledger_replay_hash": digest,
        "run_files": run_paths,
    }
    evidence[str(determinism_summary)] = write(
        determinism_summary,
        json.dumps(summary, sort_keys=True) + "\n",
    )

    for source in source_objects.values():
        path = repo / source["path"]
        evidence[str(path)] = sha256_bytes(path.read_bytes())
    workbuddy_review = evidence_root / "workbuddy_review.json"
    review_payload = {
        "verdict": "PASS",
        "P0": 0,
        "P1": 0,
        "P2": 0,
        "tested_commit": commit,
    }
    evidence[str(workbuddy_review)] = write(
        workbuddy_review,
        json.dumps(review_payload, sort_keys=True) + "\n",
    )

    return {
        "started_at": "2026-07-30T06:26:32+08:00",
        "completed_at": "2026-07-30T07:26:32+08:00",
        "effective_work_duration": "1:00:00",
        "effective_work_duration_seconds": 3600.0,
        "time_limit_seconds": 43200,
        "repository": str(repo),
        "branch": "gate1a-test",
        "commit": commit,
        "dirty_worktree": False,
        "strategy_hash": source_objects["strategy"]["sha256"],
        "state_machine_hash": source_objects["state_machine"]["sha256"],
        "config_hash": source_objects["config"]["sha256"],
        "source_objects": source_objects,
        "required_commands": list(REQUIRED_COMMANDS),
        "test_commands": commands,
        "network_block_status": {
            "universal_network_blocked": True,
            "probes": {
                name: "PASS"
                for name in ("parent", "child", "dns", "ipv4", "ipv6")
            },
        },
        "scenario_results": [
            {"name": name, "status": "PASS"}
            for name in REQUIRED_SCENARIOS
        ],
        "restart_results": [
            {"name": f"restart-{index}", "status": "PASS"}
            for index in range(1, 12)
        ],
        "determinism": summary,
        "machine_evidence": {
            "command_log_path": str(command_log),
            "scenario_results_path": str(primary_scenarios),
            "determinism_summary_path": str(determinism_summary),
            "determinism_run_paths": run_paths,
        },
        "unresolved_P0": [],
        "unresolved_P1": [],
        "unresolved_P2": [],
        "evidence_paths": evidence,
        "workbuddy_review": review_payload,
        "workbuddy_review_path": str(workbuddy_review),
    }


def refresh_checksum(manifest: dict, path: Path) -> None:
    manifest["evidence_paths"][str(path)] = sha256_bytes(path.read_bytes())


def test_complete_machine_evidence_produces_pass(tmp_path) -> None:
    verdict = GateArbiter(require_workbuddy=True).decide(passing_manifest(tmp_path))

    assert verdict["verdict"] == "PASS"
    assert verdict["failures"] == []
    assert verdict["network_block_status"]["universal_network_blocked"] is True
    assert len(verdict["scenario_results"]) == len(REQUIRED_SCENARIOS)
    assert len(verdict["restart_results"]) == 11


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.update(dirty_worktree=True), "dirty worktree"),
        (
            lambda data: data["test_commands"][0].update(exit_code=1),
            "command evidence disagrees",
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
def test_any_failed_required_evidence_stops(
    tmp_path,
    mutation,
    expected,
) -> None:
    manifest = passing_manifest(tmp_path)
    mutation(manifest)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any(expected in failure for failure in verdict["failures"])


def test_scenario_summary_cannot_hide_failed_machine_evidence(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    path = Path(manifest["machine_evidence"]["scenario_results_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scenario_results"][0]["status"] = "STOP"
    payload["scenario_results"][0]["exit_code"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    refresh_checksum(manifest, path)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("scenario machine evidence" in item for item in verdict["failures"])


def test_forged_pass_and_matrix_hash_cannot_override_frozen_oracle(
    tmp_path,
) -> None:
    manifest = passing_manifest(tmp_path)
    path = Path(manifest["machine_evidence"]["scenario_results_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    item = payload["scenario_results"][0]
    item["observed_orders"] = ["FORGED"]
    item["expected_orders"] = ["FORGED"]
    payload["matrix_business_hash"] = sha256_bytes(
        json.dumps(
            canonical_scenarios(payload["scenario_results"]),
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    refresh_checksum(manifest, path)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("frozen scenario oracle" in item for item in verdict["failures"])


@pytest.mark.parametrize(
    ("command_name", "case_name", "expected"),
    [
        ("network_matrix", NETWORK_CASES[0], "network probe"),
        ("crash_matrix", CRASH_CASES[0], "restart evidence"),
    ],
)
def test_batch_junit_summary_cannot_hide_missing_specific_case(
    tmp_path,
    command_name,
    case_name,
    expected,
) -> None:
    manifest = passing_manifest(tmp_path)
    command = next(
        item
        for item in manifest["test_commands"]
        if item["name"] == command_name
    )
    path = Path(command["junit_path"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f'<testcase classname="gate1a" name="{case_name}" />',
            "",
        ),
        encoding="utf-8",
    )
    refresh_checksum(manifest, path)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any(expected in item for item in verdict["failures"])


def test_determinism_summary_cannot_hide_divergent_run(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    path = Path(manifest["machine_evidence"]["determinism_run_paths"][-1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scenario_results"][0]["business_hash"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    refresh_checksum(manifest, path)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("determinism run" in item for item in verdict["failures"])


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        ("macos_sandbox", "macOS sandbox"),
        ("python_guard", "Python guard"),
    ],
)
def test_every_command_requires_recorded_network_controls(
    tmp_path,
    control,
    expected,
) -> None:
    manifest = passing_manifest(tmp_path)
    command_log = Path(manifest["machine_evidence"]["command_log_path"])
    records = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["network_controls"].pop(control)
    command_log.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    refresh_checksum(manifest, command_log)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any(expected in item for item in verdict["failures"])


def test_each_command_must_be_bound_to_tested_commit(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    command_log = Path(manifest["machine_evidence"]["command_log_path"])
    records = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["commit"] = "f" * 40
    command_log.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    refresh_checksum(manifest, command_log)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("tested commit" in item for item in verdict["failures"])


def test_no_guard_status_requires_explicit_probe_launcher_argument(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    command_log = Path(manifest["machine_evidence"]["command_log_path"])
    records = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
    ]
    records[0]["network_controls"]["python_guard"]["status"] = (
        "DISABLED_FOR_OS_PROBE"
    )
    command_log.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    refresh_checksum(manifest, command_log)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("no-guard probe" in item for item in verdict["failures"])


def test_tested_commit_must_exist(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    manifest["commit"] = "a" * 40

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("tested commit" in item for item in verdict["failures"])


def test_source_hashes_must_match_git_show_blobs(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    manifest["source_objects"]["strategy"]["sha256"] = "c" * 64
    manifest["strategy_hash"] = "c" * 64

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("git show" in item for item in verdict["failures"])


def test_elapsed_time_is_recomputed_and_must_not_exceed_12_hours(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    start = datetime.fromisoformat(manifest["started_at"])
    manifest["completed_at"] = (start + timedelta(hours=12, seconds=1)).isoformat()
    manifest["effective_work_duration"] = "0:00:01"
    manifest["effective_work_duration_seconds"] = 1.0

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("12-hour" in item for item in verdict["failures"])
    assert any("duration" in item for item in verdict["failures"])


def test_referenced_machine_evidence_must_be_checksum_covered(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    scenario_path = manifest["machine_evidence"]["scenario_results_path"]
    manifest["evidence_paths"].pop(scenario_path)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("not checksum-covered" in item for item in verdict["failures"])


def test_skipped_junit_test_is_stop(tmp_path) -> None:
    manifest = passing_manifest(tmp_path)
    junit = Path(manifest["test_commands"][0]["junit_path"])
    junit.write_text(
        '<testsuite tests="145" failures="0" errors="0" skipped="1"></testsuite>',
        encoding="utf-8",
    )
    refresh_checksum(manifest, junit)

    verdict = GateArbiter(require_workbuddy=True).decide(manifest)

    assert verdict["verdict"] == "STOP"
    assert any("skipped" in failure for failure in verdict["failures"])


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
