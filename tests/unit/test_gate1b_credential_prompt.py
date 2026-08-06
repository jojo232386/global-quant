from __future__ import annotations

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
