from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS


REQUIRED_NETWORK_PROBES = {"parent", "child", "dns", "ipv4", "ipv6"}
REQUIRED_RESTART_COUNT = 11


class GateArbiter:
    """Fail-closed evaluator for frozen Gate 1A evidence."""

    def __init__(self, *, require_workbuddy: bool) -> None:
        self.require_workbuddy = require_workbuddy

    def decide(self, manifest: dict[str, Any]) -> dict[str, Any]:
        failures: list[str] = []
        self._check_identity(manifest, failures)
        self._check_commands(manifest, failures)
        self._check_network(manifest, failures)
        self._check_scenarios(manifest, failures)
        self._check_restarts(manifest, failures)
        self._check_determinism(manifest, failures)
        self._check_findings(manifest, failures)
        self._check_evidence_hashes(manifest, failures)
        self._check_workbuddy(manifest, failures)

        verdict = "STOP" if failures else "PASS"
        return {
            "verdict": verdict,
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "effective_work_duration": manifest.get("effective_work_duration"),
            "repository": manifest.get("repository"),
            "branch": manifest.get("branch"),
            "commit": manifest.get("commit"),
            "dirty_worktree": manifest.get("dirty_worktree"),
            "strategy_hash": manifest.get("strategy_hash"),
            "state_machine_hash": manifest.get("state_machine_hash"),
            "config_hash": manifest.get("config_hash"),
            "test_commands": manifest.get("test_commands", []),
            "exit_codes": {
                command.get("name"): command.get("exit_code")
                for command in manifest.get("test_commands", [])
            },
            "network_block_status": manifest.get("network_block_status"),
            "scenario_results": manifest.get("scenario_results"),
            "restart_results": manifest.get("restart_results"),
            "ledger_replay_hash": manifest.get("determinism", {}).get(
                "ledger_replay_hash",
            ),
            "unresolved_P0": manifest.get("unresolved_P0"),
            "unresolved_P1": manifest.get("unresolved_P1"),
            "unresolved_P2": manifest.get("unresolved_P2"),
            "evidence_paths": sorted(manifest.get("evidence_paths", {})),
            "workbuddy_review": manifest.get("workbuddy_review"),
            "failures": failures,
        }

    @staticmethod
    def _check_identity(manifest: dict[str, Any], failures: list[str]) -> None:
        required = (
            "started_at",
            "completed_at",
            "effective_work_duration",
            "repository",
            "branch",
            "commit",
            "strategy_hash",
            "state_machine_hash",
            "config_hash",
        )
        for key in required:
            if not manifest.get(key):
                failures.append(f"missing identity field: {key}")
        if manifest.get("dirty_worktree") is not False:
            failures.append("dirty worktree is not allowed")
        commit = str(manifest.get("commit", ""))
        if len(commit) != 40:
            failures.append("commit must be a full 40-character SHA")

    def _check_commands(self, manifest: dict[str, Any], failures: list[str]) -> None:
        required = set(manifest.get("required_commands", []))
        commands = manifest.get("test_commands", [])
        by_name = {command.get("name"): command for command in commands}
        if not required or not required.issubset(by_name):
            failures.append("required command evidence is missing")
        if len(by_name) != len(commands):
            failures.append("duplicate command evidence name")

        for name in sorted(required):
            command = by_name.get(name)
            if command is None:
                continue
            if command.get("exit_code") != 0:
                failures.append(f"non-zero command exit: {name}")
                continue
            log_path = Path(str(command.get("log_path", "")))
            junit_path = Path(str(command.get("junit_path", "")))
            if not log_path.is_file():
                failures.append(f"missing command log: {name}")
            if not junit_path.is_file():
                failures.append(f"missing JUnit evidence: {name}")
                continue
            self._check_junit(name, junit_path, failures)

    @staticmethod
    def _check_junit(name: str, path: Path, failures: list[str]) -> None:
        try:
            root = ElementTree.parse(path).getroot()
        except (ElementTree.ParseError, OSError) as exc:
            failures.append(f"invalid JUnit evidence for {name}: {exc}")
            return
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            suites = [root]
        tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
        failures_count = sum(
            int(suite.attrib.get("failures", 0))
            for suite in suites
        )
        errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
        if tests <= 0:
            failures.append(f"JUnit contains no tests: {name}")
        if failures_count or errors:
            failures.append(f"JUnit failures or errors: {name}")
        if skipped:
            failures.append(f"JUnit contains skipped or xfailed tests: {name}")

    @staticmethod
    def _check_network(manifest: dict[str, Any], failures: list[str]) -> None:
        status = manifest.get("network_block_status", {})
        if status.get("universal_network_blocked") is not True:
            failures.append("network isolation is not universal for gate processes")
        probes = status.get("probes", {})
        if set(probes) != REQUIRED_NETWORK_PROBES:
            failures.append("network isolation probe set is incomplete")
        elif any(result != "PASS" for result in probes.values()):
            failures.append("network isolation probe failed")

    @staticmethod
    def _check_scenarios(manifest: dict[str, Any], failures: list[str]) -> None:
        results = manifest.get("scenario_results", [])
        by_name = {item.get("name"): item for item in results}
        if set(by_name) != set(REQUIRED_SCENARIOS) or len(results) != len(by_name):
            failures.append("scenario matrix is incomplete or duplicated")
            return
        if any(item.get("status") != "PASS" for item in results):
            failures.append("scenario matrix contains a non-PASS result")

    @staticmethod
    def _check_restarts(manifest: dict[str, Any], failures: list[str]) -> None:
        results = manifest.get("restart_results", [])
        names = [item.get("name") for item in results]
        if len(results) != REQUIRED_RESTART_COUNT or len(set(names)) != len(names):
            failures.append("restart matrix must contain 11 unique results")
            return
        if any(item.get("status") != "PASS" for item in results):
            failures.append("restart matrix contains a non-PASS result")

    @staticmethod
    def _check_determinism(manifest: dict[str, Any], failures: list[str]) -> None:
        result = manifest.get("determinism", {})
        if (
            result.get("status") != "PASS"
            or set(result.get("hash_seeds", [])) != {"1", "20260730"}
            or result.get("repetitions") != 3
            or len(str(result.get("ledger_replay_hash", ""))) != 64
        ):
            failures.append("determinism matrix is incomplete")

    @staticmethod
    def _check_findings(manifest: dict[str, Any], failures: list[str]) -> None:
        if manifest.get("unresolved_P0"):
            failures.append("unresolved P0 findings remain")
        if manifest.get("unresolved_P1"):
            failures.append("unresolved P1 findings remain")
        if "unresolved_P2" not in manifest:
            failures.append("unresolved P2 field is missing")

    @staticmethod
    def _check_evidence_hashes(
        manifest: dict[str, Any],
        failures: list[str],
    ) -> None:
        evidence = manifest.get("evidence_paths", {})
        if not evidence:
            failures.append("evidence checksum manifest is empty")
            return
        observed_hashes: set[str] = set()
        for raw_path, expected_hash in evidence.items():
            path = Path(raw_path)
            if not path.is_file():
                failures.append(f"missing evidence file: {path}")
                continue
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            observed_hashes.add(observed)
            if observed != expected_hash:
                failures.append(f"evidence checksum mismatch: {path}")
        for key in ("strategy_hash", "state_machine_hash", "config_hash"):
            value = manifest.get(key)
            if value and value not in observed_hashes:
                failures.append(f"{key} is not backed by evidence checksum")

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
