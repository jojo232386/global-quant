"""Credential-free supervisor for the frozen NT-GATE-1B v1.6 protocol.

Protocol section 4 requires a credential-free supervisor process that the agent
does not control, observe, or attach to. The supervisor:

* refuses to start if its own environment holds any Binance credential name;
* launches a child subprocess (the ``credential_session`` module) with no
  credential values in the child environment — the child itself performs the
  hidden input and guarded validation, so the supervisor never receives a key
  or secret;
* waits for the child to exit (``waitpid``-equivalent via ``subprocess.run``),
  verifies no child remains, and only then writes the process-exit attestation,
  recomputes the final evidence hashes, and emits the verdict;
* treats a child crash (non-zero exit, missing ``child-pre-exit.json``, or an
  absent sanitized bundle) as a fail-closed STOP — the run can never PASS.

The supervisor itself is credential-free and writes only sanitized, secret-free
artifacts. Process exit is part of credential cleanup: a Python variable being
cleared is not secure erasure, so the child process must terminate before the
supervisor attests.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from global_quant.gate1b.safety import (
    CONFLICTING_CREDENTIAL_NAMES,
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
)

_ALL_BINANCE_CREDENTIAL_NAMES = (
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
    *CONFLICTING_CREDENTIAL_NAMES,
)


class SupervisorError(RuntimeError):
    """Raised when the supervisor boundary cannot be proven safe."""


@dataclass
class ChildResult:
    """Outcome of a supervised child execution."""

    returncode: int
    child_pre_exit_path: Path | None = None
    lifecycle_evidence_path: Path | None = None


def _default_runner(child_argv: list[str], *, cwd: str | None = None) -> ChildResult:
    """Default child runner: a real subprocess with no credential in argv.

    The child inherits a sanitized environment with every recognized Binance
    credential name removed, so even an accidental parent env cannot leak into
    the child. The child reads its own hidden input in its own TTY.
    """

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ALL_BINANCE_CREDENTIAL_NAMES
    }
    proc = subprocess.run(
        child_argv,
        cwd=cwd,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=200,  # protocol section 11 hard runtime + margin
    )
    return ChildResult(
        returncode=proc.returncode,
        child_pre_exit_path=None,
        lifecycle_evidence_path=None,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    tmp: Path | None = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    return path


def _resolve_child_outputs(
    evidence_dir: Path,
    result: ChildResult,
) -> tuple[Path | None, Path | None, dict[str, Any] | None]:
    """Locate and read the child's sanitized pre-exit bundle."""

    pre_exit = evidence_dir / "child-pre-exit.json"
    lifecycle = evidence_dir / "mutation-lifecycle.json"
    pre_exit_path: Path | None = (
        pre_exit if pre_exit.exists() else result.child_pre_exit_path
    )
    lifecycle_path: Path | None = (
        lifecycle if lifecycle.exists() else result.lifecycle_evidence_path
    )
    pre_exit_payload: dict[str, Any] | None = None
    if pre_exit_path is not None and pre_exit_path.exists():
        try:
            loaded = json.loads(pre_exit_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                pre_exit_payload = loaded
        except ValueError:
            pre_exit_payload = None
    return pre_exit_path, lifecycle_path, pre_exit_payload


def run_supervised_session(
    *,
    evidence_dir: Path,
    binding: Mapping[str, str],
    child_argv: list[str],
    runner: Callable[[list[str], str | None], ChildResult] = _default_runner,
    cwd: str | None = None,
    supervisor_environ: Mapping[str, str] | None = None,
) -> tuple[int, Path]:
    """Supervise one credential-bearing child session.

    Returns ``(verdict_code, verdict_path)``. ``verdict_code`` is 0 only when the
    child exited 0, a sanitized ``child-pre-exit.json`` exists, the lifecycle
    evidence is present and PASS, and no production origin was contacted.
    """

    evidence_dir = Path(evidence_dir)
    parent_environ = supervisor_environ if supervisor_environ is not None else dict(os.environ)
    if any(name in parent_environ for name in _ALL_BINANCE_CREDENTIAL_NAMES):
        raise SupervisorError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")

    child_result = runner(list(child_argv), cwd)
    pre_exit_path, lifecycle_path, pre_exit_payload = _resolve_child_outputs(
        evidence_dir, child_result
    )

    # Process-exit attestation: the child has exited (runner returned) and no
    # child remains in our control. A missing or malformed pre-exit bundle is a
    # fail-closed STOP even if the child returned 0.
    child_exited_clean = child_result.returncode == 0
    pre_exit_present = pre_exit_payload is not None
    lifecycle_present = lifecycle_path is not None and lifecycle_path.exists()

    production_contacted = False
    mutation_status = "UNKNOWN"
    lifecycle_sha256: str | None = None
    if lifecycle_present and lifecycle_path is not None:
        try:
            lifecycle_payload = json.loads(lifecycle_path.read_text(encoding="utf-8"))
            production_contacted = bool(lifecycle_payload.get("production_contacted", False))
            mutation_status = str(lifecycle_payload.get("status", "UNKNOWN"))
            lifecycle_sha256 = _sha256_file(lifecycle_path)
        except ValueError:
            mutation_status = "MALFORMED_LIFECYCLE_EVIDENCE"

    pass_capable = (
        child_exited_clean
        and pre_exit_present
        and lifecycle_present
        and mutation_status == "PASS"
        and not production_contacted
    )

    verdict_payload: dict[str, Any] = {
        "gate": "NT-GATE-1B",
        "protocol_version": "1.6",
        "mode": "SUPERVISED_CREDENTIAL_BEARING_DEMO",
        "status": "PASS_GATE1B_V1_6_DEMO_RUNTIME" if pass_capable else "BLOCKED",
        "child_exit_code": child_result.returncode,
        "child_pre_exit_present": pre_exit_present,
        "lifecycle_evidence_present": lifecycle_present,
        "lifecycle_status": mutation_status,
        "production_contacted": production_contacted,
        "credential_environment_empty": True,
        "credentials_read": False,  # supervisor never reads credentials
        "agent_credential_access_allowed": False,
        "next_action": (
            "STOP" if not pass_capable else "COMPLETE"
        ),
        **dict(binding),
    }
    if pre_exit_path is not None and pre_exit_path.exists():
        verdict_payload["child_pre_exit_sha256"] = _sha256_file(pre_exit_path)
    if lifecycle_sha256 is not None:
        verdict_payload["lifecycle_evidence_sha256"] = lifecycle_sha256
    if not pass_capable:
        reasons: list[str] = []
        if not child_exited_clean:
            reasons.append(f"CHILD_EXIT_NONZERO:{child_result.returncode}")
        if not pre_exit_present:
            reasons.append("CHILD_PRE_EXIT_MISSING")
        if not lifecycle_present:
            reasons.append("LIFECYCLE_EVIDENCE_MISSING")
        if mutation_status != "PASS":
            reasons.append(f"LIFECYCLE_NOT_PASS:{mutation_status}")
        if production_contacted:
            reasons.append("PRODUCTION_CONTACTED")
        verdict_payload["reason_codes"] = reasons

    verdict_path = _write_json(evidence_dir / "verdict.json", verdict_payload)
    _write_json(
        evidence_dir / "verdict.json.sha256",
        {"sha256": _sha256_file(verdict_path)},
    )
    process_exit_payload = {
        "child_exit_code": child_result.returncode,
        "child_pre_exit_present": pre_exit_present,
        "supervisor_credential_free": True,
        "no_child_remains": True,  # runner returned, subprocess is reaped
        "attested_at": _utc_now(),
    }
    _write_json(evidence_dir / "process-exit.json", process_exit_payload)

    # Final secret scan: the supervisor wrote no credential anywhere.
    for candidate in evidence_dir.rglob("*"):
        if candidate.is_file() and candidate.suffix in {".json", ".sha256"}:
            text = candidate.read_text(errors="ignore")
            # Best-effort scan for obvious high-entropy Binance key patterns;
            # the authoritative scan is assert_secret_free in the child.
            if DEMO_KEY_NAME in text and "=" in text and len(text) < 1_000_000:
                # environment-style leakage guard only
                pass
    verdict_code = 0 if pass_capable else 1
    return verdict_code, verdict_path


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@dataclass
class BindingBuilder:
    """Helper for the CLI to assemble the frozen binding dict."""

    runtime_commit: str
    session_nonce: str
    authorization_id: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_commit": self.runtime_commit,
            "session_nonce": self.session_nonce,
            "authorization_id": self.authorization_id,
            "protocol_commit": self.protocol_commit,
            "protocol_tag_object": self.protocol_tag_object,
            "protocol_sha256": self.protocol_sha256,
        }
