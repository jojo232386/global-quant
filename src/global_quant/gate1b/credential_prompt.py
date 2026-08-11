from __future__ import annotations

import json
import os
import stat
from pathlib import Path

ED25519_BEGIN = "-----BEGIN PRIVATE KEY-----"
ED25519_END = "-----END PRIVATE KEY-----"
MAX_PRIVATE_KEY_LINES = 64
MAX_PRIVATE_KEY_BYTES = 16 * 1024


class CredentialPromptError(RuntimeError):
    """The child-local private-key reader rejected its input."""


def main(argv: list[str] | None = None) -> int:
    """The legacy prompted entrypoint is permanently fail-closed."""

    del argv
    print(
        json.dumps(
            {"exit_code": 1, "reason": "LEGACY_CREDENTIAL_ENTRYPOINT_RETIRED"},
            sort_keys=True,
        )
    )
    return 1


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
