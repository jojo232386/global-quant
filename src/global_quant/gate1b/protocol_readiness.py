"""Credential-free readiness checks for the frozen NT-GATE-1B v1.5 protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_RELATIVE_PATH = Path("protocols/NT_GATE_1B_V1_5.md")
PROTOCOL_TAG = "nt-gate-1b-v1.5-protocol"

# The readiness process inspects names only. It never indexes the environment
# mapping or copies values into memory, output, or evidence.
RECOGNIZED_BINANCE_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_DEMO_API_KEY",
        "BINANCE_DEMO_API_SECRET",
        "BINANCE_FUTURES_TESTNET_API_KEY",
        "BINANCE_FUTURES_TESTNET_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    }
)


class ProtocolReadinessError(RuntimeError):
    """Raised when the protocol-only boundary cannot pass safely."""


def _run_git(
    project_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run a local-only Git inspection command without exposing stderr."""

    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=check,
    )


def _base_payload(
    *,
    status: str,
    reason_codes: Sequence[str],
    credential_environment_empty: bool,
) -> dict[str, Any]:
    return {
        "gate": "NT-GATE-1B",
        "protocol_version": "1.5",
        "mode": "PROTOCOL_READINESS_ONLY",
        "status": status,
        "reason_codes": list(reason_codes),
        "completed_at": datetime.now(UTC).isoformat(),
        "credential_environment_empty": credential_environment_empty,
        "credentials_read": False,
        "network_accessed": False,
        "authenticated_request_sent": False,
        "order_summary": {"canceled": 0, "filled": 0, "submitted": 0},
        "economic_event_summary": {"fees": 0, "funding": 0},
        "position_changes": 0,
        "agent_credential_access_allowed": False,
        "next_action": (
            "WAIT_FOR_EXPLICIT_CREDENTIAL_AUTHORIZATION"
            if status == "PASS"
            else "STOP_PROTOCOL_READINESS"
        ),
    }


def _write_evidence(evidence_dir: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically write the sole readiness evidence document."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = evidence_dir / "protocol-readiness.json"
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=evidence_dir,
            prefix=".protocol-readiness-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    return destination


def _git_text(project_root: Path, *args: str) -> str:
    return _run_git(project_root, *args).stdout.decode("ascii").strip()


def _collect_frozen_protocol_state(project_root: Path) -> dict[str, str]:
    tag_ref = f"refs/tags/{PROTOCOL_TAG}"

    try:
        tag_type = _git_text(project_root, "cat-file", "-t", tag_ref)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ProtocolReadinessError("PROTOCOL_GIT_STATE_INVALID") from exc

    if tag_type != "tag":
        raise ProtocolReadinessError("PROTOCOL_TAG_NOT_ANNOTATED")

    try:
        tag_commit = _git_text(project_root, "rev-parse", f"{tag_ref}^{{commit}}")
        head_commit = _git_text(project_root, "rev-parse", "HEAD^{commit}")
        ancestor = _run_git(
            project_root,
            "merge-base",
            "--is-ancestor",
            tag_commit,
            head_commit,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ProtocolReadinessError("PROTOCOL_GIT_STATE_INVALID") from exc

    if ancestor.returncode == 1:
        raise ProtocolReadinessError("PROTOCOL_TAG_NOT_ANCESTOR")
    if ancestor.returncode != 0:
        raise ProtocolReadinessError("PROTOCOL_GIT_STATE_INVALID")

    protocol_path = project_root / PROTOCOL_RELATIVE_PATH
    try:
        current_protocol = protocol_path.read_bytes()
        frozen_protocol = _run_git(
            project_root,
            "show",
            f"{tag_ref}:{PROTOCOL_RELATIVE_PATH.as_posix()}",
        ).stdout
    except FileNotFoundError as exc:
        raise ProtocolReadinessError("PROTOCOL_FILE_UNAVAILABLE") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolReadinessError("PROTOCOL_GIT_STATE_INVALID") from exc

    if current_protocol != frozen_protocol:
        raise ProtocolReadinessError("PROTOCOL_BYTES_CHANGED_AFTER_FREEZE")

    return {
        "protocol_tag": PROTOCOL_TAG,
        "protocol_commit": tag_commit,
        "tested_commit": head_commit,
        "protocol_sha256": hashlib.sha256(current_protocol).hexdigest(),
    }


def run_protocol_readiness(
    *,
    project_root: Path,
    evidence_dir: Path,
    environ: Mapping[str, str],
) -> tuple[int, Path]:
    """Run the offline protocol gate and return its exit code and evidence path."""

    environment_names = set(environ)
    if environment_names & RECOGNIZED_BINANCE_CREDENTIAL_ENV_NAMES:
        payload = _base_payload(
            status="STOP",
            reason_codes=["CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"],
            credential_environment_empty=False,
        )
        return 1, _write_evidence(evidence_dir, payload)

    try:
        frozen_state = _collect_frozen_protocol_state(Path(project_root))
    except ProtocolReadinessError as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[str(exc)],
            credential_environment_empty=True,
        )
        return 1, _write_evidence(evidence_dir, payload)

    payload = _base_payload(
        status="PASS",
        reason_codes=[],
        credential_environment_empty=True,
    )
    payload.update(frozen_state)
    return 0, _write_evidence(evidence_dir, payload)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the credential-free NT-GATE-1B v1.5 protocol readiness gate."
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Directory that will receive protocol-readiness.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with no credential or network inputs."""

    args = _parse_args(argv)
    exit_code, evidence_path = run_protocol_readiness(
        project_root=PROJECT_ROOT,
        evidence_dir=args.evidence_dir,
        environ=os.environ,
    )
    print(
        json.dumps(
            {"evidence_path": str(evidence_path), "exit_code": exit_code},
            sort_keys=True,
        )
    )
    return exit_code
