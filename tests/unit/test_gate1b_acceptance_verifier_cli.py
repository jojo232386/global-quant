"""Offline CLI tests for trusted-context construction from candidate Git objects."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from global_quant.gate1b.review_artifact import (
    V1_10_PROTOCOL_VERSION,
    build_trusted_expected_context,
    load_active_acceptance_declaration,
    write_synthetic_acceptance_artifact,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "verify_gate_1b_acceptance.py"
_GENERATOR_PATH = _PROJECT_ROOT / "scripts" / "generate_gate_1b_acceptance_artifact.py"
_CANDIDATE = "a" * 40
_PROTOCOL_CONTENT = b"tracked v1.10 protocol\n"
_DECLARATION = (
    json.dumps(
        {
            "artifact_schema_version": "1",
            "manifest_schema_version": 1,
            "pass_verdict": "PASS_GATE1B_V1_10_READ_ONLY_DIAGNOSTICS",
            "protocol_path": "protocols/NT_GATE_1B_V1_10.md",
            "protocol_version": "1.10",
        },
        sort_keys=True,
    )
    + "\n"
).encode()


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gate1b_acceptance_verifier", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator() -> ModuleType:
    scripts_path = str(_GENERATOR_PATH.parent)
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "gate1b_acceptance_generator",
            _GENERATOR_PATH,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts_path)


def _expected_context():
    declaration = load_active_acceptance_declaration(
        _DECLARATION,
        expected_protocol_version=V1_10_PROTOCOL_VERSION,
    )
    return build_trusted_expected_context(
        declaration,
        reviewed_head=_CANDIDATE,
        protocol_content=_PROTOCOL_CONTENT,
    )


def _install_fake_git(
    monkeypatch: pytest.MonkeyPatch, verifier: ModuleType
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        assert check is True
        calls.append(arguments)
        if arguments == ("rev-parse", "--verify", f"{_CANDIDATE}^{{commit}}"):
            output = f"{_CANDIDATE}\n".encode()
        elif arguments == (
            "show",
            f"{_CANDIDATE}:protocols/NT_GATE_1B_ACTIVE_ACCEPTANCE.json",
        ):
            output = _DECLARATION
        elif arguments == (
            "show",
            f"{_CANDIDATE}:protocols/NT_GATE_1B_V1_10.md",
        ):
            output = _PROTOCOL_CONTENT
        else:
            raise AssertionError(f"unexpected Git request: {arguments!r}")
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(verifier, "_git", fake_git)
    return calls


def test_cli_pass_uses_only_explicit_candidate_declaration_and_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _load_verifier()
    calls = _install_fake_git(monkeypatch, verifier)
    artifact = write_synthetic_acceptance_artifact(
        tmp_path / "synthetic-v1.10.json",
        expected=_expected_context(),
    )

    result = verifier.main(
        [
            "--artifact",
            str(artifact),
            "--candidate",
            _CANDIDATE,
            "--expected-protocol-version",
            V1_10_PROTOCOL_VERSION,
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "artifact_schema_version": "1",
        "protocol_identity": _expected_context().protocol_identity,
        "protocol_version": "1.10",
        "result": "PASS",
        "reviewed_head": _CANDIDATE,
    }
    assert calls == [
        ("rev-parse", "--verify", f"{_CANDIDATE}^{{commit}}"),
        ("show", f"{_CANDIDATE}:protocols/NT_GATE_1B_ACTIVE_ACCEPTANCE.json"),
        ("show", f"{_CANDIDATE}:protocols/NT_GATE_1B_V1_10.md"),
    ]
    assert all("tag" not in argument for call in calls for argument in call)


def test_cli_unknown_future_version_rejects_before_artifact_can_select_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _load_verifier()
    calls = _install_fake_git(monkeypatch, verifier)
    artifact = write_synthetic_acceptance_artifact(
        tmp_path / "synthetic-v1.10.json",
        expected=_expected_context(),
    )

    result = verifier.main(
        [
            "--artifact",
            str(artifact),
            "--candidate",
            _CANDIDATE,
            "--expected-protocol-version",
            "1.11",
        ]
    )

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "REJECT"
    assert output["reason"] == "EXPECTED_PROTOCOL_VERSION_UNDECLARED"
    assert calls == [
        ("rev-parse", "--verify", f"{_CANDIDATE}^{{commit}}"),
        ("show", f"{_CANDIDATE}:protocols/NT_GATE_1B_ACTIVE_ACCEPTANCE.json"),
    ]


def test_cli_requires_an_exact_full_candidate_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = _load_verifier()
    monkeypatch.setattr(
        verifier,
        "_git",
        lambda *_arguments, **_kwargs: pytest.fail("Git must not run for an invalid SHA"),
    )
    artifact = write_synthetic_acceptance_artifact(
        tmp_path / "synthetic-v1.10.json",
        expected=_expected_context(),
    )

    result = verifier.main(
        [
            "--artifact",
            str(artifact),
            "--candidate",
            _CANDIDATE[:12],
            "--expected-protocol-version",
            V1_10_PROTOCOL_VERSION,
        ]
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out) == {
        "reason": "EXPECTED_CANDIDATE_INVALID",
        "result": "REJECT",
    }


def test_reviewer_generator_uses_trusted_context_and_owner_only_new_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    generator = _load_generator()
    monkeypatch.setattr(
        generator, "_trusted_expected_context", lambda **_kwargs: _expected_context()
    )
    output = tmp_path / "synthetic-reviewer-artifact.json"
    arguments = [
        "--confirm-independent-review-complete",
        "--output",
        str(output),
        "--candidate",
        _CANDIDATE,
        "--expected-protocol-version",
        V1_10_PROTOCOL_VERSION,
        "--reviewer-identity",
        "synthetic-test-reviewer",
        "--verdict",
        "PASS_GATE1B_V1_10_READ_ONLY_DIAGNOSTICS",
        "--p0",
        "0",
        "--p1",
        "0",
        "--p2",
        "0",
        "--p3",
        "0",
        "--reviewed-at",
        "2026-08-12T00:00:00+00:00",
    ]

    assert generator.main(arguments) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["artifact_schema_version"] == "1"
    assert payload["reviewed_head"] == _CANDIDATE
    assert stat.S_IMODE(os.stat(output).st_mode) == 0o600
    assert json.loads(capsys.readouterr().out)["result"] == "ARTIFACT_CREATED"

    original = output.read_bytes()
    assert generator.main(arguments) == 1
    assert output.read_bytes() == original
    assert json.loads(capsys.readouterr().out) == {
        "reason": "REVIEW_ARTIFACT_OUTPUT_ALREADY_EXISTS",
        "result": "REJECT",
    }


def test_reviewer_generator_refuses_nonzero_p1_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    generator = _load_generator()
    monkeypatch.setattr(
        generator, "_trusted_expected_context", lambda **_kwargs: _expected_context()
    )
    output = tmp_path / "must-not-exist.json"

    assert (
        generator.main(
            [
                "--confirm-independent-review-complete",
                "--output",
                str(output),
                "--candidate",
                _CANDIDATE,
                "--expected-protocol-version",
                V1_10_PROTOCOL_VERSION,
                "--reviewer-identity",
                "synthetic-test-reviewer",
                "--verdict",
                "PASS_GATE1B_V1_10_READ_ONLY_DIAGNOSTICS",
                "--p0",
                "0",
                "--p1",
                "1",
                "--p2",
                "0",
                "--p3",
                "0",
                "--reviewed-at",
                "2026-08-12T00:00:00+00:00",
            ]
        )
        == 1
    )
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "reason": "REVIEW_NOT_FINAL_ACCEPTANCE_ELIGIBLE",
        "result": "REJECT",
    }
