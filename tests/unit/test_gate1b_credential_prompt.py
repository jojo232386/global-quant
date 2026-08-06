from __future__ import annotations

import io
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


def test_prompted_preflight_reads_ed25519_private_key_in_one_hidden_capture(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}
    private_key_prompts = 0

    def fake_run_preflight(*, environ, confirm_demo_only, evidence_dir):
        captured.update(environ)
        evidence = Path(evidence_dir) / "preflight.json"
        evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")
        return 0, evidence

    monkeypatch.setattr(credential_prompt, "run_preflight", fake_run_preflight)

    def prompt_private_key() -> str:
        nonlocal private_key_prompts
        private_key_prompts += 1
        return (
            "-----BEGIN PRIVATE KEY-----\n"
            "base64-private-material-test-only\n"
            "-----END PRIVATE KEY-----\n"
        )

    exit_code, _ = credential_prompt.run_prompted_preflight(
        evidence_dir=tmp_path,
        parent_environ={},
        prompt_secret=lambda _label: "ed25519-demo-key",
        prompt_private_key=prompt_private_key,
        input_is_tty=True,
        key_type="ed25519",
    )

    assert exit_code == 0
    assert captured["BINANCE_DEMO_API_KEY"] == "ed25519-demo-key"
    assert captured["BINANCE_DEMO_API_SECRET"] == (
        "-----BEGIN PRIVATE KEY-----\n"
        "base64-private-material-test-only\n"
        "-----END PRIVATE KEY-----\n"
    )
    assert private_key_prompts == 1


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


def test_ed25519_private_key_capture_keeps_echo_disabled_for_entire_pem(monkeypatch) -> None:
    class FakeTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 123

    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "base64-private-material-test-only\n"
        "-----END PRIVATE KEY-----\n"
    )
    input_stream = FakeTerminal(private_key)
    output_stream = io.StringIO()
    original = [0, 0, 0, credential_prompt.termios.ECHO | 8, 0, 0]
    changes: list[tuple[int, int, list[int]]] = []
    monkeypatch.setattr(credential_prompt.termios, "tcgetattr", lambda _fd: original.copy())
    monkeypatch.setattr(
        credential_prompt.termios,
        "tcsetattr",
        lambda fd, when, attrs: changes.append((fd, when, attrs.copy())),
    )

    captured = credential_prompt.read_hidden_ed25519_private_key(
        input_stream=input_stream,
        output_stream=output_stream,
    )

    assert captured == private_key
    assert len(changes) == 2
    assert changes[0][2][3] & credential_prompt.termios.ECHO == 0
    assert changes[1][2] == original
    assert "base64-private-material-test-only" not in output_stream.getvalue()


def test_ed25519_private_key_capture_restores_echo_after_invalid_input(monkeypatch) -> None:
    class FakeTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 456

    input_stream = FakeTerminal("invalid\n")
    output_stream = io.StringIO()
    original = [0, 0, 0, credential_prompt.termios.ECHO | 8, 0, 0]
    changes: list[list[int]] = []
    monkeypatch.setattr(credential_prompt.termios, "tcgetattr", lambda _fd: original.copy())
    monkeypatch.setattr(
        credential_prompt.termios,
        "tcsetattr",
        lambda _fd, _when, attrs: changes.append(attrs.copy()),
    )

    with pytest.raises(CredentialPromptError, match="INVALID_ED25519_PRIVATE_KEY"):
        credential_prompt.read_hidden_ed25519_private_key(
            input_stream=input_stream,
            output_stream=output_stream,
        )

    assert len(changes) == 2
    assert changes[0][3] & credential_prompt.termios.ECHO == 0
    assert changes[1] == original


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
