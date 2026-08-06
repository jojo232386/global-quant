from __future__ import annotations

import argparse
import getpass
import json
import os
import resource
import stat
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
MAX_PRIVATE_KEY_BYTES = 16 * 1024


class CredentialPromptError(RuntimeError):
    """Raised before prompting when ephemeral credential injection is unsafe."""


def run_prompted_preflight(
    *,
    evidence_dir: Path,
    parent_environ: Mapping[str, str],
    prompt_secret: Callable[[str], str] = getpass.getpass,
    input_is_tty: bool,
    key_type: str = "hmac",
    private_key_file: Path | None = None,
    core_dump_guard: Callable[[], None] | None = None,
) -> tuple[int, Path]:
    _validate_parent_environment(parent_environ)
    if not input_is_tty:
        raise CredentialPromptError("INTERACTIVE_TERMINAL_REQUIRED")
    (core_dump_guard or disable_core_dumps)()

    if key_type == "ed25519":
        if private_key_file is None:
            raise CredentialPromptError("ED25519_PRIVATE_KEY_FILE_REQUIRED")
        api_secret = read_ed25519_private_key(private_key_file)
    elif key_type == "hmac":
        if private_key_file is not None:
            raise CredentialPromptError("PRIVATE_KEY_FILE_FORBIDDEN_FOR_HMAC")
        api_secret = ""
    else:
        raise CredentialPromptError("UNSUPPORTED_DEMO_KEY_TYPE")

    api_key = prompt_secret("Demo API key (hidden): ")
    if not api_key:
        raise CredentialPromptError("EMPTY_DEMO_CREDENTIAL")
    if key_type == "hmac":
        api_secret = prompt_secret("Demo API secret (hidden): ")
        if not api_secret:
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
    parser.add_argument("--key-type", choices=("hmac", "ed25519"), required=True)
    parser.add_argument("--private-key-file", type=Path)
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
            private_key_file=args.private_key_file,
        )
    except CredentialPromptError as exc:
        print(json.dumps({"exit_code": 1, "reason": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


def _validate_parent_environment(environ: Mapping[str, str]) -> None:
    if any(name in environ for name in ALL_BINANCE_CREDENTIAL_NAMES):
        raise CredentialPromptError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")


def disable_core_dumps() -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError) as exc:
        raise CredentialPromptError("CORE_DUMP_GUARD_UNAVAILABLE") from exc


def read_ed25519_private_key(path: Path) -> str:
    path = Path(path)
    if path.is_symlink():
        raise CredentialPromptError("PRIVATE_KEY_MUST_BE_REGULAR_FILE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CredentialPromptError("PRIVATE_KEY_FILE_UNAVAILABLE") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialPromptError("PRIVATE_KEY_MUST_BE_REGULAR_FILE")
        if metadata.st_uid != os.getuid():
            raise CredentialPromptError("PRIVATE_KEY_OWNER_MISMATCH")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CredentialPromptError("INSECURE_PRIVATE_KEY_PERMISSIONS")
        if metadata.st_size <= 0 or metadata.st_size > MAX_PRIVATE_KEY_BYTES:
            raise CredentialPromptError("PRIVATE_KEY_SIZE_INVALID")
        encoded = _read_bounded(descriptor, MAX_PRIVATE_KEY_BYTES)
    finally:
        os.close(descriptor)

    try:
        value = encoded.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CredentialPromptError("INVALID_ED25519_PRIVATE_KEY") from exc
    return normalize_ed25519_private_key(value)


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(4096, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise CredentialPromptError("PRIVATE_KEY_SIZE_INVALID")
    return b"".join(chunks)


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
