from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from global_quant.gate1a.environment import collect_tool_versions
from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS


GATE_TIME_LIMIT_SECONDS = 12 * 60 * 60
REQUIRED_COMMAND_MINIMUMS = {
    "full_seed_1_rep_1": 150,
    "full_seed_20260730_rep_1": 150,
    "full_seed_1_rep_2": 150,
    "full_seed_20260730_rep_2": 150,
    "full_seed_1_rep_3": 150,
    "full_seed_20260730_rep_3": 150,
    "network_matrix": 11,
    "crash_matrix": 17,
    "nautilus_backtest": 3,
    "scenario_matrix_test": 12,
    "determinism_matrix_test": 1,
    "strategy_callback_matrix": 2,
    "tool_versions": 1,
}
NETWORK_EVIDENCE_CASES = {
    "parent": "test_python_network_paths_fail_nonzero_and_log_stack[connect]",
    "child": "test_python_network_paths_fail_nonzero_and_log_stack[child]",
    "dns": "test_python_network_paths_fail_nonzero_and_log_stack[dns]",
    "ipv4": "test_os_sandbox_blocks_raw_socket_without_python_guard[ipv4]",
    "ipv6": "test_os_sandbox_blocks_raw_socket_without_python_guard[ipv6]",
}
RESTART_EVIDENCE_CASES = {
    name: (
        "test_sigkill_recovery_is_durable_idempotent_and_order_unique"
        f"[{name}]",
    )
    for name in (
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
    )
}
RESTART_EVIDENCE_CASES["checkpoint_corruption_and_replay_crash"] = (
    "test_corrupted_checkpoint_is_fatal_and_never_silently_rebuilt",
    "test_second_crash_during_replay_does_not_change_durable_ledger",
    "test_checkpoint_matches_replay_or_fails_closed",
    "test_reversal_target_survives_restart_during_partial_close",
)
RESTART_EVIDENCE_CASES["real_strategy_fill_crash_recovery"] = (
    "test_real_strategy_fill_survives_sigkill_and_applies_exactly_once",
)
RESTART_EVIDENCE_CASES["real_strategy_unknown_fill"] = (
    "test_real_strategy_unknown_fill_is_durably_fail_closed",
)
SOURCE_OBJECT_PATHS = {
    "strategy": "src/global_quant/gate1a/strategy.py",
    "state_machine": "src/global_quant/gate1a/coordinator.py",
    "ledger": "src/global_quant/gate1a/ledger.py",
    "recovery": "src/global_quant/gate1a/recovery.py",
    "environment_sampler": "src/global_quant/gate1a/environment.py",
    "scenario_runner": "src/global_quant/gate1a/scenarios.py",
    "scenario_oracle": "src/global_quant/gate1a/scenario_oracle.py",
    "scenario_fixture": (
        "src/global_quant/gate1a/fixtures/nt_gate_1a_scenario_oracle_v1.json"
    ),
    "callback_oracle": (
        "src/global_quant/gate1a/fixtures/"
        "nt_gate_1a_strategy_callback_oracle_v2.json"
    ),
    "callback_worker": "tests/helpers/strategy_callback_worker.py",
    "callback_test": "tests/integration/test_strategy_callback_recovery.py",
    "arbiter": "src/global_quant/gate1a/arbiter.py",
    "manifest_builder": "scripts/build_gate_manifest.py",
    "command_logger": "scripts/run_logged.py",
    "offline_launcher": "scripts/run_offline.sh",
    "evidence_runner": "scripts/run_gate_1a_evidence.sh",
    "config": "protocols/NT_GATE_1A_V1_2.md",
}
SOURCE_HASH_FIELDS = {
    "strategy": "strategy_hash",
    "state_machine": "state_machine_hash",
    "config": "config_hash",
}
SCENARIO_FIELDS = {
    "name",
    "status",
    "initial_state",
    "input_events",
    "observed_orders",
    "observed_fills",
    "expected_orders",
    "expected_fills",
    "final_positions",
    "expected_final_positions",
    "final_wallet",
    "expected_final_wallet",
    "protection_state",
    "expected_protection_state",
    "exit_code",
    "expected_exit_code",
    "fail_closed",
    "expected_failure",
    "observed_events",
    "ledger_hash",
    "business_hash",
    "expected_business_hash",
    "oracle_version",
    "validation_errors",
    "error",
}
CANONICAL_SCENARIO_FIELDS = (
    "name",
    "status",
    "initial_state",
    "input_events",
    "observed_orders",
    "observed_fills",
    "expected_orders",
    "expected_fills",
    "final_positions",
    "expected_final_positions",
    "final_wallet",
    "expected_final_wallet",
    "protection_state",
    "expected_protection_state",
    "exit_code",
    "expected_exit_code",
    "fail_closed",
    "expected_failure",
    "observed_events",
    "ledger_hash",
    "business_hash",
    "expected_business_hash",
    "oracle_version",
    "validation_errors",
    "error",
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


class GateArbiter:
    """Fail-closed evaluator for frozen Gate 1A machine evidence."""

    def __init__(self, *, require_workbuddy: bool) -> None:
        self.require_workbuddy = require_workbuddy
        self._commands_by_name: dict[str, dict[str, Any]] = {}
        self._network_status: dict[str, Any] = {}
        self._scenario_results: list[dict[str, str]] = []
        self._restart_results: list[dict[str, str]] = []
        self._ledger_replay_hash: str | None = None
        self._manifest: dict[str, Any] = {}

    def decide(
        self,
        manifest: dict[str, Any],
        *,
        external_failures: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        failures = list(external_failures)
        self._commands_by_name = {}
        self._network_status = {}
        self._scenario_results = []
        self._restart_results = []
        self._ledger_replay_hash = None
        self._manifest = manifest

        self._check_evidence_hashes(manifest, failures)
        self._check_identity(manifest, failures)
        self._check_commands(manifest, failures)
        self._check_versions(manifest, failures)
        self._check_network(failures)
        self._check_scenarios(manifest, failures)
        self._check_restarts(failures)
        self._check_determinism(manifest, failures)
        self._check_findings(manifest, failures)
        self._check_workbuddy(manifest, failures)

        verdict = "STOP" if failures else "PASS"
        return {
            "verdict": verdict,
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "effective_work_duration": manifest.get("effective_work_duration"),
            "effective_work_duration_seconds": manifest.get(
                "effective_work_duration_seconds",
            ),
            "time_limit_seconds": manifest.get("time_limit_seconds"),
            "repository": manifest.get("repository"),
            "branch": manifest.get("branch"),
            "commit": manifest.get("commit"),
            "dirty_worktree": manifest.get("dirty_worktree"),
            "strategy_hash": manifest.get("strategy_hash"),
            "state_machine_hash": manifest.get("state_machine_hash"),
            "config_hash": manifest.get("config_hash"),
            "versions": manifest.get("versions"),
            "source_objects": manifest.get("source_objects"),
            "test_commands": manifest.get("test_commands", []),
            "exit_codes": {
                command.get("name"): command.get("exit_code")
                for command in manifest.get("test_commands", [])
            },
            "network_block_status": self._network_status,
            "scenario_results": self._scenario_results,
            "restart_results": self._restart_results,
            "ledger_replay_hash": self._ledger_replay_hash,
            "unresolved_P0": manifest.get("unresolved_P0"),
            "unresolved_P1": manifest.get("unresolved_P1"),
            "unresolved_P2": manifest.get("unresolved_P2"),
            "evidence_paths": sorted(manifest.get("evidence_paths", {})),
            "workbuddy_review": manifest.get("workbuddy_review"),
            "failures": failures,
        }

    def _check_identity(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        required = (
            "started_at",
            "completed_at",
            "effective_work_duration",
            "effective_work_duration_seconds",
            "time_limit_seconds",
            "repository",
            "branch",
            "commit",
            "strategy_hash",
            "state_machine_hash",
            "config_hash",
            "source_objects",
        )
        for key in required:
            if manifest.get(key) is None or manifest.get(key) == "":
                failures.append(f"missing identity field: {key}")
        if manifest.get("dirty_worktree") is not False:
            failures.append("dirty worktree is not allowed")
        self._check_timebox(manifest, failures)
        self._check_git_source_objects(manifest, failures)

    @staticmethod
    def _check_timebox(
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        try:
            started_at = datetime.fromisoformat(str(manifest["started_at"]))
            completed_at = datetime.fromisoformat(str(manifest["completed_at"]))
        except (KeyError, TypeError, ValueError):
            failures.append("Gate timebox timestamps are invalid")
            return
        if started_at.utcoffset() is None or completed_at.utcoffset() is None:
            failures.append("Gate timebox timestamps must include UTC offsets")
            return
        elapsed = (completed_at - started_at).total_seconds()
        if elapsed < 0:
            failures.append("Gate completion precedes its start")
        if elapsed > GATE_TIME_LIMIT_SECONDS:
            failures.append("Gate exceeded the machine-enforced 12-hour limit")
        if manifest.get("time_limit_seconds") != GATE_TIME_LIMIT_SECONDS:
            failures.append("Gate time limit is not the frozen 12-hour value")
        declared = manifest.get("effective_work_duration_seconds")
        if not isinstance(declared, (int, float)) or abs(declared - elapsed) > 0.001:
            failures.append("effective work duration disagrees with timestamps")

    @staticmethod
    def _git_bytes(repo: Path, *args: str) -> bytes:
        return subprocess.check_output(
            ["git", *args],
            cwd=repo,
            stderr=subprocess.DEVNULL,
        )

    def _check_git_source_objects(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        repository = Path(str(manifest.get("repository", "")))
        commit = str(manifest.get("commit", ""))
        if not repository.is_dir():
            failures.append("tested repository path does not exist")
            return
        if not COMMIT_PATTERN.fullmatch(commit):
            failures.append("commit must be a full 40-character SHA")
            return
        try:
            resolved = self._git_bytes(
                repository,
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
            ).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            failures.append("tested commit does not exist in repository")
            return
        if resolved != commit:
            failures.append("tested commit did not resolve to the declared SHA")
            return

        source_objects = manifest.get("source_objects")
        if not isinstance(source_objects, dict):
            failures.append("source object evidence is missing")
            return
        if set(source_objects) != set(SOURCE_OBJECT_PATHS):
            failures.append("source object evidence set is incomplete")
            return

        for name, expected_path in SOURCE_OBJECT_PATHS.items():
            item = source_objects.get(name)
            if not isinstance(item, dict) or item.get("path") != expected_path:
                failures.append(f"source object path is invalid: {name}")
                continue
            try:
                content = self._git_bytes(
                    repository,
                    "show",
                    f"{commit}:{expected_path}",
                )
                blob_hash = self._git_bytes(
                    repository,
                    "rev-parse",
                    f"{commit}:{expected_path}",
                ).decode().strip()
            except (OSError, subprocess.CalledProcessError):
                failures.append(f"git show could not read frozen source: {name}")
                continue
            content_hash = hashlib.sha256(content).hexdigest()
            hash_field = SOURCE_HASH_FIELDS.get(name)
            if item.get("sha256") != content_hash or (
                hash_field is not None
                and manifest.get(hash_field) != content_hash
            ):
                failures.append(f"git show SHA-256 mismatch: {name}")
            if item.get("blob_hash") != blob_hash:
                failures.append(f"git show blob hash mismatch: {name}")

    def _check_commands(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        required = set(REQUIRED_COMMAND_MINIMUMS)
        if set(manifest.get("required_commands", [])) != required:
            failures.append("frozen required command set is missing or changed")

        commands = manifest.get("test_commands", [])
        if not isinstance(commands, list):
            failures.append("test command manifest is invalid")
            return
        by_name = {
            command.get("name"): command
            for command in commands
            if isinstance(command, dict)
        }
        if set(by_name) != required:
            failures.append("required command evidence is missing or unexpected")
        if len(by_name) != len(commands):
            failures.append("duplicate command evidence name")

        raw_records = self._read_command_records(manifest, failures)
        raw_by_name = {
            record.get("name"): record
            for record in raw_records
            if isinstance(record, dict)
        }
        if len(raw_by_name) != len(raw_records):
            failures.append("duplicate raw command evidence name")
        if not required.issubset(raw_by_name):
            failures.append("raw required command evidence is missing")

        for name, record in raw_by_name.items():
            self._check_network_controls(
                manifest,
                str(name),
                record,
                failures,
            )

        for name, minimum_tests in REQUIRED_COMMAND_MINIMUMS.items():
            command = by_name.get(name)
            record = raw_by_name.get(name)
            if command is None or record is None:
                continue
            self._commands_by_name[name] = command
            comparable = (
                ("exit_code", "exit_code"),
                ("started_at", "started_at"),
                ("completed_at", "completed_at"),
                ("command", "command"),
                ("network_controls", "network_controls"),
                ("repository", "repository"),
                ("branch", "branch"),
                ("commit", "commit"),
                ("dirty_worktree", "dirty_worktree"),
            )
            for command_key, record_key in comparable:
                if command.get(command_key) != record.get(record_key):
                    failures.append(
                        f"command evidence disagrees with raw log: {name}",
                    )
                    break
            if self._resolved(record.get("repository")) != self._resolved(
                manifest.get("repository"),
            ):
                failures.append(f"command repository disagrees: {name}")
            if record.get("branch") != manifest.get("branch"):
                failures.append(f"command branch disagrees: {name}")
            if record.get("commit") != manifest.get("commit"):
                failures.append(f"command tested commit disagrees: {name}")
            if record.get("dirty_worktree") is not False:
                failures.append(f"command ran from dirty worktree: {name}")
            if command.get("minimum_tests") != minimum_tests:
                failures.append(f"frozen JUnit minimum changed: {name}")
            if command.get("exit_code") != 0:
                failures.append(f"non-zero command exit: {name}")

            log_path = Path(str(command.get("log_path", "")))
            junit_path = Path(str(command.get("junit_path", "")))
            if self._resolved(record.get("output_log")) != self._resolved(log_path):
                failures.append(f"command output log path disagrees: {name}")
            if not log_path.is_file():
                failures.append(f"missing command log: {name}")
            if not junit_path.is_file():
                failures.append(f"missing JUnit evidence: {name}")
                continue
            self._check_junit(
                name,
                junit_path,
                failures,
                minimum_tests=minimum_tests,
            )

    @staticmethod
    def _resolved(path: object) -> str:
        if not path:
            return ""
        return str(Path(str(path)).resolve())

    def _read_command_records(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> list[dict[str, Any]]:
        machine = manifest.get("machine_evidence", {})
        path = Path(str(machine.get("command_log_path", "")))
        if not path.is_file():
            failures.append("raw command log machine evidence is missing")
            return []
        records = []
        try:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if line.strip():
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError(f"line {line_number} is not an object")
                    records.append(record)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            failures.append(f"invalid raw command log machine evidence: {exc}")
            return []
        return records

    @staticmethod
    def _check_network_controls(
        manifest: dict[str, Any],
        name: str,
        record: dict[str, Any],
        failures: list[str],
    ) -> None:
        controls = record.get("network_controls")
        if not isinstance(controls, dict):
            failures.append(f"macOS sandbox evidence missing for command: {name}")
            failures.append(f"Python guard evidence missing for command: {name}")
            return
        sandbox = controls.get("macos_sandbox")
        guard = controls.get("python_guard")
        if not isinstance(sandbox, dict) or sandbox.get("status") != "ENABLED":
            failures.append(f"macOS sandbox evidence missing for command: {name}")
        command = record.get("command")
        command = command if isinstance(command, list) else []
        expected_launcher = str(
            (
                Path(str(manifest.get("repository", "")))
                / "scripts"
                / "run_offline.sh"
            ).resolve(),
        )
        launcher = sandbox.get("launcher") if isinstance(sandbox, dict) else None
        if (
            GateArbiter._resolved(launcher) != expected_launcher
            or not command
            or GateArbiter._resolved(command[0]) != expected_launcher
        ):
            failures.append(f"macOS sandbox launcher mismatch for command: {name}")

        if not isinstance(guard, dict):
            failures.append(f"Python guard evidence missing for command: {name}")
            return
        guard_status = guard.get("status")
        explicit_no_guard = len(command) > 1 and command[1] == (
            "--without-python-guard"
        )
        if guard_status == "ENABLED" and not explicit_no_guard:
            return
        if (
            guard_status == "DISABLED_FOR_OS_PROBE"
            and explicit_no_guard
            and "probe" in name
        ):
            return
        if guard_status == "DISABLED_FOR_OS_PROBE":
            failures.append(
                f"no-guard probe evidence lacks explicit probe command: {name}",
            )
        else:
            failures.append(f"Python guard evidence missing for command: {name}")

    @staticmethod
    def _junit_data(
        path: Path,
    ) -> tuple[int, int, int, int, set[str]]:
        root = ElementTree.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(
            root.findall("testsuite"),
        )
        if not suites:
            suites = [root]
        tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
        failures_count = sum(
            int(suite.attrib.get("failures", 0))
            for suite in suites
        )
        errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
        passing_cases = {
            case.attrib.get("name", "")
            for case in root.iter("testcase")
            if not any(
                child.tag in {"failure", "error", "skipped"}
                for child in case
            )
        }
        return tests, failures_count, errors, skipped, passing_cases

    @classmethod
    def _check_junit(
        cls,
        name: str,
        path: Path,
        failures: list[str],
        *,
        minimum_tests: int,
    ) -> None:
        try:
            tests, failures_count, errors, skipped, _ = cls._junit_data(path)
        except (ElementTree.ParseError, OSError, TypeError, ValueError) as exc:
            failures.append(f"invalid JUnit evidence for {name}: {exc}")
            return
        if tests <= 0:
            failures.append(f"JUnit contains no tests: {name}")
        elif tests < minimum_tests:
            failures.append(
                f"JUnit contains fewer tests than frozen minimum: {name} "
                f"({tests} < {minimum_tests})",
            )
        if failures_count or errors:
            failures.append(f"JUnit failures or errors: {name}")
        if skipped:
            failures.append(f"JUnit contains skipped or xfailed tests: {name}")

    def _passing_cases(
        self,
        command_name: str,
        failures: list[str],
    ) -> set[str]:
        command = self._commands_by_name.get(command_name)
        if command is None:
            return set()
        try:
            return self._junit_data(Path(command["junit_path"]))[4]
        except (ElementTree.ParseError, OSError, KeyError, TypeError, ValueError):
            return set()

    def _check_network(self, failures: list[str]) -> None:
        cases = self._passing_cases("network_matrix", failures)
        probes = {}
        for probe, case_name in NETWORK_EVIDENCE_CASES.items():
            passed = case_name in cases
            probes[probe] = "PASS" if passed else "STOP"
            if not passed:
                failures.append(f"missing or failed network probe case: {probe}")
        self._network_status = {
            "universal_network_blocked": all(
                status == "PASS"
                for status in probes.values()
            ),
            "probes": probes,
            "source": "network_matrix JUnit testcase evidence",
        }

    def _check_scenarios(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        machine = manifest.get("machine_evidence", {})
        payload = self._read_json_object(
            Path(str(machine.get("scenario_results_path", ""))),
            "scenario machine evidence",
            failures,
        )
        if payload is None:
            return
        validated = self._validate_scenario_payload(
            payload,
            "scenario machine evidence",
            failures,
        )
        if validated is None:
            return
        results, _ = validated
        self._scenario_results = [
            {"name": item["name"], "status": item["status"]}
            for item in results
        ]

    def _check_restarts(self, failures: list[str]) -> None:
        cases = self._passing_cases("crash_matrix", failures)
        cases.update(
            self._passing_cases("strategy_callback_matrix", failures),
        )
        for name, required_cases in RESTART_EVIDENCE_CASES.items():
            passed = all(case in cases for case in required_cases)
            self._restart_results.append(
                {"name": name, "status": "PASS" if passed else "STOP"},
            )
            if not passed:
                failures.append(f"missing or failed restart evidence: {name}")

    def _check_versions(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        machine = manifest.get("machine_evidence", {})
        sampled = self._read_json_object(
            Path(str(machine.get("tool_versions_path", ""))),
            "tool version evidence",
            failures,
        )
        if sampled is None:
            return
        required = {
            "python",
            "nautilus_trader",
            "pytest",
            "uv",
            "platform",
            "architecture",
        }
        if set(sampled) != required:
            failures.append("tool version evidence set is incomplete")
            return
        for name, item in sampled.items():
            if (
                not isinstance(item, dict)
                or set(item) != {"value", "source"}
                or not isinstance(item.get("value"), str)
                or not item["value"]
                or not isinstance(item.get("source"), str)
                or not item["source"]
            ):
                failures.append(f"tool version evidence is invalid: {name}")
        if manifest.get("versions") != sampled:
            failures.append("manifest versions disagree with sampled evidence")
        try:
            running = collect_tool_versions()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append(f"running environment version sampling failed: {exc}")
            return
        if sampled != running:
            failures.append("tool versions do not match the running environment")

    def _check_determinism(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        machine = manifest.get("machine_evidence", {})
        raw_paths = machine.get("determinism_run_paths", [])
        if not isinstance(raw_paths, list) or len(raw_paths) != 6:
            failures.append("determinism run evidence must contain six files")
            return

        observed_pairs: set[tuple[str, int]] = set()
        canonical_runs: list[list[dict[str, Any]]] = []
        resolved_run_paths: list[str] = []
        for raw_path in raw_paths:
            path = Path(str(raw_path))
            resolved_run_paths.append(str(path.resolve()))
            label = f"determinism run {path.name}"
            payload = self._read_json_object(path, label, failures)
            if payload is None:
                continue
            try:
                pair = (
                    str(payload["python_hash_seed"]),
                    int(payload["repetition"]),
                )
            except (KeyError, TypeError, ValueError):
                failures.append(f"{label} seed or repetition is invalid")
                continue
            observed_pairs.add(pair)
            validated = self._validate_scenario_payload(
                payload,
                label,
                failures,
            )
            if validated is not None:
                _, canonical = validated
                canonical_runs.append(canonical)

        expected_pairs = {
            (seed, repetition)
            for repetition in range(1, 4)
            for seed in ("1", "20260730")
        }
        if observed_pairs != expected_pairs:
            failures.append("determinism run seed/repetition matrix is incomplete")
        if len(canonical_runs) != 6:
            failures.append("determinism run evidence is incomplete")
            return
        if any(run != canonical_runs[0] for run in canonical_runs[1:]):
            failures.append("determinism runs contain divergent machine evidence")
        digest = hashlib.sha256(
            json.dumps(
                canonical_runs[0],
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        self._ledger_replay_hash = digest

        summary_path = Path(str(machine.get("determinism_summary_path", "")))
        summary = self._read_json_object(
            summary_path,
            "determinism summary evidence",
            failures,
        )
        if summary is None:
            return
        summary_paths = [
            str(Path(str(path)).resolve())
            for path in summary.get("run_files", [])
        ]
        if (
            summary.get("status") != "PASS"
            or set(summary.get("hash_seeds", [])) != {"1", "20260730"}
            or summary.get("repetitions") != 3
            or summary.get("independent_processes") != 6
            or summary.get("ledger_replay_hash") != digest
            or summary_paths != resolved_run_paths
        ):
            failures.append(
                "determinism summary disagrees with independent run evidence",
            )

    @staticmethod
    def _read_json_object(
        path: Path,
        label: str,
        failures: list[str],
    ) -> dict[str, Any] | None:
        if not path.is_file():
            failures.append(f"missing {label}: {path}")
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"invalid {label}: {exc}")
            return None
        if not isinstance(payload, dict):
            failures.append(f"invalid {label}: root is not an object")
            return None
        return payload

    @staticmethod
    def _valid_sha256(value: object) -> bool:
        return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None

    def _validate_scenario_payload(
        self,
        payload: dict[str, Any],
        label: str,
        failures: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        results = payload.get("scenario_results")
        if not isinstance(results, list):
            failures.append(f"{label} has no scenario result list")
            return None
        names = [
            item.get("name")
            for item in results
            if isinstance(item, dict)
        ]
        if names != list(REQUIRED_SCENARIOS) or len(names) != len(results):
            failures.append(f"{label} scenario matrix is incomplete or reordered")
            return None

        valid = True
        frozen_oracle = self._load_frozen_scenario_oracle(label, failures)
        if frozen_oracle is None:
            return None
        expected_by_name = frozen_oracle["scenarios"]
        for item in results:
            name = str(item["name"])
            if set(item) != SCENARIO_FIELDS:
                failures.append(f"{label} fields are incomplete: {name}")
                valid = False
                continue
            if (
                item.get("status") != "PASS"
                or item.get("exit_code") != 0
                or item.get("error") is not None
            ):
                failures.append(f"{label} contains failed scenario: {name}")
                valid = False
            if (
                not isinstance(item.get("initial_state"), dict)
                or not item["initial_state"]
                or not isinstance(item.get("input_events"), list)
                or not item["input_events"]
                or not isinstance(item.get("expected_orders"), list)
                or not isinstance(item.get("expected_fills"), list)
                or not isinstance(item.get("final_positions"), dict)
                or not isinstance(item.get("final_wallet"), str)
                or item.get("final_wallet") == "UNKNOWN"
                or not isinstance(item.get("protection_state"), dict)
                or not isinstance(item.get("fail_closed"), bool)
                or not isinstance(item.get("observed_events"), list)
                or not self._valid_sha256(item.get("ledger_hash"))
                or not self._valid_sha256(item.get("business_hash"))
            ):
                failures.append(f"{label} has invalid concrete fields: {name}")
                valid = False
                continue
            expected = expected_by_name.get(name)
            if expected is None:
                failures.append(f"{label} scenario absent from frozen oracle: {name}")
                valid = False
                continue
            oracle_pairs = (
                ("observed_orders", "orders"),
                ("expected_orders", "orders"),
                ("observed_fills", "fills"),
                ("expected_fills", "fills"),
                ("final_positions", "final_positions"),
                ("expected_final_positions", "final_positions"),
                ("final_wallet", "final_wallet"),
                ("expected_final_wallet", "final_wallet"),
                ("protection_state", "protection_state"),
                ("expected_protection_state", "protection_state"),
                ("exit_code", "exit_code"),
                ("expected_exit_code", "exit_code"),
                ("business_hash", "business_hash"),
                ("expected_business_hash", "business_hash"),
            )
            if any(
                item.get(actual_field) != expected.get(expected_field)
                for actual_field, expected_field in oracle_pairs
            ):
                failures.append(f"{label} disagrees with frozen scenario oracle: {name}")
                valid = False
            if (
                item.get("oracle_version") != frozen_oracle.get("oracle_version")
                or item.get("validation_errors") != []
            ):
                failures.append(f"{label} oracle validation is invalid: {name}")
                valid = False
        special_failures = {
            "unknown_external_event": "UnexplainedEventError",
            "snapshot_replay_mismatch": "CheckpointIntegrityError",
        }
        by_name = {item["name"]: item for item in results}
        for name, expected_failure in special_failures.items():
            if by_name[name].get("expected_failure") != expected_failure:
                failures.append(f"{label} lacks expected failure proof: {name}")
                valid = False
        if by_name["unknown_external_event"].get("fail_closed") is not True:
            failures.append(f"{label} lacks fail-closed external-event proof")
            valid = False

        canonical = [
            {
                field: item[field]
                for field in CANONICAL_SCENARIO_FIELDS
            }
            for item in results
        ]
        digest = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        if payload.get("matrix_business_hash") != digest:
            failures.append(f"{label} matrix hash disagrees with concrete fields")
            valid = False
        if payload.get("status") != "PASS":
            failures.append(f"{label} status is not PASS")
            valid = False
        return (results, canonical) if valid else None

    def _load_frozen_scenario_oracle(
        self,
        label: str,
        failures: list[str],
    ) -> dict[str, Any] | None:
        repository = Path(str(self._manifest.get("repository", "")))
        commit = str(self._manifest.get("commit", ""))
        path = SOURCE_OBJECT_PATHS["scenario_fixture"]
        try:
            raw = self._git_bytes(repository, "show", f"{commit}:{path}")
            payload = json.loads(raw)
        except (
            OSError,
            subprocess.CalledProcessError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            failures.append(f"{label} frozen scenario oracle is unreadable: {exc}")
            return None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("oracle_version"), str)
            or not isinstance(payload.get("scenarios"), dict)
            or set(payload["scenarios"]) != set(REQUIRED_SCENARIOS)
        ):
            failures.append(f"{label} frozen scenario oracle schema is invalid")
            return None
        return payload

    @staticmethod
    def _check_findings(manifest: dict[str, Any], failures: list[str]) -> None:
        if manifest.get("unresolved_P0"):
            failures.append("unresolved P0 findings remain")
        if manifest.get("unresolved_P1"):
            failures.append("unresolved P1 findings remain")
        if "unresolved_P2" not in manifest:
            failures.append("unresolved P2 field is missing")

    def _check_evidence_hashes(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        evidence = manifest.get("evidence_paths", {})
        if not isinstance(evidence, dict) or not evidence:
            failures.append("evidence checksum manifest is empty")
            return
        normalized: dict[str, str] = {}
        observed_hashes: set[str] = set()
        for raw_path, expected_hash in evidence.items():
            path = Path(str(raw_path)).resolve()
            normalized_path = str(path)
            if normalized_path in normalized:
                failures.append(f"duplicate normalized evidence path: {path}")
                continue
            normalized[normalized_path] = str(expected_hash)
            if not path.is_file():
                failures.append(f"missing evidence file: {path}")
                continue
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            observed_hashes.add(observed)
            if observed != expected_hash:
                failures.append(f"evidence checksum mismatch: {path}")

        for path in self._referenced_evidence_paths(manifest):
            if self._resolved(path) not in normalized:
                failures.append(f"referenced evidence is not checksum-covered: {path}")
        for key in ("strategy_hash", "state_machine_hash", "config_hash"):
            value = manifest.get(key)
            if value and value not in observed_hashes:
                failures.append(f"{key} is not backed by evidence checksum")

    @staticmethod
    def _referenced_evidence_paths(manifest: dict[str, Any]) -> list[object]:
        paths: list[object] = []
        machine = manifest.get("machine_evidence", {})
        if isinstance(machine, dict):
            for key in (
                "command_log_path",
                "scenario_results_path",
                "determinism_summary_path",
                "tool_versions_path",
            ):
                paths.append(machine.get(key))
            run_paths = machine.get("determinism_run_paths", [])
            if isinstance(run_paths, list):
                paths.extend(run_paths)
        commands = manifest.get("test_commands", [])
        if isinstance(commands, list):
            for command in commands:
                if isinstance(command, dict):
                    paths.extend(
                        [command.get("log_path"), command.get("junit_path")],
                    )
        source_objects = manifest.get("source_objects", {})
        repository = Path(str(manifest.get("repository", "")))
        if isinstance(source_objects, dict):
            for item in source_objects.values():
                if isinstance(item, dict) and item.get("path"):
                    paths.append(repository / str(item["path"]))
        return [path for path in paths if path]

    def _check_workbuddy(
        self,
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        if not self.require_workbuddy:
            return
        review = manifest.get("workbuddy_review")
        if not review:
            failures.append("WorkBuddy review is missing")
            return
        if (
            review.get("verdict") != "PASS"
            or review.get("P0") != 0
            or review.get("P1") != 0
        ):
            failures.append("WorkBuddy review is not PASS with P0=0 and P1=0")
        if review.get("tested_commit") != manifest.get("commit"):
            failures.append("WorkBuddy review is for a different tested commit")
        review_path = manifest.get("workbuddy_review_path")
        if not review_path:
            failures.append("WorkBuddy review evidence path is missing")
        elif self._resolved(review_path) not in {
            self._resolved(path)
            for path in manifest.get("evidence_paths", {})
        }:
            failures.append("WorkBuddy review evidence is not checksum-covered")
