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
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_AUTHORIZATION_ID = re.compile(r"g1b16-[0-9a-f]{16}\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TAG_OBJECT = re.compile(r"[0-9a-f]{40}\Z")


class AuthorizationError(RuntimeError):
    """Raised when a one-time authorization cannot be proven or is replayed."""


@dataclass(frozen=True, slots=True)
class AuthorizationRecord:
    authorization_id: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str
    runtime_commit: str
    created_at: str
    status: str  # "ACTIVE" | "CONSUMED" | "RECOVERY"

    def __post_init__(self) -> None:
        values = (
            self.authorization_id,
            self.protocol_commit,
            self.protocol_tag_object,
            self.protocol_sha256,
            self.runtime_commit,
            self.created_at,
            self.status,
        )
        if any(type(value) is not str for value in values):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_FIELD_TYPE")
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise AuthorizationError("AUTHORIZATION_CREATED_AT_INVALID") from exc
        if (
            _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
            or _GIT_COMMIT.fullmatch(self.protocol_commit) is None
            or _TAG_OBJECT.fullmatch(self.protocol_tag_object) is None
            or _SHA256.fullmatch(self.protocol_sha256) is None
            or _GIT_COMMIT.fullmatch(self.runtime_commit) is None
            or created.utcoffset() is None
            or created.utcoffset().total_seconds() != 0
            or created.astimezone(UTC).isoformat() != self.created_at
            or self.status not in {"ACTIVE", "CONSUMED", "RECOVERY"}
        ):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_FIELD_INVALID")

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
        if set(data) != set(required):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_FIELDS")
        if any(type(data[field]) is not str for field in required):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_FIELD_TYPE")
        return cls(
            authorization_id=data["authorization_id"],
            protocol_commit=data["protocol_commit"],
            protocol_tag_object=data["protocol_tag_object"],
            protocol_sha256=data["protocol_sha256"],
            runtime_commit=data["runtime_commit"],
            created_at=data["created_at"],
            status=data["status"],
        )


@dataclass(frozen=True, slots=True)
class _ManifestDirectory:
    path: Path
    descriptor: int
    metadata: os.stat_result
    manifest_name: str
    lock_name: str


def is_valid_authorization_id(value: str) -> bool:
    return type(value) is str and _AUTHORIZATION_ID.fullmatch(value) is not None


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
    if type(protocol_commit) is not str or _GIT_COMMIT.fullmatch(protocol_commit) is None:
        raise AuthorizationError("INVALID_PROTOCOL_COMMIT")
    if type(protocol_tag_object) is not str or _TAG_OBJECT.fullmatch(protocol_tag_object) is None:
        raise AuthorizationError("INVALID_PROTOCOL_TAG_OBJECT")
    if type(protocol_sha256) is not str or _SHA256.fullmatch(protocol_sha256) is None:
        raise AuthorizationError("INVALID_PROTOCOL_SHA256")
    if type(runtime_commit) is not str or _GIT_COMMIT.fullmatch(runtime_commit) is None:
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


def _write_manifest_locked(
    path: Path,
    record: AuthorizationRecord,
    directory: _ManifestDirectory,
) -> Path:
    """Publish one already-authorized state while the shared lock is held."""

    return _publish_manifest(path, record, directory=directory, create_only=False)


def _open_owner_directory(path: Path) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthorizationError("AUTHORIZATION_DIRECTORY_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise AuthorizationError("AUTHORIZATION_DIRECTORY_NOT_OWNER_ONLY")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AuthorizationError("AUTHORIZATION_DIRECTORY_PATH_RACE")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _assert_directory_path_identity(path: Path, expected: os.stat_result) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AuthorizationError("AUTHORIZATION_DIRECTORY_PATH_RACE") from exc
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise AuthorizationError("AUTHORIZATION_DIRECTORY_PATH_RACE")


def _create_temporary(parent_fd: int) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(16):
        name = f".authorization-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_TEMPORARY_CREATE_FAILED") from exc
        return descriptor, name
    raise AuthorizationError("AUTHORIZATION_TEMPORARY_CREATE_FAILED")


def _publish_manifest(
    path: Path,
    record: AuthorizationRecord,
    *,
    directory: _ManifestDirectory,
    create_only: bool,
) -> Path:
    """Publish through one held owner-only dirfd and verify exact inode durability."""

    if type(record) is not AuthorizationRecord:
        raise AuthorizationError("AUTHORIZATION_RECORD_REQUIRED")
    if type(directory) is not _ManifestDirectory:
        raise AuthorizationError("AUTHORIZATION_DIRECTORY_AUTHORITY_REQUIRED")
    path = Path(path)
    if (
        path.name in {"", ".", ".."}
        or path.parent != directory.path
        or path.name != directory.manifest_name
    ):
        raise AuthorizationError("AUTHORIZATION_MANIFEST_PATH_INVALID")
    encoded = record.to_json().encode("ascii")
    parent_fd = directory.descriptor
    parent_stat = directory.metadata
    temporary_fd = -1
    temporary_name: str | None = None
    try:
        temporary_fd, temporary_name = _create_temporary(parent_fd)
        try:
            os.fchmod(temporary_fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise AuthorizationError("AUTHORIZATION_WRITE_FAILED")
                view = view[written:]
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
        except AuthorizationError:
            raise
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_WRITE_FAILED") from exc
        try:
            if create_only:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            else:
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
        except FileExistsError:
            raise AuthorizationError("AUTHORIZATION_MANIFEST_EXISTS") from None
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_PUBLICATION_FAILED") from exc
        try:
            published_stat = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_PUBLICATION_FAILED") from exc
        if (temporary_stat.st_dev, temporary_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ):
            raise AuthorizationError("AUTHORIZATION_TEMPORARY_INODE_CHANGED")
        if temporary_name is not None:
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_name = None
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_DIRECTORY_FSYNC_FAILED") from exc
        _assert_directory_path_identity(path.parent, parent_stat)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
    return path


@contextmanager
def _manifest_lock(
    path: Path,
    *,
    conflict_reason: str,
) -> Iterator[_ManifestDirectory]:
    path = Path(path)
    if path.name in {"", ".", ".."}:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_PATH_INVALID")
    directory_fd, directory_stat = _open_owner_directory(path.parent)
    lock_name = f"{path.name}.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError:
        os.close(directory_fd)
        raise AuthorizationError(conflict_reason) from None
    except OSError as exc:
        os.close(directory_fd)
        raise AuthorizationError("AUTHORIZATION_LOCK_FAILED") from exc
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _assert_directory_path_identity(path.parent, directory_stat)
        yield _ManifestDirectory(
            path=path.parent,
            descriptor=directory_fd,
            metadata=directory_stat,
            manifest_name=path.name,
            lock_name=lock_name,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(lock_name, dir_fd=directory_fd)
        with suppress(OSError):
            os.fsync(directory_fd)
        try:
            _assert_directory_path_identity(path.parent, directory_stat)
        finally:
            os.close(directory_fd)


def write_manifest(path: Path, record: AuthorizationRecord) -> Path:
    """Create the sole ACTIVE manifest; this API can never overwrite state."""

    path = Path(path)
    if type(record) is not AuthorizationRecord:
        raise AuthorizationError("AUTHORIZATION_RECORD_REQUIRED")
    if record.status != "ACTIVE":
        raise AuthorizationError("AUTHORIZATION_INITIAL_STATE_MUST_BE_ACTIVE")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _manifest_lock(
        path,
        conflict_reason="AUTHORIZATION_CONCURRENT_TRANSITION",
    ) as directory:
        try:
            os.stat(path.name, dir_fd=directory.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_DESTINATION_CHECK_FAILED") from exc
        else:
            raise AuthorizationError("AUTHORIZATION_MANIFEST_EXISTS")
        return _publish_manifest(
            path,
            record,
            directory=directory,
            create_only=True,
        )


class AuthorizationRegistry:
    """Canonical retained-attempt authority under one fixed runtime evidence root."""

    _DIRECTORY_NAME = "gate1b-v1.6-authorizations"
    _MAX_RETAINED_RECORD_BYTES = 64 * 1024

    def __init__(self, runtime_evidence_root: Path) -> None:
        candidate = Path(runtime_evidence_root)
        try:
            if candidate.is_symlink():
                raise AuthorizationError("AUTHORIZATION_REGISTRY_ROOT_INVALID")
            self.runtime_evidence_root = candidate.resolve(strict=True)
            entry = self.runtime_evidence_root.stat(follow_symlinks=False)
        except OSError as exc:
            raise AuthorizationError("AUTHORIZATION_REGISTRY_ROOT_INVALID") from exc
        if (
            not stat.S_ISDIR(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) & 0o077
        ):
            raise AuthorizationError("AUTHORIZATION_REGISTRY_ROOT_INVALID")
        self.root = self.runtime_evidence_root / self._DIRECTORY_NAME
        self.root.mkdir(mode=0o700, exist_ok=True)
        root_entry = self.root.stat(follow_symlinks=False)
        if (
            self.root.is_symlink()
            or not stat.S_ISDIR(root_entry.st_mode)
            or root_entry.st_uid != os.geteuid()
            or stat.S_IMODE(root_entry.st_mode) & 0o077
        ):
            raise AuthorizationError("AUTHORIZATION_REGISTRY_ROOT_INVALID")
        os.chmod(self.root, 0o700)

    def manifest_path(self, authorization_id: str) -> Path:
        if not is_valid_authorization_id(authorization_id):
            raise AuthorizationError("INVALID_AUTHORIZATION_ID_FORMAT")
        return self.root / f"{authorization_id}.json"

    def create(self, record: AuthorizationRecord) -> Path:
        if type(record) is not AuthorizationRecord or record.status != "ACTIVE":
            raise AuthorizationError("AUTHORIZATION_INITIAL_STATE_MUST_BE_ACTIVE")
        if self._retained_authorization_exists(record.authorization_id):
            raise AuthorizationError("AUTHORIZATION_RETAINED_ATTEMPT_EXISTS")
        return write_manifest(self.manifest_path(record.authorization_id), record)

    def _retained_authorization_exists(self, authorization_id: str) -> bool:
        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            decoded_object: dict[str, object] = {}
            for key, value in pairs:
                if key in decoded_object:
                    raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID")
                decoded_object[key] = value
            return decoded_object

        for candidate in self.runtime_evidence_root.rglob("*"):
            if candidate.is_symlink():
                raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID")
            if not candidate.is_file():
                continue
            if not (
                candidate.name in {"authorization.json", "intent.json"}
                or (
                    candidate.parent == self.root
                    and candidate.name.startswith("g1b16-")
                    and candidate.suffix == ".json"
                )
            ):
                continue
            try:
                entry = candidate.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_uid != os.geteuid()
                    or stat.S_IMODE(entry.st_mode) & 0o077
                    or entry.st_size > self._MAX_RETAINED_RECORD_BYTES
                ):
                    raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID")
                raw = candidate.read_bytes()
                decoded = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
            except AuthorizationError:
                raise
            except (OSError, UnicodeError, ValueError) as exc:
                raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID") from exc
            if type(decoded) is not dict:
                raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID")
            retained_id = decoded.get("authorization_id")
            if retained_id is not None and type(retained_id) is not str:
                raise AuthorizationError("AUTHORIZATION_RETAINED_SCAN_INVALID")
            if retained_id == authorization_id:
                return True
        return False


def _read_manifest_from_directory(
    path: Path,
    directory: _ManifestDirectory,
) -> AuthorizationRecord:
    if (
        type(directory) is not _ManifestDirectory
        or path.parent != directory.path
        or path.name != directory.manifest_name
    ):
        raise AuthorizationError("AUTHORIZATION_MANIFEST_PATH_INVALID")
    try:
        before = os.stat(
            path.name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MISSING") from None
    except OSError as exc:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_OPEN_FAILED") from exc
    if stat.S_ISLNK(before.st_mode):
        raise AuthorizationError("AUTHORIZATION_MANIFEST_IS_SYMLINK")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory.descriptor)
    except OSError as exc:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_OPEN_FAILED") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_NOT_REGULAR")
        if metadata.st_uid != os.getuid():
            raise AuthorizationError("AUTHORIZATION_MANIFEST_OWNER_MISMATCH")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AuthorizationError("AUTHORIZATION_MANIFEST_INSECURE_PERMISSIONS")
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise AuthorizationError("AUTHORIZATION_MANIFEST_PATH_RACE")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            raw = handle.read()
    finally:
        os.close(descriptor)
    _assert_directory_path_identity(directory.path, directory.metadata)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise AuthorizationError("AUTHORIZATION_MANIFEST_DUPLICATE_KEY")
            decoded[key] = value
        return decoded

    try:
        data = json.loads(raw, object_pairs_hook=reject_duplicates)
    except AuthorizationError:
        raise
    except ValueError as exc:
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MALFORMED_JSON") from exc
    if not isinstance(data, dict):
        raise AuthorizationError("AUTHORIZATION_MANIFEST_MALFORMED_JSON")
    record = AuthorizationRecord.from_mapping(data)
    if raw != record.to_json():
        raise AuthorizationError("AUTHORIZATION_MANIFEST_NONCANONICAL")
    return record


def read_manifest(path: Path) -> AuthorizationRecord:
    """Read and validate an owner-only manifest through one bound parent dirfd."""

    path = Path(path)
    try:
        parent_fd, parent_stat = _open_owner_directory(path.parent)
    except AuthorizationError as exc:
        if not path.parent.exists():
            raise AuthorizationError("AUTHORIZATION_MANIFEST_MISSING") from exc
        raise
    directory = _ManifestDirectory(
        path=path.parent,
        descriptor=parent_fd,
        metadata=parent_stat,
        manifest_name=path.name,
        lock_name="",
    )
    try:
        return _read_manifest_from_directory(path, directory)
    finally:
        os.close(parent_fd)


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

    if type(record) is not AuthorizationRecord or record.status != "ACTIVE":
        raise AuthorizationError("CONSUMED_REQUIRES_ACTIVE")
    consumed = AuthorizationRecord(
        authorization_id=record.authorization_id,
        protocol_commit=record.protocol_commit,
        protocol_tag_object=record.protocol_tag_object,
        protocol_sha256=record.protocol_sha256,
        runtime_commit=record.runtime_commit,
        created_at=record.created_at,
        status="CONSUMED",
    )
    path = Path(path)
    with _manifest_lock(
        path,
        conflict_reason="AUTHORIZATION_CONCURRENT_TRANSITION",
    ) as directory:
        current = _read_manifest_from_directory(path, directory)
        if current != record:
            raise AuthorizationError("CONSUMED_RECORD_MISMATCH")
        _write_manifest_locked(path, consumed, directory)
    return consumed


def mark_recovery(path: Path, record: AuthorizationRecord) -> AuthorizationRecord:
    """Persist the one-way ``CONSUMED`` to ``RECOVERY`` transition.

    This transition is used only after durable mutation evidence requires a
    later cleanup session.  It cannot mint a new authorization ID and cannot
    restore ``ACTIVE``/CREATE authority.
    """

    if type(record) is not AuthorizationRecord:
        raise AuthorizationError("AUTHORIZATION_RECORD_REQUIRED")
    if record.status != "CONSUMED":
        raise AuthorizationError(f"RECOVERY_REQUIRES_CONSUMED:{record.status}")
    recovery = AuthorizationRecord(
        authorization_id=record.authorization_id,
        protocol_commit=record.protocol_commit,
        protocol_tag_object=record.protocol_tag_object,
        protocol_sha256=record.protocol_sha256,
        runtime_commit=record.runtime_commit,
        created_at=record.created_at,
        status="RECOVERY",
    )
    path = Path(path)
    with _manifest_lock(
        path,
        conflict_reason="AUTHORIZATION_CONCURRENT_TRANSITION",
    ) as directory:
        current = _read_manifest_from_directory(path, directory)
        if current != record:
            raise AuthorizationError("RECOVERY_RECORD_MISMATCH")
        _write_manifest_locked(path, recovery, directory)
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
    with _manifest_lock(
        path,
        conflict_reason=f"AUTHORIZATION_CONCURRENT_CLAIM_CONFLICT:{authorization_id}",
    ) as directory:
        # --- read + validate (under lock) ---------------------------------
        record = _read_manifest_from_directory(path, directory)
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
        consumed = AuthorizationRecord(
            authorization_id=record.authorization_id,
            protocol_commit=record.protocol_commit,
            protocol_tag_object=record.protocol_tag_object,
            protocol_sha256=record.protocol_sha256,
            runtime_commit=record.runtime_commit,
            created_at=record.created_at,
            status="CONSUMED",
        )
        _write_manifest_locked(path, consumed, directory)
        return consumed
