from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import global_quant.gate1b.protocol_readiness as readiness

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_protocol_readiness_module_exists() -> None:
    spec = importlib.util.find_spec("global_quant.gate1b.protocol_readiness")

    assert spec is not None


def test_protocol_readiness_public_api_is_declared() -> None:
    assert isinstance(getattr(readiness, "ProtocolReadinessError", None), type)
    assert callable(getattr(readiness, "run_protocol_readiness", None))
    assert callable(getattr(readiness, "main", None))


def test_protocol_readiness_cli_exposes_only_evidence_directory_input() -> None:
    script = PROJECT_ROOT / "scripts" / "run_gate_1b_v1_5_readiness.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--evidence-dir" in completed.stdout
    assert "api-key" not in completed.stdout.lower()
    assert "secret" not in completed.stdout.lower()


class KeysOnlyEnvironment(Mapping[str, str]):
    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names

    def __getitem__(self, _name: str) -> str:
        raise AssertionError("credential value was read")

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def frozen_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q")
    run_git(root, "config", "user.name", "Protocol Test")
    run_git(root, "config", "user.email", "protocol-test@example.invalid")
    protocol = root / "protocols" / "NT_GATE_1B_V1_5.md"
    protocol.parent.mkdir()
    protocol.write_text("# frozen protocol\n", encoding="utf-8")
    run_git(root, "add", str(protocol.relative_to(root)))
    run_git(root, "commit", "-q", "-m", "freeze protocol")
    run_git(
        root,
        "tag",
        "-a",
        "nt-gate-1b-v1.5-protocol",
        "-m",
        "freeze protocol",
    )
    (root / "implementation.txt").write_text("offline only\n", encoding="utf-8")
    run_git(root, "add", "implementation.txt")
    run_git(root, "commit", "-q", "-m", "add offline implementation")
    return root


def assert_outcome(result: object) -> tuple[int, Path]:
    assert isinstance(result, tuple)
    assert len(result) == 2
    exit_code, evidence_path = result
    assert isinstance(exit_code, int)
    assert isinstance(evidence_path, Path)
    return exit_code, evidence_path


def test_readiness_passes_with_frozen_protocol_and_zero_external_impact(
    tmp_path,
    monkeypatch,
) -> None:
    root = frozen_repo(tmp_path)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("protocol readiness attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)

    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=root,
            evidence_dir=tmp_path / "evidence",
            environ={},
        ),
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["gate"] == "NT-GATE-1B"
    assert payload["protocol_version"] == "1.5"
    assert payload["mode"] == "PROTOCOL_READINESS_ONLY"
    assert payload["protocol_tag"] == "nt-gate-1b-v1.5-protocol"
    assert payload["protocol_commit"] == run_git(
        root,
        "rev-parse",
        "refs/tags/nt-gate-1b-v1.5-protocol^{}",
    )
    assert payload["tested_commit"] == run_git(root, "rev-parse", "HEAD")
    assert payload["credential_environment_empty"] is True
    assert payload["credentials_read"] is False
    assert payload["network_accessed"] is False
    assert payload["authenticated_request_sent"] is False
    assert payload["order_summary"] == {"canceled": 0, "filled": 0, "submitted": 0}
    assert payload["economic_event_summary"] == {"fees": 0, "funding": 0}
    assert payload["position_changes"] == 0
    assert payload["agent_credential_access_allowed"] is False
    assert payload["next_action"] == "WAIT_FOR_EXPLICIT_CREDENTIAL_AUTHORIZATION"
    assert list(evidence_path.parent.glob("*")) == [evidence_path]


def test_credential_environment_stops_without_reading_values_or_running_git(
    tmp_path,
    monkeypatch,
) -> None:
    def forbidden_git(*_args, **_kwargs):
        raise AssertionError("Git ran before credential environment rejection")

    monkeypatch.setattr(readiness, "_run_git", forbidden_git, raising=False)
    environment = KeysOnlyEnvironment(("BINANCE_DEMO_API_KEY",))

    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=tmp_path / "not-a-repository",
            evidence_dir=tmp_path / "evidence",
            environ=environment,
        ),
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert payload["status"] == "STOP"
    assert payload["reason_codes"] == ["CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"]
    assert payload["credentials_read"] is False
    assert payload["network_accessed"] is False


def test_changed_protocol_bytes_fail_closed(tmp_path) -> None:
    root = frozen_repo(tmp_path)
    protocol = root / "protocols" / "NT_GATE_1B_V1_5.md"
    protocol.write_text("# changed after freeze\n", encoding="utf-8")

    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=root,
            evidence_dir=tmp_path / "evidence",
            environ={},
        ),
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert payload["status"] == "STOP"
    assert payload["reason_codes"] == ["PROTOCOL_BYTES_CHANGED_AFTER_FREEZE"]


def test_non_annotated_protocol_tag_fails_closed(tmp_path) -> None:
    root = frozen_repo(tmp_path)
    run_git(root, "tag", "-d", "nt-gate-1b-v1.5-protocol")
    run_git(root, "tag", "nt-gate-1b-v1.5-protocol", "HEAD~1")

    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=root,
            evidence_dir=tmp_path / "evidence",
            environ={},
        ),
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert payload["reason_codes"] == ["PROTOCOL_TAG_NOT_ANNOTATED"]


def test_protocol_tag_must_be_an_ancestor_of_tested_commit(tmp_path) -> None:
    root = frozen_repo(tmp_path)
    tree = run_git(root, "rev-parse", "HEAD^{tree}")
    unrelated = subprocess.run(
        ["git", "-C", str(root), "commit-tree", tree, "-m", "unrelated"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    run_git(root, "switch", "-q", "--detach", unrelated)

    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=root,
            evidence_dir=tmp_path / "evidence",
            environ={},
        ),
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert payload["reason_codes"] == ["PROTOCOL_TAG_NOT_ANCESTOR"]


def test_invalid_git_state_returns_stable_sanitized_reason(tmp_path) -> None:
    exit_code, evidence_path = assert_outcome(
        readiness.run_protocol_readiness(
            project_root=tmp_path / "not-a-repository",
            evidence_dir=tmp_path / "evidence",
            environ={},
        ),
    )
    encoded = evidence_path.read_text(encoding="utf-8")
    payload = json.loads(encoded)

    assert exit_code == 1
    assert payload["reason_codes"] == ["PROTOCOL_GIT_STATE_INVALID"]
    assert payload["credential_environment_empty"] is True
    assert "fatal:" not in encoded
    assert str(tmp_path) not in encoded
