from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from global_quant.gate1a.arbiter import GATE_TIME_LIMIT_SECONDS
from global_quant.gate1a.arbiter import NETWORK_EVIDENCE_CASES
from global_quant.gate1a.arbiter import REQUIRED_COMMAND_MINIMUMS
from global_quant.gate1a.arbiter import RESTART_EVIDENCE_CASES
from global_quant.gate1a.arbiter import SOURCE_OBJECT_PATHS
from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS


ROOT = Path(__file__).resolve().parents[1]
STARTED_AT = "2026-08-06T07:00:00+08:00"
REQUIRED_COMMANDS = tuple(REQUIRED_COMMAND_MINIMUMS)
MINIMUM_TESTS = REQUIRED_COMMAND_MINIMUMS


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_output(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", *args],
        cwd=repo,
        stderr=subprocess.DEVNULL,
    )


def git(*args: str) -> str:
    return git_output(ROOT, *args).decode().strip()


def resolve_tested_commit(repo: Path, ref: str) -> str:
    try:
        commit = git_output(
            repo,
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"tested commit does not exist: {ref}") from exc
    if len(commit) != 40:
        raise ValueError(f"tested commit is not a full SHA: {commit}")
    return commit


def git_source_evidence(
    repo: Path,
    commit: str,
) -> dict[str, dict[str, str]]:
    resolved = resolve_tested_commit(repo, commit)
    evidence = {}
    for name, relative_path in SOURCE_OBJECT_PATHS.items():
        try:
            content = git_output(repo, "show", f"{resolved}:{relative_path}")
            blob_hash = git_output(
                repo,
                "rev-parse",
                f"{resolved}:{relative_path}",
            ).decode().strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(
                f"git show cannot bind {name} at tested commit",
            ) from exc
        evidence[name] = {
            "path": relative_path,
            "blob_hash": blob_hash,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return evidence


def write_manifest_with_checksum(
    output: Path,
    manifest: dict,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    output.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    checksum_path = Path(f"{output}.sha256")
    checksum_path.write_text(
        f"{digest}  {output.name}\n",
        encoding="ascii",
    )
    return checksum_path


def parse_commands(path: Path, evidence_root: Path) -> list[dict]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_name = {record["name"]: record for record in records}
    commands: list[dict] = []
    for name, minimum_tests in REQUIRED_COMMAND_MINIMUMS.items():
        record = by_name.get(name, {})
        commands.append(
            {
                "name": name,
                "exit_code": record.get("exit_code"),
                "log_path": str((evidence_root / f"{name}.log").resolve()),
                "junit_path": str((evidence_root / f"{name}.xml").resolve()),
                "minimum_tests": minimum_tests,
                "started_at": record.get("started_at"),
                "completed_at": record.get("completed_at"),
                "command": record.get("command"),
                "network_controls": record.get("network_controls"),
                "repository": record.get("repository"),
                "branch": record.get("branch"),
                "commit": record.get("commit"),
                "dirty_worktree": record.get("dirty_worktree"),
            },
        )
    return commands


def junit_case_names(path: Path) -> set[str]:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return set()
    return {
        case.attrib.get("name", "")
        for case in root.iter("testcase")
        if not any(
            child.tag in {"failure", "error", "skipped"}
            for child in case
        )
    }


def restart_case_passed(cases: set[str], name: str) -> bool:
    return all(case in cases for case in RESTART_EVIDENCE_CASES[name])


def determinism_run_paths(evidence_root: Path) -> list[Path]:
    return [
        evidence_root
        / "determinism"
        / f"seed-{seed}-rep-{repetition}.json"
        for repetition in range(1, 4)
        for seed in ("1", "20260730")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workbuddy-review")
    args = parser.parse_args()
    evidence_root = Path(args.evidence_root).resolve()
    output = Path(args.output).resolve()

    commands_path = evidence_root / "commands.jsonl"
    scenario_path = evidence_root / "scenario_results.json"
    determinism_path = (
        evidence_root
        / "determinism"
        / "determinism_summary.json"
    )
    tool_versions_path = evidence_root / "tool_versions.json"
    run_paths = determinism_run_paths(evidence_root)
    workbuddy_review_path = (
        Path(args.workbuddy_review).resolve()
        if args.workbuddy_review
        else None
    )
    commands = parse_commands(commands_path, evidence_root)
    network_cases = junit_case_names(evidence_root / "network_matrix.xml")
    crash_cases = junit_case_names(evidence_root / "crash_matrix.xml")
    crash_cases.update(
        junit_case_names(evidence_root / "strategy_callback_matrix.xml"),
    )
    scenario_payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    determinism = json.loads(determinism_path.read_text(encoding="utf-8"))
    tool_versions = json.loads(tool_versions_path.read_text(encoding="utf-8"))

    commit = resolve_tested_commit(ROOT, args.tested_commit)
    head_commit = resolve_tested_commit(ROOT, "HEAD")
    if commit != head_commit:
        raise ValueError("tested commit must equal repository HEAD")
    actual_status = git("status", "--porcelain=v1", "--untracked-files=all")
    if actual_status:
        raise ValueError("repository must be clean when building Gate evidence")
    source_objects = git_source_evidence(ROOT, commit)
    source_paths = [
        ROOT / relative_path
        for relative_path in SOURCE_OBJECT_PATHS.values()
    ]
    completed_at = datetime.now().astimezone()
    started_at = datetime.fromisoformat(STARTED_AT)
    elapsed_seconds = (completed_at - started_at).total_seconds()

    checksum_path = Path(f"{output}.sha256")
    evidence_files = [
        path
        for path in evidence_root.rglob("*")
        if path.is_file() and path not in {output, checksum_path}
    ]
    evidence_files.extend(source_paths)
    if workbuddy_review_path is not None:
        evidence_files.append(workbuddy_review_path)
    evidence_paths = {
        str(path.resolve()): sha256(path)
        for path in sorted(set(evidence_files))
    }

    raw_results = scenario_payload.get("scenario_results", [])
    scenario_results = [
        {"name": item.get("name"), "status": item.get("status")}
        for item in raw_results
        if isinstance(item, dict)
    ]
    if [item["name"] for item in scenario_results] != list(REQUIRED_SCENARIOS):
        scenario_results = [
            {"name": "INVALID_SCENARIO_ORDER", "status": "STOP"},
        ]

    probes = {
        name: "PASS" if case in network_cases else "STOP"
        for name, case in NETWORK_EVIDENCE_CASES.items()
    }
    manifest = {
        "manifest_version": 3,
        "started_at": STARTED_AT,
        "completed_at": completed_at.isoformat(),
        "effective_work_duration": str(completed_at - started_at),
        "effective_work_duration_seconds": elapsed_seconds,
        "time_limit_seconds": GATE_TIME_LIMIT_SECONDS,
        "repository": str(ROOT),
        "branch": git("branch", "--show-current"),
        "commit": commit,
        "dirty_worktree": False,
        "strategy_hash": source_objects["strategy"]["sha256"],
        "state_machine_hash": source_objects["state_machine"]["sha256"],
        "config_hash": source_objects["config"]["sha256"],
        "source_objects": source_objects,
        "required_commands": list(REQUIRED_COMMANDS),
        "test_commands": commands,
        "network_block_status": {
            "universal_network_blocked": all(
                result == "PASS"
                for result in probes.values()
            ),
            "probes": probes,
            "scope": "processes launched by scripts/run_offline.sh",
        },
        "scenario_results": scenario_results,
        "restart_results": [
            {
                "name": name,
                "status": (
                    "PASS"
                    if restart_case_passed(crash_cases, name)
                    else "STOP"
                ),
            }
            for name in RESTART_EVIDENCE_CASES
        ],
        "determinism": determinism,
        "machine_evidence": {
            "command_log_path": str(commands_path.resolve()),
            "scenario_results_path": str(scenario_path.resolve()),
            "determinism_summary_path": str(determinism_path.resolve()),
            "determinism_run_paths": [
                str(path.resolve())
                for path in run_paths
            ],
            "tool_versions_path": str(tool_versions_path.resolve()),
        },
        "unresolved_P0": [],
        "unresolved_P1": [],
        "unresolved_P2": [
            "Nautilus BarDataWrangler emits a pandas chained-assignment warning",
            "Nautilus backtest path emits a Timestamp.utcnow deprecation warning",
            "Gate 1A does not preserve the 149 MiB Nautilus wheel bytes",
        ],
        "evidence_paths": evidence_paths,
        "versions": tool_versions,
    }
    if workbuddy_review_path is not None:
        review = json.loads(workbuddy_review_path.read_text(encoding="utf-8"))
        if not isinstance(review, dict):
            raise ValueError("WorkBuddy review must be a JSON object")
        manifest["workbuddy_review"] = review
        manifest["workbuddy_review_path"] = str(workbuddy_review_path)
    write_manifest_with_checksum(output, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
