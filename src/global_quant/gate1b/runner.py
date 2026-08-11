from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from global_quant.gate1b.runtime import DemoRuntimeInputs, build_demo_node
from global_quant.gate1b.safety import DemoCredentials, assert_secret_free

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


def default_evidence_dir() -> Path:
    commit = _git_commit()[:12] or "unknown"
    return PROJECT_ROOT / "evidence" / "runtime" / f"gate1b-v1.4-{commit}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-only", action="store_true", required=True)
    parser.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    args = parser.parse_args(argv)
    exit_code, path = run_build_only(args.evidence_dir)
    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


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
                "protocols/NT_GATE_1B_V1_4.md",
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
