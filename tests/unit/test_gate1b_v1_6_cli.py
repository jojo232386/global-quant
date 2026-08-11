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
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from global_quant.gate1b.authorization import (
    AuthorizationRegistry,
    claim_authorization,
    create_authorization,
    mark_recovery,
    read_manifest,
)
from global_quant.gate1b.review_artifact import write_synthetic_artifact
from global_quant.gate1b.runtime_binding import RuntimeBindingError
from global_quant.gate1b.supervisor import SupervisorError

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
        "--evidence-dir",
        str(_canonical_session_dir(tmp_path)),
        "--runtime-commit",
        _RUNTIME,
        "--session-nonce",
        _NONCE,
        "--authorization-id",
        _AUTH_ID,
        "--protocol-commit",
        _PROTOCOL,
        "--protocol-tag-object",
        _TAG_OBJECT,
        "--protocol-sha256",
        _SHA,
        "--authorization-manifest",
        str(_canonical_manifest(tmp_path)),
    ]
    for key, value in overrides.items():
        base.append(f"--{key.replace('_', '-')}")
        base.append(str(value))
    return base


def _runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "evidence" / "runtime"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _canonical_session_dir(tmp_path: Path) -> Path:
    return _runtime_root(tmp_path) / f"gate1b-v1.6-mutation-{_RUNTIME[:12]}" / _NONCE


def _canonical_manifest(tmp_path: Path) -> Path:
    return _runtime_root(tmp_path) / "gate1b-v1.6-authorizations" / f"{_AUTH_ID}.json"


def _canonical_args(tmp_path: Path, **overrides: object) -> list[str]:
    return _args(tmp_path, **overrides)


def _bind_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    cli: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "_RUNTIME_EVIDENCE_ROOT", _runtime_root(tmp_path))


def _ok_binding(monkeypatch: pytest.MonkeyPatch, cli, tmp_path: Path) -> None:
    _bind_runtime_root(monkeypatch, cli, tmp_path)
    monkeypatch.setattr(
        cli,
        "verify_runtime_binding",
        lambda *a, **k: SimpleNamespace(
            runtime_commit=_RUNTIME,
            runtime_tree="e" * 40,
            branch="codex/test",
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        ),
    )


def _write_auth(tmp_path: Path, auth_id: str = _AUTH_ID) -> Path:
    record = create_authorization(
        protocol_commit=_PROTOCOL,
        protocol_tag_object=_TAG_OBJECT,
        protocol_sha256=_SHA,
        runtime_commit=_RUNTIME,
        authorization_id=auth_id,
    )
    return AuthorizationRegistry(_runtime_root(tmp_path)).create(record)


def _valid_review(tmp_path: Path) -> Path:
    return write_synthetic_artifact(
        tmp_path / "review.json",
        runtime_commit=_RUNTIME,
        protocol_commit=_PROTOCOL,
        protocol_tag_object=_TAG_OBJECT,
        protocol_sha256=_SHA,
    )


def _claim(path: Path):
    return claim_authorization(
        path,
        authorization_id=_AUTH_ID,
        protocol_commit=_PROTOCOL,
        protocol_tag_object=_TAG_OBJECT,
        protocol_sha256=_SHA,
        runtime_commit=_RUNTIME,
    )


def _completion(evidence_dir: Path, *, eligible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        finalized_evidence=SimpleNamespace(verdict_path=evidence_dir / "verdict.json"),
        final_evidence_eligible=eligible,
        evidence_block_reasons=() if eligible else ("INJECTED_BLOCK",),
    )


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

    @pytest.mark.parametrize(
        ("forbidden_flag", "value"),
        (
            ("--child-python", "/tmp/fake-python"),
            ("--key-type", "ed25519"),
            ("--private-key-file", "/tmp/key.pem"),
        ),
    )
    def test_credential_and_child_launch_arguments_are_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        forbidden_flag: str,
        value: str,
    ) -> None:
        cli = _load_cli(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli._parse_args([*_args(tmp_path), forbidden_flag, value])
        assert exc.value.code != 0


class TestCliExecutionOwnership:
    def test_source_has_no_legacy_or_arbitrary_process_ownership(self) -> None:
        source = _SCRIPTS.read_text(encoding="utf-8")
        assert "global_quant.gate1b.mutation_runner" not in source
        assert "run_supervised_session" not in source
        assert "child_argv" not in source
        assert "--child-python" not in source
        assert "--key-type" not in source
        assert "--private-key-file" not in source
        assert "global_quant.gate1b.runtime_binding" in source
        assert "ExecutionSupervisor" in source
        assert ".start_primary(" not in source
        assert ".start_recovery(" not in source
        assert ".project_primary(" not in source
        assert ".project_recovery(" not in source
        assert "PrimaryJournalProjection" not in source
        assert "RecoveryJournalProjection" not in source
        assert source.count(".execute_primary(") == 1
        assert source.count(".execute_recovery(") == 1

    def test_cli_verdict_artifact_and_directory_are_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        evidence_dir = tmp_path / "new-evidence"
        verdict = cli._write_evidence(evidence_dir, {"status": "STOP"})
        assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(verdict.stat().st_mode) == 0o600

    def test_consumed_authorization_evidence_is_exact_owner_only_and_create_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        evidence_dir = cli._prepare_owner_only_directory(tmp_path / "evidence")
        consumed = replace(
            create_authorization(
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
                authorization_id=_AUTH_ID,
            ),
            status="CONSUMED",
        )

        artifact = cli._write_authorization_evidence(evidence_dir, consumed)

        assert artifact.name == "authorization.json"
        assert artifact.read_text(encoding="ascii") == consumed.to_json()
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        with pytest.raises(RuntimeError, match="EVIDENCE_ARTIFACT_ALREADY_EXISTS"):
            cli._write_authorization_evidence(evidence_dir, consumed)

    def test_atomic_writer_rejects_temporary_path_substitution_before_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        evidence_dir = cli._prepare_owner_only_directory(tmp_path / "evidence")
        artifact = evidence_dir / "authorization.json"
        replacement = b"credential-canary-substitution\n"
        real_link = cli.os.link

        def substitute_then_link(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
            *args: object,
            **kwargs: object,
        ) -> None:
            source_dir_fd = kwargs.get("src_dir_fd")
            if isinstance(source_dir_fd, int):
                cli.os.unlink(source, dir_fd=source_dir_fd)
                descriptor = cli.os.open(
                    source,
                    cli.os.O_WRONLY | cli.os.O_CREAT | cli.os.O_EXCL,
                    0o600,
                    dir_fd=source_dir_fd,
                )
                try:
                    cli.os.write(descriptor, replacement)
                    cli.os.fsync(descriptor)
                finally:
                    cli.os.close(descriptor)
            else:
                source_path = Path(source)
                source_path.unlink()
                source_path.write_bytes(replacement)
                cli.os.chmod(source_path, 0o600)
            real_link(source, destination, *args, **kwargs)

        monkeypatch.setattr(cli.os, "link", substitute_then_link)

        with pytest.raises(RuntimeError, match="EVIDENCE_TEMPORARY_INODE_CHANGED"):
            cli._atomic_write_owner_only_create_once(artifact, b"trusted\n")

        assert not artifact.exists()
        assert replacement not in b"".join(path.read_bytes() for path in evidence_dir.iterdir())

    def test_atomic_writer_rejects_parent_path_swap_and_cleans_bound_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        evidence_dir = cli._prepare_owner_only_directory(tmp_path / "evidence")
        displaced = tmp_path / "displaced-evidence"
        artifact = evidence_dir / "authorization.json"
        real_link = cli.os.link

        def swap_parent_then_link(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
            *args: object,
            **kwargs: object,
        ) -> None:
            evidence_dir.rename(displaced)
            evidence_dir.mkdir(mode=0o700)
            cli.os.chmod(evidence_dir, 0o700)
            real_link(source, destination, *args, **kwargs)

        monkeypatch.setattr(cli.os, "link", swap_parent_then_link)

        with pytest.raises(RuntimeError, match="EVIDENCE_DIRECTORY_PATH_RACE"):
            cli._atomic_write_owner_only_create_once(artifact, b"trusted\n")

        assert list(evidence_dir.iterdir()) == []
        assert list(displaced.iterdir()) == []


class TestCliFailClosed:
    @pytest.mark.parametrize("alternate", ("evidence_dir", "authorization_manifest"))
    def test_alternate_runtime_layout_stops_before_binding_or_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        alternate: str,
    ) -> None:
        cli = _load_cli(monkeypatch)
        runtime_root = _runtime_root(tmp_path)
        monkeypatch.setattr(cli, "_RUNTIME_EVIDENCE_ROOT", runtime_root, raising=False)
        runtime_called = False

        def verify(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal runtime_called
            runtime_called = True
            raise AssertionError("runtime binding must follow canonical layout admission")

        monkeypatch.setattr(cli, "verify_runtime_binding", verify)
        override = {
            "evidence_dir": tmp_path / "alternate-session",
            "authorization_manifest": tmp_path / "alternate-authorization.json",
        }[alternate]

        code = cli.main(_canonical_args(tmp_path, **{alternate: override}))

        assert code == 1
        assert runtime_called is False
        assert json.loads(capsys.readouterr().out) == {
            "exit_code": 1,
            "reason": "CANONICAL_RUNTIME_LAYOUT_REQUIRED",
        }
        assert not _canonical_session_dir(tmp_path).exists()
        assert not Path(override).exists()

    @pytest.mark.parametrize(
        "credential_name",
        (
            "BINANCE_DEMO_API_KEY",
            "BINANCE_DEMO_API_SECRET",
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "BINANCE_TESTNET_API_KEY",
            "BINANCE_TESTNET_API_SECRET",
            "BINANCE_FUTURES_TESTNET_API_KEY",
            "BINANCE_FUTURES_TESTNET_API_SECRET",
        ),
    )
    def test_credential_environment_presence_stops_before_runtime_without_canary_leak(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        credential_name: str,
    ) -> None:
        cli = _load_cli(monkeypatch)
        canary = f"credential-canary-{credential_name.lower()}"
        monkeypatch.setenv(credential_name, canary)
        runtime_called = False

        def verify(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal runtime_called
            runtime_called = True
            return SimpleNamespace(
                runtime_commit=_RUNTIME,
                runtime_tree="e" * 40,
                branch="codex/test",
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )

        monkeypatch.setattr(cli, "verify_runtime_binding", verify)

        code = cli.main(_args(tmp_path))

        stdout = capsys.readouterr().out
        result = json.loads(stdout)
        assert code == 1
        assert runtime_called is False
        assert result == {
            "exit_code": 1,
            "reason": "SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT",
        }
        assert canary not in stdout
        assert credential_name not in stdout
        assert not _canonical_session_dir(tmp_path).exists()
        assert not _canonical_manifest(tmp_path).exists()

    def test_runtime_binding_mismatch_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _bind_runtime_root(monkeypatch, cli, tmp_path)
        monkeypatch.setattr(
            cli,
            "verify_runtime_binding",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeBindingError("RUNTIME_COMMIT_MISMATCH")),
        )
        code = cli.main(_args(tmp_path))
        assert code == 1
        payload = json.loads(
            (_canonical_session_dir(tmp_path) / "cli-verdict.json").read_text(encoding="utf-8")
        )
        assert payload["status"] == "STOP"
        assert payload["reason_codes"] == ["RUNTIME_COMMIT_MISMATCH"]

    def test_runtime_binding_explicitly_binds_cli_and_fixed_credential_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _bind_runtime_root(monkeypatch, cli, tmp_path)
        calls: list[dict[str, object]] = []

        def verify(*_args: object, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                runtime_commit=_RUNTIME,
                runtime_tree="e" * 40,
                branch="codex/test",
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )

        monkeypatch.setattr(cli, "verify_runtime_binding", verify)
        assert cli.main(_args(tmp_path)) == 0
        assert len(calls) == 1
        required = calls[0]["required_source_paths"]
        assert required == (
            _SCRIPTS.resolve(),
            _SCRIPTS.parents[1] / "src/global_quant/gate1b/credential_session.py",
        )


class TestCliReviewGating:
    def test_review_absent_stops_ready_for_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        code = cli.main(_args(tmp_path))
        assert code == 0
        payload = json.loads(
            (_canonical_session_dir(tmp_path) / "cli-verdict.json").read_text(encoding="utf-8")
        )
        assert payload["protocol_version"] == "1.7"
        assert payload["status"] == "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW"
        assert payload["reason_codes"] == ["INDEPENDENT_REVIEW_ARTIFACT_ABSENT"]
        assert payload["authorization_claimed"] is False
        assert payload["credential_child_started"] is False
        assert not _canonical_manifest(tmp_path).exists()

    def test_review_wrong_head_stops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        review = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit="f" * 40,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        code = cli.main(_args(tmp_path, review_artifact=review))
        assert code == 1
        payload = json.loads(
            (_canonical_session_dir(tmp_path) / "cli-verdict.json").read_text(encoding="utf-8")
        )
        assert "REVIEW_ARTIFACT_INVALID" in payload["reason_codes"][0]

    def test_review_nonzero_p0_stops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
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
        assert (
            "REVIEW_ARTIFACT_INVALID"
            in json.loads(
                (_canonical_session_dir(tmp_path) / "cli-verdict.json").read_text(encoding="utf-8")
            )["reason_codes"][0]
        )

    def test_review_error_never_projects_artifact_controlled_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        review = _valid_review(tmp_path)
        canary = "credential-canary-from-review-artifact"
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["verdict"] = canary
        review.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(review, 0o600)

        code = cli.main(_args(tmp_path, review_artifact=review))

        evidence = _canonical_session_dir(tmp_path) / "cli-verdict.json"
        stdout = capsys.readouterr().out
        assert code == 1
        assert canary not in stdout
        assert canary.encode() not in evidence.read_bytes()
        assert json.loads(stdout)["reason"] == "REVIEW_ARTIFACT_INVALID"
        assert json.loads(evidence.read_text(encoding="utf-8"))["reason_codes"] == [
            "REVIEW_ARTIFACT_INVALID"
        ]

    def test_removed_dry_run_plumbing_argument_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _load_cli(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            cli._parse_args([*_args(tmp_path), "--dry-run-plumbing"])
        assert exc.value.code != 0

    def test_review_valid_component_admission_failure_remains_post_claim_fail_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        authorization_path = _write_auth(tmp_path)
        review = _valid_review(tmp_path)
        monkeypatch.setattr(
            cli,
            "_build_execution_supervisor",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected factory failure")),
        )

        code = cli.main(_args(tmp_path, review_artifact=review))

        assert code == 1
        assert read_manifest(authorization_path).status == "CONSUMED"
        evidence_dir = _canonical_session_dir(tmp_path)
        assert (evidence_dir / "authorization.json").exists()
        assert not (evidence_dir / "cli-verdict.json").exists()
        assert json.loads(capsys.readouterr().out) == {
            "exit_code": 1,
            "reason": "EXECUTION_COMPONENT_ADMISSION_FAILED",
        }


class TestCliFormalExecution:
    def test_primary_orders_claim_artifact_factory_and_high_level_execute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        manifest = _write_auth(tmp_path)
        review = _valid_review(tmp_path)
        evidence_dir = _canonical_session_dir(tmp_path)
        events: list[str] = []

        def tracked_claim(path: Path, **kwargs: object):
            events.append("claim")
            return claim_authorization(path, **kwargs)

        real_authorization_writer = cli._write_authorization_evidence

        def tracked_authorization_writer(directory: Path, record: object) -> Path:
            events.append("authorization-evidence")
            return real_authorization_writer(directory, record)

        class Controller:
            def execute_primary(self, *, authority: object) -> SimpleNamespace:
                events.append("execute-primary")
                assert authority.authorization_id == _AUTH_ID
                assert authority.runtime_commit == _RUNTIME
                assert authority.generation == 1
                return _completion(evidence_dir)

            def execute_recovery(self, **_kwargs: object) -> object:
                raise AssertionError("primary authorization entered recovery")

        def build_controller(**kwargs: object) -> Controller:
            events.append("factory")
            assert kwargs["evidence_root"] == evidence_dir
            assert kwargs["recovery"] is False
            return Controller()

        monkeypatch.setattr(cli, "claim_authorization", tracked_claim, raising=False)
        monkeypatch.setattr(
            cli,
            "_write_authorization_evidence",
            tracked_authorization_writer,
        )
        monkeypatch.setattr(
            cli,
            "_build_execution_supervisor",
            build_controller,
            raising=False,
        )
        monkeypatch.setattr(
            cli,
            "_write_evidence",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("CLI verdict written after authorization claim")
            ),
        )

        code = cli.main(_args(tmp_path, review_artifact=review))

        assert code == 0
        assert events == [
            "claim",
            "authorization-evidence",
            "factory",
            "execute-primary",
        ]
        consumed = read_manifest(manifest)
        assert consumed.status == "CONSUMED"
        assert (evidence_dir / "authorization.json").read_text(encoding="ascii") == (
            consumed.to_json()
        )
        assert not (evidence_dir / "cli-verdict.json").exists()
        output = json.loads(capsys.readouterr().out)
        assert output == {
            "evidence": str(evidence_dir / "verdict.json"),
            "exit_code": 0,
            "status": "EXECUTION_COMPLETE_READY_FOR_INDEPENDENT_REVIEW",
        }
        assert "PASS" not in json.dumps(output)

    def test_consumed_authorization_evidence_failure_prevents_factory_and_child(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        manifest = _write_auth(tmp_path)
        review = _valid_review(tmp_path)
        evidence_dir = _canonical_session_dir(tmp_path)
        events: list[str] = []

        def tracked_claim(path: Path, **kwargs: object):
            events.append("claim")
            return claim_authorization(path, **kwargs)

        def fail_authorization_evidence(_directory: Path, _record: object) -> Path:
            events.append("authorization-evidence-failed")
            raise RuntimeError("injected authorization evidence failure")

        monkeypatch.setattr(cli, "claim_authorization", tracked_claim, raising=False)
        monkeypatch.setattr(
            cli,
            "_write_authorization_evidence",
            fail_authorization_evidence,
        )
        monkeypatch.setattr(
            cli,
            "_build_execution_supervisor",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("factory ran without durable authorization evidence")
            ),
            raising=False,
        )
        monkeypatch.setattr(
            cli,
            "_write_evidence",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("CLI verdict written after authorization claim")
            ),
        )

        code = cli.main(_args(tmp_path, review_artifact=review))

        assert code == 1
        assert events == ["claim", "authorization-evidence-failed"]
        assert read_manifest(manifest).status == "CONSUMED"
        assert not (evidence_dir / "authorization.json").exists()
        assert not (evidence_dir / "cli-verdict.json").exists()
        assert json.loads(capsys.readouterr().out) == {
            "exit_code": 1,
            "reason": "AUTHORIZATION_EVIDENCE_DURABILITY_FAILED",
        }

    def test_recovery_reuses_consumed_artifact_without_claim_or_overwrite(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        manifest = _write_auth(tmp_path)
        consumed = _claim(manifest)
        evidence_dir = _canonical_session_dir(tmp_path)
        artifact = cli._write_authorization_evidence(evidence_dir, consumed)
        before = artifact.stat(follow_symlinks=False)
        before_bytes = artifact.read_bytes()
        mark_recovery(manifest, consumed)
        review = _valid_review(tmp_path)
        events: list[str] = []

        class Controller:
            def execute_primary(self, **_kwargs: object) -> object:
                raise AssertionError("recovery regained primary capability")

            def execute_recovery(self, *, primary_authority: object) -> SimpleNamespace:
                events.append("execute-recovery")
                assert primary_authority.authorization_id == _AUTH_ID
                assert primary_authority.generation == 1
                return _completion(evidence_dir)

        def build_controller(**kwargs: object) -> Controller:
            events.append("factory")
            assert kwargs["evidence_root"] == evidence_dir
            assert kwargs["recovery"] is True
            return Controller()

        monkeypatch.setattr(
            cli,
            "claim_authorization",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovery attempted a primary claim")
            ),
            raising=False,
        )
        monkeypatch.setattr(
            cli,
            "_write_authorization_evidence",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovery overwrote authorization evidence")
            ),
        )
        monkeypatch.setattr(
            cli,
            "_build_execution_supervisor",
            build_controller,
            raising=False,
        )

        code = cli.main(_args(tmp_path, review_artifact=review))

        assert code == 0
        assert events == ["factory", "execute-recovery"]
        after = artifact.stat(follow_symlinks=False)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert artifact.read_bytes() == before_bytes
        assert read_manifest(manifest).status == "RECOVERY"
        assert not (evidence_dir / "cli-verdict.json").exists()
        assert json.loads(capsys.readouterr().out)["status"] == (
            "EXECUTION_COMPLETE_READY_FOR_INDEPENDENT_REVIEW"
        )

    def test_primary_supervisor_error_only_marks_registry_recovery_and_prints_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cli = _load_cli(monkeypatch)
        _ok_binding(monkeypatch, cli, tmp_path)
        manifest = _write_auth(tmp_path)
        review = _valid_review(tmp_path)
        evidence_dir = _canonical_session_dir(tmp_path)

        class Controller:
            def execute_primary(self, *, authority: object) -> object:
                assert authority.authorization_id == _AUTH_ID
                raise SupervisorError("INJECTED_SUPERVISOR_STOP")

            def execute_recovery(self, **_kwargs: object) -> object:
                raise AssertionError("primary failure retried through recovery in one CLI")

        monkeypatch.setattr(
            cli,
            "_build_execution_supervisor",
            lambda **_kwargs: Controller(),
            raising=False,
        )
        monkeypatch.setattr(
            cli,
            "_write_evidence",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("CLI verdict written after authorization handoff")
            ),
        )

        code = cli.main(_args(tmp_path, review_artifact=review))

        assert code == 1
        assert read_manifest(manifest).status == "RECOVERY"
        retained = json.loads((evidence_dir / "authorization.json").read_text(encoding="ascii"))
        assert retained["status"] == "CONSUMED"
        assert not (evidence_dir / "cli-verdict.json").exists()
        assert json.loads(capsys.readouterr().out) == {
            "exit_code": 1,
            "reason": "INJECTED_SUPERVISOR_STOP",
        }

    def test_primary_factory_uses_one_deadline_and_canonical_same_root_components(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = _load_cli(monkeypatch)
        root = cli._prepare_owner_only_directory(tmp_path / "session")
        calls: list[tuple[str, object]] = []
        monotonic_calls = 0

        def monotonic() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            calls.append(("monotonic", 321.25))
            return 321.25

        class Journal:
            def __init__(self, path: Path) -> None:
                self.path = Path(path)
                calls.append(("journal", self.path))

        class Lifecycle:
            @classmethod
            def start(cls, path: Path, **kwargs: object) -> SimpleNamespace:
                calls.append(("lifecycle-start", (Path(path), kwargs)))
                return SimpleNamespace(path=Path(path))

            @classmethod
            def restore(cls, _path: Path) -> object:
                raise AssertionError("primary restored a prior lifecycle")

        class ProcessSupervisor:
            def __init__(self, **kwargs: object) -> None:
                calls.append(("process-supervisor", kwargs))

        class EvidenceLog:
            def __init__(self, path: Path, **kwargs: object) -> None:
                calls.append(("evidence-log", (Path(path), kwargs)))

        class Projector:
            def __init__(self, **kwargs: object) -> None:
                calls.append(("projector", kwargs))

        class Finalizer:
            def __init__(self, **kwargs: object) -> None:
                calls.append(("finalizer", kwargs))

        class Workload:
            @classmethod
            def production(cls, path: Path, *, runtime_sha256: str) -> object:
                calls.append(("workload", (Path(path), runtime_sha256)))
                return object()

        sentinel = object()

        class Supervisor:
            @classmethod
            def production(cls, **kwargs: object) -> object:
                calls.append(("supervisor", kwargs))
                return sentinel

        monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=monotonic), raising=False)
        monkeypatch.setattr(cli, "ExecutionJournal", Journal, raising=False)
        monkeypatch.setattr(cli, "ProcessLifecycleJournal", Lifecycle, raising=False)
        monkeypatch.setattr(cli, "CredentialProcessSupervisor", ProcessSupervisor, raising=False)
        monkeypatch.setattr(cli, "ExecutionEvidenceLog", EvidenceLog, raising=False)
        monkeypatch.setattr(cli, "ExecutionProjector", Projector, raising=False)
        monkeypatch.setattr(cli, "FinalEvidenceFinalizer", Finalizer, raising=False)
        monkeypatch.setattr(cli, "CredentialWorkload", Workload, raising=False)
        monkeypatch.setattr(cli, "ExecutionSupervisor", Supervisor)

        snapshot = SimpleNamespace(runtime_commit=_RUNTIME)
        result = cli._build_execution_supervisor(
            evidence_root=root,
            runtime_snapshot=snapshot,
            recovery=False,
        )

        assert result is sentinel
        assert monotonic_calls == 1
        assert calls[0] == ("monotonic", 321.25)
        assert calls[1] == ("journal", root / "request-ledger.json")
        lifecycle_path, lifecycle_kwargs = calls[2][1]
        assert lifecycle_path == root / "lifecycle.jsonl"
        assert lifecycle_kwargs == {
            "execution_journal_path": root / "request-ledger.json",
            "lifecycle_deadline": 501.25,
            "lifecycle_started_at": 321.25,
        }
        evidence_path, evidence_kwargs = next(
            value for name, value in calls if name == "evidence-log"
        )
        assert evidence_path == root / "requests.jsonl"
        assert evidence_kwargs == {
            "credential_canaries": (),
            "execution_journal_path": root / "request-ledger.json",
        }
        supervisor_kwargs = next(value for name, value in calls if name == "supervisor")
        assert set(supervisor_kwargs) == {
            "evidence_log",
            "execution_journal",
            "finalizer",
            "process_lifecycle_journal",
            "process_supervisor",
            "projector",
            "workload",
        }
