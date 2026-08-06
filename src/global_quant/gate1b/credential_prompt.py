from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import termios
from collections.abc import Callable
from collections.abc import Mapping
from io import TextIOBase
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
MAX_PRIVATE_KEY_LINE_LENGTH = 4096


class CredentialPromptError(RuntimeError):
    """Raised before prompting when ephemeral credential injection is unsafe."""


def run_prompted_preflight(
    *,
    evidence_dir: Path,
    parent_environ: Mapping[str, str],
    prompt_secret: Callable[[str], str] = getpass.getpass,
    prompt_private_key: Callable[[], str] | None = None,
    input_is_tty: bool,
    key_type: str = "hmac",
) -> tuple[int, Path]:
    _validate_parent_environment(parent_environ)
    if not input_is_tty:
        raise CredentialPromptError("INTERACTIVE_TERMINAL_REQUIRED")

    api_key = prompt_secret("Demo API key (hidden): ")
    if not api_key:
        raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")
    api_secret = prompt_api_secret(
        key_type=key_type,
        prompt_secret=prompt_secret,
        prompt_private_key=prompt_private_key or read_hidden_ed25519_private_key,
    )

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
    prompt_private_key: Callable[[], str] | None = None,
) -> str:
    if key_type == "hmac":
        value = prompt_secret("Demo API secret (hidden): ")
        if not value:
            raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")
        return value
    if key_type != "ed25519":
        raise CredentialPromptError("UNSUPPORTED_DEMO_KEY_TYPE")
    private_key_reader = prompt_private_key or read_hidden_ed25519_private_key
    return normalize_ed25519_private_key(private_key_reader())


def read_hidden_ed25519_private_key(
    *,
    input_stream: TextIOBase | None = None,
    output_stream: TextIOBase | None = None,
) -> str:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stderr
    if not input_stream.isatty():
        raise CredentialPromptError("INTERACTIVE_TERMINAL_REQUIRED")

    try:
        descriptor = input_stream.fileno()
        original = termios.tcgetattr(descriptor)
    except (AttributeError, OSError) as exc:
        raise CredentialPromptError("TERMINAL_ECHO_GUARD_UNAVAILABLE") from exc

    hidden = original.copy()
    hidden[3] &= ~termios.ECHO
    lines: list[str] = []
    output_stream.write("Paste the complete Ed25519 private key PEM (hidden):\n")
    output_stream.flush()
    try:
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
        for _ in range(MAX_PRIVATE_KEY_LINES):
            line = input_stream.readline(MAX_PRIVATE_KEY_LINE_LENGTH + 1)
            if not line or len(line) > MAX_PRIVATE_KEY_LINE_LENGTH:
                raise CredentialPromptError("INVALID_ED25519_PRIVATE_KEY")
            stripped = line.rstrip("\r\n")
            lines.append(stripped)
            if stripped == ED25519_END:
                break
        else:
            raise CredentialPromptError("INVALID_ED25519_PRIVATE_KEY")
    finally:
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
        output_stream.write("\n")
        output_stream.flush()

    return normalize_ed25519_private_key("\n".join(lines) + "\n")


def normalize_ed25519_private_key(value: str) -> str:
    lines = value.splitlines()
    if (
        len(lines) < 3
        or len(lines) > MAX_PRIVATE_KEY_LINES
        or lines[0] != ED25519_BEGIN
        or lines[-1] != ED25519_END
        or any(not line for line in lines[1:-1])
    ):
        raise CredentialPromptError("INVALID_ED25519_PRIVATE_KEY")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
