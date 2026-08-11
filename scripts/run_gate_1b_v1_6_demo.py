#!/usr/bin/env python3
"""Credential-free front door for the frozen Gate 1B v1.6 process boundary.

Runtime binding and independent-review admission happen before any one-time
authorization claim.  Credential mode and key-file selection stay inside the
fixed credential child and are intentionally absent from this command line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

# Ensure ``src`` is importable when run as a plain script (matches pyproject
# ``pythonpath = ["src"]`` without requiring an installed package).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from global_quant.gate1b.authorization import (  # noqa: E402
    AuthorizationError,
    AuthorizationRecord,
    AuthorizationRegistry,
    claim_authorization,
    is_valid_authorization_id,
    mark_recovery,
    read_manifest,
    validate_recovery_authorization_for_runtime,
)
from global_quant.gate1b.execution_evidence_log import ExecutionEvidenceLog  # noqa: E402
from global_quant.gate1b.execution_journal import (  # noqa: E402
    ExecutionJournal,
    SessionAuthority,
)
from global_quant.gate1b.execution_projection import ExecutionProjector  # noqa: E402
from global_quant.gate1b.final_evidence import FinalEvidenceFinalizer  # noqa: E402
from global_quant.gate1b.process_boundary import (  # noqa: E402
    CredentialProcessSupervisor,
    CredentialWorkload,
    ProcessLifecycleJournal,
)
from global_quant.gate1b.review_artifact import (  # noqa: E402
    ReviewArtifactError,
    is_reviewed_for_runtime,
)
from global_quant.gate1b.runtime_binding import (  # noqa: E402
    RuntimeBindingError,
    RuntimeSnapshot,
    verify_runtime_binding,
)
from global_quant.gate1b.safety import (  # noqa: E402
    CONFLICTING_CREDENTIAL_NAMES,
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
)
from global_quant.gate1b.supervisor import (  # noqa: E402
    ExecutionCompletion,
    ExecutionSupervisor,
    SupervisorError,
)

READY_FOR_REVIEW = "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW"
_ENTRYPOINT_PATH = Path(__file__).resolve()
_CREDENTIAL_CHILD_PATH = _PROJECT_ROOT / "src" / "global_quant" / "gate1b" / "credential_session.py"
_REQUIRED_RUNTIME_PATHS = (_ENTRYPOINT_PATH, _CREDENTIAL_CHILD_PATH)
_RUNTIME_EVIDENCE_ROOT = _PROJECT_ROOT / "evidence" / "runtime"
_CREDENTIAL_CHILD_SHA256 = "c0532c9dbc068b42337823504a8c9f4482d60def406f0fd53e57af7884d0331b"
_LIFECYCLE_LIMIT_SECONDS = 180.0
_ALLOWED_CHILD_ENVIRONMENT_NAMES = frozenset(
    {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
)
_CREDENTIAL_ENVIRONMENT_NAMES = frozenset(
    (DEMO_KEY_NAME, DEMO_SECRET_NAME, *CONFLICTING_CREDENTIAL_NAMES)
)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SESSION_NONCE = re.compile(r"[0-9a-f]{16}\Z")


def _open_owner_only_directory(path: Path) -> tuple[Path, int, os.stat_result]:
    """Open one owner-only directory and bind its lexical path to one inode."""

    directory = Path(os.path.abspath(path))
    try:
        before = directory.stat(follow_symlinks=False)
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700, follow_symlinks=False)
        before = directory.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        raise RuntimeError("EVIDENCE_DIRECTORY_NOT_OWNER_ONLY")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise RuntimeError("EVIDENCE_DIRECTORY_UNAVAILABLE") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise RuntimeError("EVIDENCE_DIRECTORY_NOT_OWNER_ONLY")
    return directory, descriptor, opened


def _prepare_owner_only_directory(path: Path) -> Path:
    """Create one lexical evidence directory and prove it is owner-only."""

    directory, descriptor, _metadata = _open_owner_only_directory(path)
    os.close(descriptor)
    return directory


def _assert_directory_path_identity(directory: Path, expected: os.stat_result) -> None:
    try:
        observed = directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("EVIDENCE_DIRECTORY_PATH_RACE") from exc
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeError("EVIDENCE_DIRECTORY_PATH_RACE")


def _create_owner_only_temporary(parent_fd: int, artifact_name: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(16):
        temporary_name = f".{artifact_name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError("EVIDENCE_TEMPORARY_CREATE_FAILED") from exc
        return descriptor, temporary_name
    raise RuntimeError("EVIDENCE_TEMPORARY_CREATE_FAILED")


def _atomic_write_owner_only_create_once(path: Path, encoded: bytes) -> Path:
    """Publish one fsynced 0600 file without an overwrite window."""

    path = Path(os.path.abspath(path))
    directory, directory_fd, directory_metadata = _open_owner_only_directory(path.parent)
    if path.parent != directory or path.name in {"", ".", ".."}:
        os.close(directory_fd)
        raise RuntimeError("EVIDENCE_PATH_INVALID")
    temporary_fd = -1
    temporary_name: str | None = None
    published = False
    retained = False
    try:
        temporary_fd, temporary_name = _create_owner_only_temporary(
            directory_fd,
            path.name,
        )
        os.fchmod(temporary_fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise RuntimeError("EVIDENCE_WRITE_FAILED")
            view = view[written:]
        os.fsync(temporary_fd)
        temporary_metadata = os.fstat(temporary_fd)
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError as exc:
            raise RuntimeError("EVIDENCE_ARTIFACT_ALREADY_EXISTS") from exc
        except OSError as exc:
            raise RuntimeError("EVIDENCE_PUBLICATION_FAILED") from exc
        published_metadata = os.stat(
            path.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (temporary_metadata.st_dev, temporary_metadata.st_ino) != (
            published_metadata.st_dev,
            published_metadata.st_ino,
        ):
            raise RuntimeError("EVIDENCE_TEMPORARY_INODE_CHANGED")
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        metadata = os.stat(
            path.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino)
            != (temporary_metadata.st_dev, temporary_metadata.st_ino)
        ):
            raise RuntimeError("EVIDENCE_ARTIFACT_NOT_OWNER_ONLY")
        os.fsync(directory_fd)
        _assert_directory_path_identity(directory, directory_metadata)
        retained = True
        return path
    finally:
        if published and not retained:
            with suppress(FileNotFoundError):
                os.unlink(path.name, dir_fd=directory_fd)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        if not retained:
            with suppress(OSError):
                os.fsync(directory_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        os.close(directory_fd)


def _write_evidence(evidence_dir: Path, payload: dict[str, Any]) -> Path:
    evidence_dir = _prepare_owner_only_directory(Path(evidence_dir))
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return _atomic_write_owner_only_create_once(
        evidence_dir / "cli-verdict.json",
        encoded,
    )


def _write_authorization_evidence(
    evidence_dir: Path,
    record: AuthorizationRecord,
) -> Path:
    """Retain the exact post-claim non-secret authorization state once."""

    if type(record) is not AuthorizationRecord or record.status not in {
        "CONSUMED",
        "RECOVERY",
    }:
        raise RuntimeError("CLAIMED_AUTHORIZATION_RECORD_REQUIRED")
    evidence_dir = _prepare_owner_only_directory(Path(evidence_dir))
    return _atomic_write_owner_only_create_once(
        evidence_dir / "authorization.json",
        record.to_json().encode("ascii"),
    )


def _base_payload(
    *,
    status: str,
    reason_codes: list[str],
    binding: dict[str, str],
    credential_environment_empty: bool = True,
) -> dict[str, Any]:
    return {
        "gate": "NT-GATE-1B",
        "protocol_version": "1.6",
        "mode": "CREDENTIAL_BEARING_DEMO_CLI",
        "status": status,
        "reason_codes": reason_codes,
        "credential_environment_empty": credential_environment_empty,
        "authorization_claimed": False,
        "credential_child_started": False,
        "credentials_read": False,
        "network_accessed": False,
        "authenticated_request_sent": False,
        "order_summary": {"canceled": 0, "filled": 0, "submitted": 0},
        "economic_event_summary": {"fees": 0, "funding": 0},
        "position_changes": 0,
        "agent_credential_access_allowed": False,
        "next_action": status,
        **binding,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the NT-GATE-1B v1.6 credential-bearing Demo lifecycle CLI."
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--session-nonce", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--protocol-commit", required=True)
    parser.add_argument("--protocol-tag-object", required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument(
        "--authorization-manifest",
        type=Path,
        required=True,
        help="Owner-only local authorization manifest path (git-ignored).",
    )
    parser.add_argument(
        "--review-artifact",
        type=Path,
        default=None,
        help="Independent review artifact path. If absent, the CLI stops at "
        "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW.",
    )
    return parser.parse_args(argv)


def _snapshot_binding(snapshot: RuntimeSnapshot) -> dict[str, str]:
    return {
        "runtime_commit": snapshot.runtime_commit,
        "runtime_tree": snapshot.runtime_tree,
        "runtime_branch": snapshot.branch,
        "protocol_commit": snapshot.protocol_commit,
        "protocol_tag_object": snapshot.protocol_tag_object,
        "protocol_sha256": snapshot.protocol_sha256,
    }


def _canonical_runtime_paths(
    *,
    runtime_commit: str,
    session_nonce: str,
    authorization_id: str,
) -> tuple[Path, Path]:
    if (
        type(runtime_commit) is not str
        or _GIT_COMMIT.fullmatch(runtime_commit) is None
        or type(session_nonce) is not str
        or _SESSION_NONCE.fullmatch(session_nonce) is None
        or not is_valid_authorization_id(authorization_id)
    ):
        raise RuntimeError("CANONICAL_RUNTIME_LAYOUT_REQUIRED")
    runtime_root = Path(os.path.abspath(_RUNTIME_EVIDENCE_ROOT))
    evidence_dir = runtime_root / f"gate1b-v1.6-mutation-{runtime_commit[:12]}" / session_nonce
    authorization_manifest = (
        runtime_root / "gate1b-v1.6-authorizations" / f"{authorization_id}.json"
    )
    return evidence_dir, authorization_manifest


def _sanitized_child_environment() -> dict[str, str]:
    """Copy only non-credential process settings needed by the fixed child."""

    return {
        name: os.environ[name] for name in _ALLOWED_CHILD_ENVIRONMENT_NAMES if name in os.environ
    }


def _require_recovery_artifacts(evidence_root: Path) -> None:
    """Reject recovery before constructors could create missing durable state."""

    required = (
        "request-ledger.json",
        "request-ledger.json.head",
        "lifecycle.jsonl",
        "lifecycle.jsonl.head",
        "requests.jsonl",
    )
    for name in required:
        try:
            entry = (evidence_root / name).stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("RECOVERY_DURABLE_STATE_REQUIRED") from exc
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise RuntimeError("RECOVERY_DURABLE_STATE_REQUIRED")


def _build_execution_supervisor(
    *,
    evidence_root: Path,
    runtime_snapshot: RuntimeSnapshot,
    recovery: bool,
) -> ExecutionSupervisor:
    """Compose the one canonical same-root supervisor without driving it."""

    evidence_root = _prepare_owner_only_directory(evidence_root)
    execution_path = evidence_root / "request-ledger.json"
    lifecycle_path = evidence_root / "lifecycle.jsonl"
    requests_path = evidence_root / "requests.jsonl"
    if recovery:
        _require_recovery_artifacts(evidence_root)
        execution_journal = ExecutionJournal(execution_path)
        process_journal = ProcessLifecycleJournal.restore(lifecycle_path)
    else:
        lifecycle_started_at = time.monotonic()
        lifecycle_deadline = lifecycle_started_at + _LIFECYCLE_LIMIT_SECONDS
        execution_journal = ExecutionJournal(execution_path)
        process_journal = ProcessLifecycleJournal.start(
            lifecycle_path,
            lifecycle_started_at=lifecycle_started_at,
            lifecycle_deadline=lifecycle_deadline,
            execution_journal_path=execution_journal.path,
        )
    sanitized_environment = _sanitized_child_environment()
    process_supervisor = CredentialProcessSupervisor(
        lifecycle_journal=process_journal,
        execution_journal=execution_journal,
        parent_environment=sanitized_environment,
        credential_stdin=None,
    )
    evidence_log = ExecutionEvidenceLog(
        requests_path,
        execution_journal_path=execution_journal.path,
        credential_canaries=(),
    )
    projector = ExecutionProjector(
        runtime_snapshot=runtime_snapshot,
        execution_journal=execution_journal,
        process_journal=process_journal,
    )
    finalizer = FinalEvidenceFinalizer(
        root=evidence_root,
        execution_journal_path=execution_journal.path,
        supervisor_environment=sanitized_environment,
        canary_tokens=(),
    )
    workload = CredentialWorkload.production(
        _CREDENTIAL_CHILD_PATH,
        runtime_sha256=_CREDENTIAL_CHILD_SHA256,
    )
    return ExecutionSupervisor.production(
        workload=workload,
        process_supervisor=process_supervisor,
        execution_journal=execution_journal,
        process_lifecycle_journal=process_journal,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=finalizer,
    )


def _authorization_binding(args: argparse.Namespace) -> dict[str, str]:
    return {
        "authorization_id": args.authorization_id,
        "protocol_commit": args.protocol_commit,
        "protocol_tag_object": args.protocol_tag_object,
        "protocol_sha256": args.protocol_sha256,
        "runtime_commit": args.runtime_commit,
    }


def _validate_recovery_authorization_evidence(
    evidence_dir: Path,
    source: AuthorizationRecord,
) -> AuthorizationRecord:
    retained = read_manifest(evidence_dir / "authorization.json")
    if (
        retained.status not in {"CONSUMED", "RECOVERY"}
        or retained.authorization_id != source.authorization_id
        or retained.protocol_commit != source.protocol_commit
        or retained.protocol_tag_object != source.protocol_tag_object
        or retained.protocol_sha256 != source.protocol_sha256
        or retained.runtime_commit != source.runtime_commit
        or retained.created_at != source.created_at
    ):
        raise AuthorizationError("RECOVERY_AUTHORIZATION_EVIDENCE_MISMATCH")
    return retained


def _emit_post_handoff_stop(evidence_dir: Path, *, reason: str) -> int:
    """Print only sanitized state; final evidence remains supervisor-owned."""

    payload: dict[str, object] = {"exit_code": 1, "reason": reason}
    verdict_path = evidence_dir / "verdict.json"
    try:
        entry = verdict_path.stat(follow_symlinks=False)
    except OSError:
        pass
    else:
        if (
            stat.S_ISREG(entry.st_mode)
            and entry.st_uid == os.geteuid()
            and stat.S_IMODE(entry.st_mode) == 0o600
            and entry.st_nlink == 1
        ):
            payload["evidence"] = str(verdict_path)
    print(json.dumps(payload, sort_keys=True))
    return 1


def _emit_execution_completion(completion: ExecutionCompletion) -> int:
    eligible = completion.final_evidence_eligible is True
    status = (
        "EXECUTION_COMPLETE_READY_FOR_INDEPENDENT_REVIEW"
        if eligible
        else "EXECUTION_COMPLETE_BLOCKED"
    )
    exit_code = 0 if eligible else 1
    print(
        json.dumps(
            {
                "evidence": str(completion.finalized_evidence.verdict_path),
                "exit_code": exit_code,
                "status": status,
            },
            sort_keys=True,
        )
    )
    return exit_code


def _emit_stop(
    evidence_dir: Path,
    *,
    reason: str,
    binding: dict[str, str],
    credential_environment_empty: bool = True,
) -> int:
    path = _write_evidence(
        evidence_dir,
        _base_payload(
            status="STOP",
            reason_codes=[reason],
            binding=binding,
            credential_environment_empty=credential_environment_empty,
        ),
    )
    print(json.dumps({"exit_code": 1, "evidence": str(path), "reason": reason}))
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    evidence_dir = Path(os.path.abspath(args.evidence_dir))
    binding = {
        "runtime_commit": args.runtime_commit,
        "session_nonce": args.session_nonce,
        "authorization_id": args.authorization_id,
        "protocol_commit": args.protocol_commit,
        "protocol_tag_object": args.protocol_tag_object,
        "protocol_sha256": args.protocol_sha256,
    }

    # Inspect names only.  Credential values must never be read or projected by
    # this control-plane process, even on a fail-closed path.
    if any(name in os.environ for name in _CREDENTIAL_ENVIRONMENT_NAMES):
        print(
            json.dumps(
                {
                    "exit_code": 1,
                    "reason": "SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT",
                }
            )
        )
        return 1

    try:
        canonical_evidence_dir, canonical_manifest = _canonical_runtime_paths(
            runtime_commit=args.runtime_commit,
            session_nonce=args.session_nonce,
            authorization_id=args.authorization_id,
        )
    except RuntimeError:
        canonical_evidence_dir = Path()
        canonical_manifest = Path()
    supplied_manifest = Path(os.path.abspath(args.authorization_manifest))
    if evidence_dir != canonical_evidence_dir or supplied_manifest != canonical_manifest:
        print(
            json.dumps(
                {
                    "exit_code": 1,
                    "reason": "CANONICAL_RUNTIME_LAYOUT_REQUIRED",
                }
            )
        )
        return 1

    # The complete committed-runtime proof precedes review and authorization.
    try:
        snapshot = verify_runtime_binding(
            _PROJECT_ROOT,
            expected_runtime_commit=args.runtime_commit,
            expected_protocol_commit=args.protocol_commit,
            expected_protocol_tag_object=args.protocol_tag_object,
            expected_protocol_sha256=args.protocol_sha256,
            required_source_paths=_REQUIRED_RUNTIME_PATHS,
        )
    except RuntimeBindingError as exc:
        return _emit_stop(evidence_dir, reason=str(exc), binding=binding)
    binding.update(_snapshot_binding(snapshot))

    review_path = Path(args.review_artifact) if args.review_artifact else None
    try:
        reviewed = bool(
            review_path is not None
            and is_reviewed_for_runtime(
                review_path,
                runtime_commit=snapshot.runtime_commit,
                protocol_commit=snapshot.protocol_commit,
                protocol_tag_object=snapshot.protocol_tag_object,
                protocol_sha256=snapshot.protocol_sha256,
            )
        )
    except ReviewArtifactError:
        return _emit_stop(
            evidence_dir,
            reason="REVIEW_ARTIFACT_INVALID",
            binding=binding,
        )

    if not reviewed:
        path = _write_evidence(
            evidence_dir,
            _base_payload(
                status=READY_FOR_REVIEW,
                reason_codes=["INDEPENDENT_REVIEW_ARTIFACT_ABSENT"],
                binding=binding,
            ),
        )
        print(json.dumps({"exit_code": 0, "evidence": str(path), "status": READY_FOR_REVIEW}))
        return 0

    # The canonical retained registry is the only authorization state
    # authority.  From this point onward, fail-closed output is stdout-only: a
    # concurrent or prior claim may already own this exact session directory.
    try:
        registry = AuthorizationRegistry(_RUNTIME_EVIDENCE_ROOT)
        manifest_path = registry.manifest_path(args.authorization_id)
        if supplied_manifest != manifest_path:
            raise AuthorizationError("AUTHORIZATION_MANIFEST_PATH_INVALID")
        source_record = read_manifest(manifest_path)
    except AuthorizationError as exc:
        return _emit_post_handoff_stop(evidence_dir, reason=str(exc))

    authority_binding = _authorization_binding(args)
    if source_record.status == "ACTIVE":
        if evidence_dir.exists():
            return _emit_post_handoff_stop(
                evidence_dir,
                reason="PRIMARY_EVIDENCE_SESSION_ALREADY_EXISTS",
            )
        try:
            consumed = claim_authorization(manifest_path, **authority_binding)
        except AuthorizationError as exc:
            return _emit_post_handoff_stop(evidence_dir, reason=str(exc))
        try:
            _write_authorization_evidence(evidence_dir, consumed)
        except (OSError, RuntimeError):
            return _emit_post_handoff_stop(
                evidence_dir,
                reason="AUTHORIZATION_EVIDENCE_DURABILITY_FAILED",
            )
        try:
            authority = SessionAuthority.build(
                authorization_id=consumed.authorization_id,
                runtime_commit=consumed.runtime_commit,
                session_nonce=args.session_nonce,
                generation=1,
            )
            controller = _build_execution_supervisor(
                evidence_root=evidence_dir,
                runtime_snapshot=snapshot,
                recovery=False,
            )
        except Exception:
            return _emit_post_handoff_stop(
                evidence_dir,
                reason="EXECUTION_COMPONENT_ADMISSION_FAILED",
            )
        try:
            completion = controller.execute_primary(authority=authority)
        except SupervisorError as exc:
            try:
                mark_recovery(manifest_path, consumed)
            except AuthorizationError:
                return _emit_post_handoff_stop(
                    evidence_dir,
                    reason="RECOVERY_AUTHORIZATION_TRANSITION_FAILED",
                )
            return _emit_post_handoff_stop(evidence_dir, reason=exc.reason)
        return _emit_execution_completion(completion)

    if source_record.status == "RECOVERY":
        try:
            recovery_record = validate_recovery_authorization_for_runtime(
                source_record,
                **authority_binding,
            )
            _validate_recovery_authorization_evidence(
                evidence_dir,
                recovery_record,
            )
            primary_authority = SessionAuthority.build(
                authorization_id=recovery_record.authorization_id,
                runtime_commit=recovery_record.runtime_commit,
                session_nonce=args.session_nonce,
                generation=1,
            )
            controller = _build_execution_supervisor(
                evidence_root=evidence_dir,
                runtime_snapshot=snapshot,
                recovery=True,
            )
        except Exception:
            return _emit_post_handoff_stop(
                evidence_dir,
                reason="RECOVERY_ADMISSION_FAILED",
            )
        try:
            completion = controller.execute_recovery(
                primary_authority=primary_authority,
            )
        except SupervisorError as exc:
            return _emit_post_handoff_stop(evidence_dir, reason=exc.reason)
        return _emit_execution_completion(completion)

    return _emit_post_handoff_stop(
        evidence_dir,
        reason="AUTHORIZATION_ALREADY_CONSUMED",
    )


if __name__ == "__main__":
    raise SystemExit(main())
