from __future__ import annotations

import os
from pathlib import Path

import pytest

import global_quant.gate1b.credential_prompt as credential_prompt
from global_quant.gate1b.credential_prompt import CredentialPromptError


def test_prompted_preflight_refuses_live_or_testnet_environment_before_prompt(tmp_path) -> None:
    prompted = False

    def forbidden_prompt(_label: str) -> str:
        nonlocal prompted
        prompted = True
        raise AssertionError("credential prompt should not run")

    with pytest.raises(CredentialPromptError, match="CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"):
        credential_prompt.run_prompted_preflight(
            evidence_dir=tmp_path,
            parent_environ={"BINANCE_API_KEY": "live-value-must-never-be-read"},
            prompt_secret=forbidden_prompt,
            input_is_tty=True,
        )

    assert prompted is False


def test_prompted_preflight_refuses_preexisting_demo_environment_before_prompt(tmp_path) -> None:
    with pytest.raises(CredentialPromptError, match="CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"):
        credential_prompt.run_prompted_preflight(
            evidence_dir=tmp_path,
            parent_environ={"BINANCE_DEMO_API_KEY": "stale-demo-key"},
            prompt_secret=lambda _label: "must-not-be-used",
            input_is_tty=True,
        )


def test_prompted_preflight_requires_an_interactive_terminal(tmp_path) -> None:
    with pytest.raises(CredentialPromptError, match="INTERACTIVE_TERMINAL_REQUIRED"):
        credential_prompt.run_prompted_preflight(
            evidence_dir=tmp_path,
            parent_environ={},
            prompt_secret=lambda _label: "must-not-be-used",
            input_is_tty=False,
        )


def test_prompted_preflight_injects_secrets_only_into_in_process_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    answers = iter(("ephemeral-demo-key", "ephemeral-demo-secret"))
    captured: dict[str, object] = {}

    def fake_run_preflight(*, environ, confirm_demo_only, evidence_dir):
        captured["environ"] = dict(environ)
        captured["confirm_demo_only"] = confirm_demo_only
        captured["evidence_dir"] = evidence_dir
        evidence = Path(evidence_dir) / "preflight.json"
        evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")
        return 0, evidence

    monkeypatch.setattr(credential_prompt, "run_preflight", fake_run_preflight)

    exit_code, evidence_path = credential_prompt.run_prompted_preflight(
        evidence_dir=tmp_path,
        parent_environ={},
        prompt_secret=lambda _label: next(answers),
        input_is_tty=True,
    )

    assert exit_code == 0
    assert evidence_path == tmp_path / "preflight.json"
    assert captured == {
        "environ": {
            "BINANCE_DEMO_API_KEY": "ephemeral-demo-key",
            "BINANCE_DEMO_API_SECRET": "ephemeral-demo-secret",
        },
        "confirm_demo_only": True,
        "evidence_dir": tmp_path,
    }


def test_prompted_preflight_reads_ed25519_private_key_from_secure_file(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}
    private_key = tmp_path / "demo.pem"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\n"
        "base64-private-material-test-only\n"
        "-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    private_key.chmod(0o600)

    def fake_run_preflight(*, environ, confirm_demo_only, evidence_dir):
        captured.update(environ)
        evidence = Path(evidence_dir) / "preflight.json"
        evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")
        return 0, evidence

    monkeypatch.setattr(credential_prompt, "run_preflight", fake_run_preflight)

    exit_code, _ = credential_prompt.run_prompted_preflight(
        evidence_dir=tmp_path,
        parent_environ={},
        prompt_secret=lambda _label: "ed25519-demo-key",
        input_is_tty=True,
        key_type="ed25519",
        private_key_file=private_key,
    )

    assert exit_code == 0
    assert captured["BINANCE_DEMO_API_KEY"] == "ed25519-demo-key"
    assert captured["BINANCE_DEMO_API_SECRET"] == (
        "-----BEGIN PRIVATE KEY-----\n"
        "base64-private-material-test-only\n"
        "-----END PRIVATE KEY-----\n"
    )


def test_ed25519_private_key_file_is_required(tmp_path) -> None:
    with pytest.raises(CredentialPromptError, match="ED25519_PRIVATE_KEY_FILE_REQUIRED"):
        credential_prompt.run_prompted_preflight(
            evidence_dir=tmp_path,
            parent_environ={},
            prompt_secret=lambda _label: "ed25519-demo-key",
            input_is_tty=True,
            key_type="ed25519",
        )


@pytest.mark.parametrize(
    "value",
    [
        "not-a-pem\n",
        "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
        "prefix\n-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\nsuffix\n",
    ],
)
def test_ed25519_private_key_requires_bounded_complete_pem(value) -> None:
    with pytest.raises(CredentialPromptError, match="INVALID_ED25519_PRIVATE_KEY"):
        credential_prompt.normalize_ed25519_private_key(value)


def test_ed25519_private_key_file_refuses_insecure_permissions(tmp_path) -> None:
    private_key = tmp_path / "insecure.pem"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\n"
        "body\n"
        "-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    private_key.chmod(0o644)

    with pytest.raises(CredentialPromptError, match="INSECURE_PRIVATE_KEY_PERMISSIONS"):
        credential_prompt.read_ed25519_private_key(private_key)


def test_ed25519_private_key_file_refuses_symlink(tmp_path) -> None:
    target = tmp_path / "target.pem"
    target.write_text(
        "-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    target.chmod(0o600)
    link = tmp_path / "link.pem"
    link.symlink_to(target)

    with pytest.raises(CredentialPromptError, match="PRIVATE_KEY_MUST_BE_REGULAR_FILE"):
        credential_prompt.read_ed25519_private_key(link)


def test_ed25519_private_key_file_must_belong_to_current_user(tmp_path, monkeypatch) -> None:
    private_key = tmp_path / "foreign.pem"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    private_key.chmod(0o600)
    monkeypatch.setattr(os, "getuid", lambda: private_key.stat().st_uid + 1)

    with pytest.raises(CredentialPromptError, match="PRIVATE_KEY_OWNER_MISMATCH"):
        credential_prompt.read_ed25519_private_key(private_key)


def test_ed25519_private_key_file_refuses_oversize_content(tmp_path) -> None:
    private_key = tmp_path / "oversize.pem"
    private_key.write_bytes(b"x" * (credential_prompt.MAX_PRIVATE_KEY_BYTES + 1))
    private_key.chmod(0o600)

    with pytest.raises(CredentialPromptError, match="PRIVATE_KEY_SIZE_INVALID"):
        credential_prompt.read_ed25519_private_key(private_key)


def test_core_dump_guard_sets_hard_and_soft_limits_to_zero(monkeypatch) -> None:
    captured: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        credential_prompt.resource,
        "setrlimit",
        lambda resource_id, limits: captured.append((resource_id, limits)),
    )

    credential_prompt.disable_core_dumps()

    assert captured == [(credential_prompt.resource.RLIMIT_CORE, (0, 0))]


@pytest.mark.parametrize("answers", [("", "secret"), ("key", "")])
def test_prompted_preflight_rejects_empty_values(tmp_path, answers) -> None:
    values = iter(answers)

    with pytest.raises(CredentialPromptError, match="EMPTY_DEMO_CREDENTIAL"):
        credential_prompt.run_prompted_preflight(
            evidence_dir=tmp_path,
            parent_environ={},
            prompt_secret=lambda _label: next(values),
            input_is_tty=True,
        )
