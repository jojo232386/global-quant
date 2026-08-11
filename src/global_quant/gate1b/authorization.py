"""Owner-only authorization manifest for the frozen NT-GATE-1B v1.6 protocol.

Implements protocol section 9's one-time authorization boundary:

* Each explicit runtime authorization creates one non-secret ID of the form
  ``g1b16-{16 lowercase hex}`` in an owner-only local manifest.
* The manifest binds the authorization to the exact frozen protocol commit, the
  annotated tag object, the protocol SHA-256, and the runtime commit it is valid
  for. A mismatch or replay across protocol/runtime fails closed.
* The manifest is local and git-ignored (protocol section 16); it stores no
  credential value, only non-secret authorization metadata.
* A consumed or recovery authorization cannot be reused to obtain another
  attempt; the model may not invent a replacement ID.
* ``claim_authorization`` atomically validates and marks an authorization
  CONSUMED so that concurrent processes cannot both enter the credential-bearing
  lifecycle using the same authorization ID.

This module is credential-free. It never reads, indexes, or serializes any API
key or secret.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_AUTHORIZATION_ID = re.compile(r"^g1b16-[0-9a-f]{16}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TAG_OBJECT = re.compile(r"^[0-9a-f]{40}$")


class AuthorizationError(RuntimeError):
    """Raised when a one-time authorization cannot be proven or is replayed."""


@dataclass(frozen=True)
class AuthorizationRecord:
    authorization_id: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str
    runtime_commit: str
    created_at: str
    status: str  # "ACTIVE" | "CONSUMED" | "RECOVERY"

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "authorization_id": self.authorization_id,
                    "protocol_commit": self.protocol_commit,
                    "protocol_tag_object": self.protocol_tag_object,
                    "protocol_sha256": self.protocol_sha256,
                    "runtime_commit": self.runtime_commit,
                    "created_at": self.created_at,
                    "status": self.status,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> AuthorizationRecord:
        required = (
            "authorization_id",
            "protocol_commit",
            "protocol_tag_object",
            "protocol_sha256",
            "runtime_commit",
            "created_at",
            "status",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise AuthorizationError(f"AUTHORIZATION_MANIFEST_MISSING_FIELDS:{missing}")
        return cls(
            authorization_id=str(data["authorization_id"]),
            protocol_commit=str(data["protocol_commit"]),
            protocol_tag_object=str(data["protocol_tag_object"]),
            protocol_sha256=str(data["protocol_sha256"]),
            runtime_commit=str(data["runtime_commit"]),
            created_at=str(data["created_at"]),
            status=str(data["status"]),
        )


def is_valid_authorization_id(value: str) -> bool:
    return bool(_AUTHORIZATION_ID.match(value))


def create_authorization(
    *,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    runtime_commit: str,
    authorization_id: str,
) -> AuthorizationRecord:
    """Create a new ACTIVE authorization record bound to the frozen protocol.

    The caller must supply a freshly generated ``authorization_id`` matching the
    frozen ``g1b16-{16 hex}`` format. This function does not generate the ID
    itself (the supervisor session generates it from OS randomness), so the model
    cannot invent a replacement ID to obtain another attempt.
    """

    if not is_valid_authorization_id(authorization_id):
        raise AuthorizationError("INVALID_AUTHORIZATION_ID_FORMAT")
    if not _GIT_COMMIT.match(protocol_commit):
        raise AuthorizationError("INVALID_PROTOCOL_COMMIT")
    if not _TAG_OBJECT.match(protocol_tag_object):
        raise AuthorizationError("INVALID_PROTOCOL_TAG_OBJECT")
    if not _SHA256.match(protocol_sha256):
        raise AuthorizationError("INVALID_PROTOCOL_SHA256")
    if not _GIT_COMMIT.match(runtime_commit):
        raise AuthorizationError("INVALID_RUNTIME_COMMIT")
    return AuthorizationRecord(
        authorization_id=authorization_id,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
        runtime_commit=runtime_commit,
        created_at=datetime.now(UTC).isoformat(),
        status="ACTIVE",
    )


def write_manifest(path: Path, record: AuthorizationRecord) -> Path:
    """Atomically write the owner-only manifest with 0600 permissions.

    The manifest path must live under a git-ignored location (the caller chooses
    an evidence/runtime directory already excluded by ``.gitignore``). No
    credential value is ever written.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = record.to_json()
    tmp: Path | None = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".authorization-", suffix=".json", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    return path


def read_manifest(path: Path) -> AuthorizationRecord:
    """Read and validate an owner-only manifest from disk.

    Refuses symlinks, requires 0600 permissions, and validates the schema. A
    missing, malformed, stale, or unknown authorization fails closed.
    """

    path = Path(path)
    if not path.exists():
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MISSING")
    if path.is_symlink():
        raise AuthorizationError("AUTHORIZATION_MANIFEST_IS_SYMLINK")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_NOT_REGULAR")
        if metadata.st_uid != os.getuid():
            raise AuthorizationError("AUTHORIZATION_MANIFEST_OWNER_MISMATCH")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AuthorizationError("AUTHORIZATION_MANIFEST_INSECURE_PERMISSIONS")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            raw = handle.read()
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MALFORMED_JSON") from exc
    if not isinstance(data, dict):
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MALFORMED_JSON")
    record = AuthorizationRecord.from_mapping(data)
    if record.status not in {"ACTIVE", "CONSUMED", "RECOVERY"}:
        raise AuthorizationError(f"AUTHORIZATION_MANIFEST_INVALID_STATUS:{record.status}")
    return record


def validate_authorization_for_runtime(
    record: AuthorizationRecord,
    *,
    authorization_id: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    runtime_commit: str,
) -> AuthorizationRecord:
    """Prove the authorization binds to the exact current protocol/runtime.

    A mismatch on any frozen identity, an unknown/stale ID, a replay across a
    different protocol/runtime, or a non-ACTIVE status fails closed. The record
    is returned unchanged; the caller (supervisor) is responsible for marking it
    CONSUMED after the lifecycle completes.
    """

    _validate_authorization_binding(
        record,
        authorization_id=authorization_id,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
        runtime_commit=runtime_commit,
    )
    if record.status == "CONSUMED":
        raise AuthorizationError("AUTHORIZATION_ALREADY_CONSUMED")
    if record.status == "RECOVERY":
        raise AuthorizationError("AUTHORIZATION_RECOVERY_ONLY")
    if record.status != "ACTIVE":
        raise AuthorizationError(f"AUTHORIZATION_INVALID_STATUS:{record.status}")
    return record


def validate_recovery_authorization_for_runtime(
    record: AuthorizationRecord,
    *,
    authorization_id: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    runtime_commit: str,
) -> AuthorizationRecord:
    """Validate the same exact binding for a strictly recovery-only session.

    Recovery validation never changes the record back to ``ACTIVE`` and never
    grants a normal create capability.  A ``RECOVERY`` record intentionally
    remains reusable after a recovery child crashes; the execution journal's
    generation gate separately guarantees that only one credential-bearing
    generation is active at a time.
    """

    _validate_authorization_binding(
        record,
        authorization_id=authorization_id,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
        runtime_commit=runtime_commit,
    )
    if record.status != "RECOVERY":
        raise AuthorizationError(f"AUTHORIZATION_NOT_RECOVERY:{record.status}")
    return record


def _validate_authorization_binding(
    record: AuthorizationRecord,
    *,
    authorization_id: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    runtime_commit: str,
) -> None:
    if record.authorization_id != authorization_id:
        raise AuthorizationError("AUTHORIZATION_ID_MISMATCH")
    if record.protocol_commit != protocol_commit:
        raise AuthorizationError("AUTHORIZATION_PROTOCOL_REPLAY")
    if record.protocol_tag_object != protocol_tag_object:
        raise AuthorizationError("AUTHORIZATION_PROTOCOL_TAG_REPLAY")
    if record.protocol_sha256 != protocol_sha256:
        raise AuthorizationError("AUTHORIZATION_PROTOCOL_HASH_REPLAY")
    if record.runtime_commit != runtime_commit:
        raise AuthorizationError("AUTHORIZATION_RUNTIME_REPLAY")


def mark_consumed(path: Path, record: AuthorizationRecord) -> AuthorizationRecord:
    """Persist the CONSUMED status so the authorization cannot be reused."""

    consumed = AuthorizationRecord(
        authorization_id=record.authorization_id,
        protocol_commit=record.protocol_commit,
        protocol_tag_object=record.protocol_tag_object,
        protocol_sha256=record.protocol_sha256,
        runtime_commit=record.runtime_commit,
        created_at=record.created_at,
        status="CONSUMED",
    )
    write_manifest(path, consumed)
    return consumed


def mark_recovery(path: Path, record: AuthorizationRecord) -> AuthorizationRecord:
    """Persist the one-way ``CONSUMED`` to ``RECOVERY`` transition.

    This transition is used only after durable mutation evidence requires a
    later cleanup session.  It cannot mint a new authorization ID and cannot
    restore ``ACTIVE``/CREATE authority.
    """

    if record.status != "CONSUMED":
        raise AuthorizationError(f"RECOVERY_REQUIRES_CONSUMED:{record.status}")
    current = read_manifest(path)
    if current != record:
        raise AuthorizationError("RECOVERY_RECORD_MISMATCH")
    recovery = AuthorizationRecord(
        authorization_id=record.authorization_id,
        protocol_commit=record.protocol_commit,
        protocol_tag_object=record.protocol_tag_object,
        protocol_sha256=record.protocol_sha256,
        runtime_commit=record.runtime_commit,
        created_at=record.created_at,
        status="RECOVERY",
    )
    write_manifest(path, recovery)
    return recovery


# ---------------------------------------------------------------------------
# Atomic claim (protocol section 9 one-time authorization).
#
# ``read_manifest`` + ``validate_authorization_for_runtime`` + ``mark_consumed``
# is a three-step read-check-write sequence.  Two concurrent processes racing on
# the same authorization ID can both read ACTIVE, both validate, and both enter
# the credential-bearing lifecycle before either writes CONSUMED.
#
# ``claim_authorization`` replaces the three steps with a single atomic
# operation: it acquires an exclusive lock, reads the manifest, validates it,
# asserts ACTIVE, atomically writes CONSUMED, and releases the lock.  Only one
# caller can hold the lock at a time, so at most one caller enters the
# credential-bearing path.
#
# The lock is implemented via ``os.O_CREAT | os.O_EXCL`` on a sibling ``.lock``
# file, which is atomic on all POSIX and macOS local filesystems (APFS, HFS+,
# ext4, tmpfs).  A stale lock left by a crashed process is not recovered
# automatically — the caller handles this by removing the lock file only after
# a successful claim or in the ``finally`` block.
# ---------------------------------------------------------------------------


def claim_authorization(
    path: Path,
    *,
    authorization_id: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    runtime_commit: str,
) -> AuthorizationRecord:
    """Atomically claim an ACTIVE authorization and mark it CONSUMED.

    Two concurrent processes with the same ``authorization_id`` cannot both
    succeed — at most one will claim the authorization and enter the
    credential-bearing lifecycle.  The loser fails closed *before* any
    credential input or network/mutation activity.

    Returns the consumed ``AuthorizationRecord`` on success.  Raises
    ``AuthorizationError`` on any failure (missing manifest, stale,
    already-consumed, protocol/runtime mismatch, concurrent claim conflict).
    """

    path = Path(path)
    lock_path = Path(str(path) + ".lock")

    # --- atomic lock acquisition ------------------------------------------
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(lock_fd)
    except FileExistsError:
        raise AuthorizationError(
            f"AUTHORIZATION_CONCURRENT_CLAIM_CONFLICT:{authorization_id}"
        ) from None

    try:
        # --- read + validate (under lock) ---------------------------------
        record = read_manifest(path)
        validate_authorization_for_runtime(
            record,
            authorization_id=authorization_id,
            protocol_commit=protocol_commit,
            protocol_tag_object=protocol_tag_object,
            protocol_sha256=protocol_sha256,
            runtime_commit=runtime_commit,
        )
        if record.status != "ACTIVE":
            raise AuthorizationError(f"AUTHORIZATION_NOT_ACTIVE:{record.status}:{authorization_id}")
        # --- atomically write CONSUMED ------------------------------------
        return mark_consumed(path, record)
    finally:
        _remove_lock(lock_path)


def _remove_lock(lock_path: Path) -> None:
    with suppress(OSError):
        lock_path.unlink(missing_ok=True)
