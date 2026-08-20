"""Fail-closed entry authorization for the GMAQ dry-run runtime.

This module is imported by the Freqtrade strategy, so it deliberately uses
only the Python standard library.  A running bot is not permission to enter a
trade: every entry attempt must match a short-lived, audited authorization
bound to the exact runtime, candidate commit, and configuration digest.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import re
import time


GATE_SCHEMA_VERSION = 1
DRY_RUN_ENVIRONMENT = "dry_run"
DRY_RUN_SCOPE = "DEMO_DRY_RUN_ENTRY"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


def _deny(reason: str) -> GateDecision:
    return GateDecision(False, reason)


def _serialize(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _record_sha(record: dict) -> str:
    return hashlib.sha256(_serialize(record).encode("utf-8")).hexdigest()


def _file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: pathlib.Path, label: str) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None, f"{label}_missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, f"{label}_corrupt"
    if not isinstance(value, dict):
        return None, f"{label}_not_object"
    return value, None


def _read_audit(path: pathlib.Path) -> tuple[list[dict] | None, str | None]:
    try:
        raw_lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None, "audit_missing"
    except (OSError, UnicodeDecodeError):
        return None, "audit_unreadable"

    records: list[dict] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None, "audit_corrupt"
        if not isinstance(record, dict):
            return None, "audit_record_not_object"
        records.append(record)
    if not records:
        return None, "audit_empty"
    return records, None


def _audit_chain_valid(records: list[dict]) -> tuple[bool, str]:
    for index, record in enumerate(records):
        if record.get("seq") != index + 1:
            return False, "audit_sequence_invalid"
        expected_prev = "" if index == 0 else _record_sha(records[index - 1])
        if record.get("prev_sha") != expected_prev:
            return False, "audit_chain_broken"
    return True, "audit_chain_valid"


def evaluate_entry_gate(
    *,
    state_path: pathlib.Path,
    audit_path: pathlib.Path,
    config_path: pathlib.Path,
    expected_environment: str,
    expected_candidate_sha: str,
    expected_config_sha256: str,
    expected_run_id: str,
    now_epoch: int | None = None,
) -> GateDecision:
    """Validate the complete authorization binding for one entry attempt."""

    if expected_environment != DRY_RUN_ENVIRONMENT:
        return _deny("runtime_environment_not_dry_run")
    if not GIT_SHA_RE.fullmatch(expected_candidate_sha or ""):
        return _deny("runtime_candidate_unbound")
    if not SHA256_RE.fullmatch(expected_config_sha256 or ""):
        return _deny("runtime_config_unbound")
    if not expected_run_id:
        return _deny("runtime_run_id_unbound")

    try:
        actual_config_sha = _file_sha256(config_path)
        config = json.loads(config_path.read_text())
    except FileNotFoundError:
        return _deny("config_missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _deny("config_corrupt")
    if not isinstance(config, dict):
        return _deny("config_not_object")
    exchange = config.get("exchange", {})
    if (
        config.get("dry_run") is not True
        or not isinstance(exchange, dict)
        or exchange.get("key")
        or exchange.get("secret")
    ):
        return _deny("config_not_credential_free_dry_run")
    if actual_config_sha != expected_config_sha256:
        return _deny("runtime_config_digest_mismatch")

    state, error = _read_object(state_path, "state")
    if error:
        return _deny(error)
    assert state is not None
    if state.get("schema_version") != GATE_SCHEMA_VERSION:
        return _deny("state_schema_mismatch")
    if state.get("state") != "ARMED":
        return _deny("state_not_armed")
    if state.get("environment") != expected_environment:
        return _deny("state_environment_mismatch")
    if state.get("authorization_scope") != DRY_RUN_SCOPE:
        return _deny("authorization_scope_mismatch")
    if state.get("candidate_sha") != expected_candidate_sha:
        return _deny("candidate_sha_mismatch")
    if state.get("config_sha256") != expected_config_sha256:
        return _deny("config_sha_mismatch")
    if state.get("run_id") != expected_run_id:
        return _deny("run_id_mismatch")
    if state.get("preflight_verdict") != "PASS":
        return _deny("preflight_not_passed")
    if not state.get("authorization_id"):
        return _deny("authorization_id_missing")

    now = int(time.time()) if now_epoch is None else int(now_epoch)
    issued_at = state.get("issued_at_epoch")
    expires_at = state.get("expires_at_epoch")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        return _deny("authorization_time_invalid")
    if issued_at > now + 5:
        return _deny("authorization_not_yet_valid")
    if expires_at <= now:
        return _deny("authorization_expired")

    records, error = _read_audit(audit_path)
    if error:
        return _deny(error)
    assert records is not None
    valid, reason = _audit_chain_valid(records)
    if not valid:
        return _deny(reason)

    authorization_seq = state.get("authorization_audit_seq")
    authorization_sha = state.get("authorization_audit_sha")
    if not isinstance(authorization_seq, int) or not SHA256_RE.fullmatch(authorization_sha or ""):
        return _deny("authorization_audit_binding_invalid")
    if authorization_seq < 1 or authorization_seq > len(records):
        return _deny("authorization_audit_record_missing")
    authorization_record = records[authorization_seq - 1]
    if _record_sha(authorization_record) != authorization_sha:
        return _deny("authorization_audit_digest_mismatch")
    refs = authorization_record.get("refs", {})
    if (
        authorization_record.get("event") != "arm"
        or authorization_record.get("verdict") != "ARMED"
        or not isinstance(refs, dict)
        or refs.get("authorization_id") != state.get("authorization_id")
        or refs.get("authorization_scope") != DRY_RUN_SCOPE
        or refs.get("candidate_sha") != expected_candidate_sha
        or refs.get("config_sha256") != expected_config_sha256
        or refs.get("run_id") != expected_run_id
        or refs.get("expires_at_epoch") != expires_at
    ):
        return _deny("authorization_audit_record_mismatch")

    return GateDecision(True, "authorized_dry_run_entry")


def decision_from_environment() -> GateDecision:
    user_data = pathlib.Path(os.environ.get("GMAQ_USER_DATA_DIR", "/freqtrade/user_data"))
    return evaluate_entry_gate(
        state_path=pathlib.Path(
            os.environ.get("GMAQ_GATE_STATE_PATH", str(user_data / "audit" / "state.json"))
        ),
        audit_path=pathlib.Path(
            os.environ.get("GMAQ_AUDIT_PATH", str(user_data / "audit" / "manifest.jsonl"))
        ),
        config_path=pathlib.Path(
            os.environ.get("GMAQ_CONFIG_PATH", str(user_data / "config.json"))
        ),
        expected_environment=os.environ.get("GMAQ_GATE_ENVIRONMENT", "UNBOUND"),
        expected_candidate_sha=os.environ.get("GMAQ_CANDIDATE_SHA", "UNBOUND"),
        expected_config_sha256=os.environ.get("GMAQ_CONFIG_SHA256", "UNBOUND"),
        expected_run_id=os.environ.get("GMAQ_RUN_ID", "UNBOUND"),
    )
