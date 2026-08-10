"""Tests for the official v1.6 CLI (``scripts/run_gate_1b_v1_6_demo.py``).

Coverage per task section F:

* no arguments / bad arguments never silently succeed;
* runtime/protocol binding mismatch fails closed;
* authorization manifest missing / stale / wrong-ID fails closed;
* review artifact absent -> PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW
  (the intended end state after implementation + self-review);
* review artifact with wrong HEAD or non-zero P0/P1 fails closed;
* a valid review + fake supervised child yields the structured verdict.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from global_quant.gate1b.authorization import create_authorization, write_manifest
from global_quant.gate1b.mutation_runner import MutationRunnerError
from global_quant.gate1b.review_artifact import write_synthetic_artifact

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "run_gate_1b_v1_6_demo.py"

_RUNTIME = "a" * 40
_NONCE = "0123456789abcdef"
_AUTH_ID = "g1b16-0123456789abcdef"
_PROTOCOL = "b" * 40
_TAG_OBJECT = "c" * 40
_SHA = "d" * 64


def _load_cli(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("gate1b_v1_6_cli_under_test", _SCRIPTS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, "gate1b_v1_6_cli_under_test", module)
    spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path, **overrides: object) -> list[str]:
    base = [
        "--evidence-dir", str(tmp_path / "ev"),
        "--runtime-commit", _RUNTIME,
        "--session-nonce", _NONCE,
        "--authorization-id", _AUTH_ID,
        "--protocol-commit", _PROTOCOL,
        "--protocol-tag-object", _TAG_OBJECT,
        "--protocol-sha256", _SHA,
        "--authorization-manifest", str(tmp_path / "auth.json"),
    ]
    for key, value in overrides.items():
        base.append(f"--{key.replace('_', '-')}")
        base.append(str(value))
    return base


def _ok_binding(monkeypatch: pytest.MonkeyPatch, cli) -> None:
    monkeypatch.setattr(
        cli,
        "_verify_runtime_binding",
        lambda *a, **k: {
            "runtime_commit": _RUNTIME,
            "runtime_tree": "t" * 40,
            "protocol_commit": _PROTOCOL,
            "protocol_tag_object": _TAG_OBJECT,
            "protocol_sha256": _SHA,
            "protocol_tag": "nt-gate-1b-v1.6-protocol",
        },
    )


def _write_auth(tmp_path: Path, auth_id: str = _AUTH_ID) -> Path:
    record = create_authorization(
        protocol_commit=_PROTOCOL,
        protocol_tag_object=_TAG_OBJECT,
        protocol_sha256=_SHA,
        runtime_commit=_RUNTIME,
        authorization_id=auth_id,
    )
    return write_manifest(tmp_path / "auth.json", record)


class TestCliArgumentSafety:
    def test_no_arguments_system_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code != 0  # never silent success

    def test_missing_required_argument_system_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        with pytest.raises(SystemExit):
            cli.main(["--evidence-dir", "/tmp/x"])


class TestCliFailClosed:
    def test_runtime_binding_mismatch_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        monkeypatch.setattr(
            cli,
            "_verify_runtime_binding",
            lambda *a, **k: (_ for _ in ()).throw(MutationRunnerError("RUNTIME_COMMIT_MISMATCH")),
        )
        code = cli.main(_args(tmp_path))
        assert code == 1
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert payload["status"] == "STOP"
        assert payload["reason_codes"] == ["RUNTIME_COMMIT_MISMATCH"]

    def test_authorization_manifest_missing_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        code = cli.main(_args(tmp_path))
        assert code == 1
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert payload["status"] == "STOP"
        assert payload["reason_codes"][0] == "AUTHORIZATION_MANIFEST_MISSING"

    def test_authorization_wrong_id_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path, auth_id="g1b16-ffffffffffffffff")
        code = cli.main(_args(tmp_path))
        assert code == 1
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert "AUTHORIZATION_ID_MISMATCH" in payload["reason_codes"][0]

    def test_authorization_consumed_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        from global_quant.gate1b.authorization import mark_consumed, read_manifest

        path = _write_auth(tmp_path)
        mark_consumed(path, read_manifest(path))
        code = cli.main(_args(tmp_path))
        assert code == 1
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert "AUTHORIZATION_ALREADY_CONSUMED" in payload["reason_codes"][0]


class TestCliReviewGating:
    def test_review_absent_stops_ready_for_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path)
        code = cli.main(_args(tmp_path))
        assert code == 0
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert payload["status"] == "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW"
        assert payload["reason_codes"] == ["INDEPENDENT_REVIEW_ARTIFACT_ABSENT"]

    def test_review_wrong_head_stops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path)
        review = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit="f" * 40,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        code = cli.main(_args(tmp_path, review_artifact=review))
        assert code == 1
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert "REVIEW_ARTIFACT_INVALID" in payload["reason_codes"][0]

    def test_review_nonzero_p0_stops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path)
        review = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        import os
        import stat

        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["findings"]["p0"] = 1
        review.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(review, stat.S_IRUSR | stat.S_IWUSR)
        code = cli.main(_args(tmp_path, review_artifact=review))
        assert code == 1
        assert "REVIEW_ARTIFACT_INVALID" in json.loads(
            (tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8")
        )["reason_codes"][0]

    def test_review_valid_dry_run_plumbing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path)
        review = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        code = cli.main([*_args(tmp_path, review_artifact=review), "--dry-run-plumbing"])
        assert code == 0
        payload = json.loads((tmp_path / "ev" / "cli-verdict.json").read_text(encoding="utf-8"))
        assert payload["status"] == "PASS_PLUMBING_VERIFIED_REVIEW_PRESENT"

    def test_review_valid_supervised_child_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli)
        _write_auth(tmp_path)
        review = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        verdict_path = tmp_path / "ev" / "verdict.json"
        verdict_path.parent.mkdir(parents=True, exist_ok=True)
        verdict_path.write_text(
            json.dumps({"status": "PASS_GATE1B_V1_6_DEMO_RUNTIME"}), encoding="utf-8"
        )

        def _fake_supervised(**kwargs):
            return 0, verdict_path

        monkeypatch.setattr(cli, "run_supervised_session", _fake_supervised)
        code = cli.main(_args(tmp_path, review_artifact=review))
        assert code == 0
