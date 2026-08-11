from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import global_quant.gate1b.credential_prompt as credential_prompt
from global_quant.gate1b.credential_prompt import CredentialPromptError


def test_importing_credential_helpers_does_not_load_legacy_runner() -> None:
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    script = "\n".join(
        (
            "import sys",
            f"sys.path.insert(0, {source_root!r})",
            "import global_quant.gate1b.credential_prompt",
            "assert 'global_quant.gate1b.runner' not in sys.modules",
            "assert 'global_quant.gate1b.mutation_runner' not in sys.modules",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_legacy_prompted_credential_execution_surfaces_are_retired() -> None:
    assert not hasattr(credential_prompt, "run_preflight")
    assert not hasattr(credential_prompt, "run_prompted_preflight")


def test_legacy_prompted_entrypoint_is_a_sanitized_stop(capsys) -> None:
    assert credential_prompt.main(["--anything"]) == 1
    assert capsys.readouterr().out == (
        '{"exit_code": 1, "reason": "LEGACY_CREDENTIAL_ENTRYPOINT_RETIRED"}\n'
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
        "-----BEGIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----\n",
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
