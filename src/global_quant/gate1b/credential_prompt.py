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


class CredentialPromptError(RuntimeError):
    """Raised before prompting when ephemeral credential injection is unsafe."""


def run_prompted_preflight(
    *,
    evidence_dir: Path,
    parent_environ: Mapping[str, str],
    prompt_secret: Callable[[str], str] = getpass.getpass,
    input_is_tty: bool,
) -> tuple[int, Path]:
    _validate_parent_environment(parent_environ)
    if not input_is_tty:
        raise CredentialPromptError("INTERACTIVE_TERMINAL_REQUIRED")

    api_key = prompt_secret("Demo API key (hidden): ")
    api_secret = prompt_secret("Demo API secret (hidden): ")
    if not api_key or not api_secret:
        raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")

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
        )
    except CredentialPromptError as exc:
        print(json.dumps({"exit_code": 1, "reason": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


def _validate_parent_environment(environ: Mapping[str, str]) -> None:
    if any(name in environ for name in ALL_BINANCE_CREDENTIAL_NAMES):
        raise CredentialPromptError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")


if __name__ == "__main__":
    raise SystemExit(main())
