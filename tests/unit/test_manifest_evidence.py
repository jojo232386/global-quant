from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "build_gate_manifest.py"
DECIDE_SCRIPT = ROOT / "scripts" / "decide_gate.py"


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_git_show_source_evidence_binds_existing_commit_and_blobs() -> None:
    builder = load_script(BUILD_SCRIPT, "gate_manifest_builder")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()

    evidence = builder.git_source_evidence(ROOT, commit)

    assert builder.resolve_tested_commit(ROOT, commit) == commit
    for item in evidence.values():
        content = subprocess.check_output(
            ["git", "show", f"{commit}:{item['path']}"],
            cwd=ROOT,
        )
        blob = subprocess.check_output(
            ["git", "rev-parse", f"{commit}:{item['path']}"],
            cwd=ROOT,
            text=True,
        ).strip()
        assert item["sha256"] == hashlib.sha256(content).hexdigest()
        assert item["blob_hash"] == blob


def test_nonexistent_tested_commit_is_rejected() -> None:
    builder = load_script(BUILD_SCRIPT, "gate_manifest_builder_missing_commit")

    with pytest.raises(ValueError, match="tested commit"):
        builder.resolve_tested_commit(ROOT, "a" * 40)


def test_manifest_sidecar_detects_manifest_drift(tmp_path) -> None:
    builder = load_script(BUILD_SCRIPT, "gate_manifest_checksum_writer")
    decider = load_script(DECIDE_SCRIPT, "gate_manifest_checksum_reader")
    manifest_path = tmp_path / "manifest.json"
    payload = {"commit": "a" * 40, "unresolved_P2": []}

    checksum_path = builder.write_manifest_with_checksum(manifest_path, payload)

    expected = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert checksum_path == Path(f"{manifest_path}.sha256")
    assert decider.verify_manifest_checksum(manifest_path, checksum_path) == expected

    manifest_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        decider.verify_manifest_checksum(manifest_path, checksum_path)


def test_verdict_is_written_with_detached_checksum(tmp_path) -> None:
    decider = load_script(DECIDE_SCRIPT, "gate_verdict_checksum_writer")
    verdict_path = tmp_path / "gate_1a_verdict.json"

    checksum_path = decider.write_verdict(
        verdict_path,
        {"verdict": "STOP", "failures": ["test"]},
    )

    assert checksum_path == Path(f"{verdict_path}.sha256")
    assert decider.verify_manifest_checksum(
        verdict_path,
        checksum_path,
    ) == hashlib.sha256(verdict_path.read_bytes()).hexdigest()
