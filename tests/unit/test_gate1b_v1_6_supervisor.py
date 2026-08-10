"""Tests for the credential-free supervisor (``supervisor.py``).

Coverage per task section F:

* supervisor refuses to start when its own environment holds a credential name;
* a clean child (exit 0 + sanitized pre-exit + PASS lifecycle evidence) yields a
  PASS verdict with a process-exit attestation;
* a child crash (non-zero exit / missing pre-exit / missing or non-PASS
  lifecycle evidence) fails closed to BLOCKED;
* the supervisor never writes a credential and attests ``no_child_remains``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from global_quant.gate1b.supervisor import (
    ChildResult,
    SupervisorError,
    run_supervised_session,
)

_RUNTIME = "a" * 40
_NONCE = "0123456789abcdef"
_AUTH_ID = "g1b16-0123456789abcdef"
_PROTOCOL = "b" * 40
_TAG_OBJECT = "c" * 40
_SHA = "d" * 64

_BINDING = {
    "runtime_commit": _RUNTIME,
    "session_nonce": _NONCE,
    "authorization_id": _AUTH_ID,
    "protocol_commit": _PROTOCOL,
    "protocol_tag_object": _TAG_OBJECT,
    "protocol_sha256": _SHA,
}


def _write_pass_lifecycle(evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "child-pre-exit.json").write_text(
        json.dumps({"status": "child_complete", "credentials_read": False}) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / "mutation-lifecycle.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "production_contacted": False,
                "create_requests": 1,
                "cancel_requests": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _clean_child(evidence_dir: Path) -> ChildResult:
    _write_pass_lifecycle(evidence_dir)
    return ChildResult(returncode=0)


class TestSupervisorBoundary:
    def test_credential_environment_must_be_empty(self, tmp_path: Path) -> None:
        with pytest.raises(SupervisorError) as exc:
            run_supervised_session(
                evidence_dir=tmp_path / "ev",
                binding=_BINDING,
                child_argv=["python", "-c", "pass"],
                supervisor_environ={"BINANCE_DEMO_API_KEY": "x"},
            )
        assert "CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY" in str(exc.value)


class TestVerdicts:
    def test_clean_child_yields_pass(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        code, verdict_path = run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=lambda argv, cwd: _clean_child(evidence_dir),
        )
        assert code == 0
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert verdict["status"] == "PASS_GATE1B_V1_6_DEMO_RUNTIME"
        assert verdict["child_exit_code"] == 0
        assert verdict["child_pre_exit_present"] is True
        assert verdict["production_contacted"] is False

    def test_process_exit_attestation_written(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=lambda argv, cwd: _clean_child(evidence_dir),
        )
        attestation = json.loads(
            (evidence_dir / "process-exit.json").read_text(encoding="utf-8")
        )
        assert attestation["child_exit_code"] == 0
        assert attestation["no_child_remains"] is True
        assert attestation["supervisor_credential_free"] is True

    def test_child_crash_fails_closed(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        code, verdict_path = run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=lambda argv, cwd: ChildResult(returncode=1),
        )
        assert code == 1
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert verdict["status"] == "BLOCKED"
        assert "CHILD_EXIT_NONZERO:1" in verdict["reason_codes"]
        assert "CHILD_PRE_EXIT_MISSING" in verdict["reason_codes"]

    def test_missing_pre_exit_fails_closed_even_with_exit_zero(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        code, verdict_path = run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=lambda argv, cwd: ChildResult(returncode=0),
        )
        assert code == 1
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert verdict["status"] == "BLOCKED"
        assert "CHILD_PRE_EXIT_MISSING" in verdict["reason_codes"]

    def test_non_pass_lifecycle_fails_closed(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "child-pre-exit.json").write_text("{}", encoding="utf-8")
        (evidence_dir / "mutation-lifecycle.json").write_text(
            json.dumps({"status": "STOP", "production_contacted": False}) + "\n",
            encoding="utf-8",
        )

        def _runner(argv, cwd):
            return ChildResult(returncode=0)

        code, verdict_path = run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=_runner,
        )
        assert code == 1
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert "LIFECYCLE_NOT_PASS:STOP" in verdict["reason_codes"]

    def test_production_contacted_fails_closed(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "ev"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "child-pre-exit.json").write_text("{}", encoding="utf-8")
        (evidence_dir / "mutation-lifecycle.json").write_text(
            json.dumps({"status": "PASS", "production_contacted": True}) + "\n",
            encoding="utf-8",
        )

        def _runner(argv, cwd):
            return ChildResult(returncode=0)

        code, verdict_path = run_supervised_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            child_argv=["fake-child"],
            runner=_runner,
        )
        assert code == 1
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert "PRODUCTION_CONTACTED" in verdict["reason_codes"]
