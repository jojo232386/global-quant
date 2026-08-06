from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from global_quant.gate1b.demo_preflight import run_signed_preflight
from global_quant.gate1b.demo_preflight import sanitized_preflight_evidence
from global_quant.gate1b.preflight import PreflightError
from global_quant.gate1b.runtime import DemoRuntimeInputs
from global_quant.gate1b.runtime import build_demo_node
from global_quant.gate1b.safety import DEMO_KEY_NAME
from global_quant.gate1b.safety import DEMO_SECRET_NAME
from global_quant.gate1b.safety import DemoCredentialError
from global_quant.gate1b.safety import DemoCredentials
from global_quant.gate1b.safety import assert_secret_free
from global_quant.gate1b.safety import load_demo_credentials


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run_build_only(evidence_dir: Path) -> tuple[int, Path]:
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    credentials = DemoCredentials(
        api_key="offline-build-key-test-only",
        api_secret="offline-build-secret-test-only",
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        node, _ = build_demo_node(
            DemoRuntimeInputs(
                credentials=credentials,
                evidence_dir=evidence_dir / "nautilus",
                ledger_path=evidence_dir / "build-only-ledger.jsonl",
                initial_wallet=Decimal("10000"),
                source_hash=_source_hash(),
                config_hash=_config_hash(),
            ),
        )
        node.dispose()
    finally:
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()
    payload = {
        "status": "READY",
        "mode": "BUILD_ONLY",
        "network_accessed": False,
        "credentials_read": False,
        "source_hash": _source_hash(),
        "config_hash": _config_hash(),
        "completed_at": _utc_now(),
    }
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    assert_secret_free(encoded, credentials)
    _assert_directory_secret_free(evidence_dir, credentials)
    path = _write_json(evidence_dir / "build_only.json", payload)
    return 0, path


def run_preflight(
    *,
    environ: Mapping[str, str],
    confirm_demo_only: bool,
    evidence_dir: Path,
) -> tuple[int, Path]:
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    presence = {
        DEMO_KEY_NAME: bool(environ.get(DEMO_KEY_NAME)),
        DEMO_SECRET_NAME: bool(environ.get(DEMO_SECRET_NAME)),
    }
    try:
        credentials = load_demo_credentials(
            environ,
            confirm_demo_only=confirm_demo_only,
        )
    except DemoCredentialError as exc:
        reason = str(exc).split(":", 1)[0]
        status = "INCONCLUSIVE" if reason == "MISSING_DEMO_CREDENTIALS" else "STOP"
        payload = _failure_payload(
            status=status,
            reason=reason,
            presence=presence,
            network_accessed=False,
        )
        return (2 if status == "INCONCLUSIVE" else 1), _write_json(
            evidence_dir / "preflight.json",
            payload,
        )

    try:
        snapshot, result = asyncio.run(run_signed_preflight(credentials))
        payload = {
            **sanitized_preflight_evidence(snapshot=snapshot, result=result),
            "mode": "SIGNED_READ_ONLY_PREFLIGHT",
            "network_accessed": True,
            "credential_presence": presence,
            "credential_redaction": "PASS",
            "account_identifier": "NOT_EXPOSED_BY_PREFLIGHT_ENDPOINTS",
            "completed_at": _utc_now(),
        }
        encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        assert_secret_free(encoded, credentials)
        path = _write_json(evidence_dir / "preflight.json", payload)
        _assert_directory_secret_free(evidence_dir, credentials)
        return 0, path
    except PreflightError as exc:
        payload = _failure_payload(
            status="STOP",
            reason=str(exc).split(":", 1)[0],
            presence=presence,
            network_accessed=True,
        )
        encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        assert_secret_free(encoded, credentials)
        return 1, _write_json(evidence_dir / "preflight.json", payload)
    except (TimeoutError, ConnectionError, OSError):
        payload = _failure_payload(
            status="INCONCLUSIVE",
            reason="DEMO_PREFLIGHT_UNAVAILABLE",
            presence=presence,
            network_accessed=True,
        )
        encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        assert_secret_free(encoded, credentials)
        return 2, _write_json(evidence_dir / "preflight.json", payload)
    except Exception as exc:  # The evidence records only the type, never raw response text.
        payload = _failure_payload(
            status="STOP",
            reason=f"PREFLIGHT_FAILURE_{type(exc).__name__.upper()}",
            presence=presence,
            network_accessed=True,
        )
        encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        assert_secret_free(encoded, credentials)
        return 1, _write_json(evidence_dir / "preflight.json", payload)


def default_evidence_dir() -> Path:
    commit = _git_commit()[:12] or "unknown"
    return PROJECT_ROOT / "evidence" / "runtime" / f"gate1b-v1.3-{commit}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build-only", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-demo-only", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    args = parser.parse_args(argv)
    if args.build_only:
        exit_code, path = run_build_only(args.evidence_dir)
    else:
        exit_code, path = run_preflight(
            environ=os.environ,
            confirm_demo_only=args.confirm_demo_only,
            evidence_dir=args.evidence_dir,
        )
    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


def _failure_payload(
    *,
    status: str,
    reason: str,
    presence: dict[str, bool],
    network_accessed: bool,
) -> dict[str, object]:
    return {
        "status": status,
        "reason_codes": [reason],
        "mode": "SIGNED_READ_ONLY_PREFLIGHT",
        "network_accessed": network_accessed,
        "credential_presence": presence,
        "credential_redaction": "PASS",
        "automated_cleanup_allowed": False,
        "completed_at": _utc_now(),
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return path


def _digest_files(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_hash() -> str:
    return _digest_files(
        tuple(
            PROJECT_ROOT / relative
            for relative in (
                "src/global_quant/gate1a/strategy.py",
                "src/global_quant/gate1a/coordinator.py",
                "src/global_quant/gate1a/ledger.py",
                "src/global_quant/gate1a/recovery.py",
            )
        ),
    )


def _config_hash() -> str:
    return _digest_files(
        tuple(
            PROJECT_ROOT / relative
            for relative in (
                "src/global_quant/gate1b/config.py",
                "src/global_quant/gate1b/runtime.py",
                "src/global_quant/gate1b/safety.py",
                "src/global_quant/gate1b/credential_prompt.py",
                "scripts/run_gate_1b_prompted.py",
                "protocols/NT_GATE_1B_V1_3.md",
            )
        ),
    )


def _assert_directory_secret_free(path: Path, credentials: DemoCredentials) -> None:
    for candidate in path.rglob("*"):
        if candidate.is_file():
            assert_secret_free(candidate.read_text(errors="ignore"), credentials)


def _git_commit() -> str:
    head = PROJECT_ROOT / ".git" / "HEAD"
    if not head.exists():
        return ""
    value = head.read_text().strip()
    if not value.startswith("ref: "):
        return value
    reference = PROJECT_ROOT / ".git" / value.removeprefix("ref: ")
    return reference.read_text().strip() if reference.exists() else ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
