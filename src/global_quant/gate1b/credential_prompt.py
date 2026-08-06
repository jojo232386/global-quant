from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

from global_quant.gate1b.runner import run_preflight
from global_quant.gate1b.safety import CONFLICTING_CREDENTIAL_NAMES
from global_quant.gate1b.safety import DEMO_KEY_NAME
from global_quant.gate1b.safety import DEMO_SECRET_NAME


ALL_BINANCE_CREDENTIAL_NAMES = (
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
    *CONFLICTING_CREDENTIAL_NAMES,
)
ED25519_BEGIN = "-----BEGIN PRIVATE KEY-----"
ED25519_END = "-----END PRIVATE KEY-----"
MAX_PRIVATE_KEY_LINES = 64


class CredentialPromptError(RuntimeError):
    """Raised before prompting when ephemeral credential injection is unsafe."""


def run_prompted_preflight(
    *,
    evidence_dir: Path,
    parent_environ: Mapping[str, str],
    prompt_secret: Callable[[str], str] = getpass.getpass,
    input_is_tty: bool,
    key_type: str = "hmac",
) -> tuple[int, Path]:
    _validate_parent_environment(parent_environ)
    if not input_is_tty:
        raise CredentialPromptError("INTERACTIVE_TERMINAL_REQUIRED")

    api_key = prompt_secret("Demo API key (hidden): ")
    if not api_key:
        raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")
    api_secret = prompt_api_secret(key_type=key_type, prompt_secret=prompt_secret)

    ephemeral = {
        DEMO_KEY_NAME: api_key,
        DEMO_SECRET_NAME: api_secret,
    }
    try:
        return run_preflight(
            environ=ephemeral,
            confirm_demo_only=True,
            evidence_dir=Path(evidence_dir),
        )
    finally:
        ephemeral.clear()
        api_key = ""
        api_secret = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-demo-only", action="store_true")
    parser.add_argument("--key-type", choices=("hmac", "ed25519"), required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.preflight or not args.confirm_demo_only:
        print(json.dumps({"exit_code": 1, "reason": "EXPLICIT_PREFLIGHT_ARMING_REQUIRED"}))
        return 1

    try:
        exit_code, path = run_prompted_preflight(
            evidence_dir=args.evidence_dir,
            parent_environ=os.environ,
            input_is_tty=sys.stdin.isatty(),
            key_type=args.key_type,
        )
    except CredentialPromptError as exc:
        print(json.dumps({"exit_code": 1, "reason": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


def _validate_parent_environment(environ: Mapping[str, str]) -> None:
    if any(name in environ for name in ALL_BINANCE_CREDENTIAL_NAMES):
        raise CredentialPromptError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")


def prompt_api_secret(
    *,
    key_type: str,
    prompt_secret: Callable[[str], str],
) -> str:
    if key_type == "hmac":
        value = prompt_secret("Demo API secret (hidden): ")
        if not value:
            raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")
        return value
    if key_type != "ed25519":
        raise CredentialPromptError("UNSUPPORTED_DEMO_KEY_TYPE")

    lines = [prompt_secret("Ed25519 private key line 1 (hidden): ")]
    if lines[0] != ED25519_BEGIN:
        raise CredentialPromptError("INVALID_ED25519_PRIVATE_KEY")
    for line_number in range(2, MAX_PRIVATE_KEY_LINES + 1):
        line = prompt_secret(f"Ed25519 private key line {line_number} (hidden): ")
        lines.append(line)
        if line == ED25519_END:
            return "\n".join(lines) + "\n"
    raise CredentialPromptError("UNTERMINATED_ED25519_PRIVATE_KEY")


if __name__ == "__main__":
    raise SystemExit(main())
