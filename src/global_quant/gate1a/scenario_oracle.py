from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


ORACLE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "nt_gate_1a_scenario_oracle_v1.json"
)
ORACLE_SHA256 = "5b6eb1f25028ebcee04aa399d0acfc9968a2e1cf20aebc9d85a8a93ab697b757"
EXPECTED_FIELDS = {
    "business_hash",
    "orders",
    "fills",
    "final_positions",
    "final_wallet",
    "protection_state",
    "exit_code",
}
COMPARISONS = (
    ("business_hash", "business_hash"),
    ("observed_orders", "orders"),
    ("observed_fills", "fills"),
    ("final_positions", "final_positions"),
    ("final_wallet", "final_wallet"),
    ("protection_state", "protection_state"),
    ("exit_code", "exit_code"),
)


class ScenarioOracleError(RuntimeError):
    """Raised when the frozen oracle itself is incomplete or changed."""


def load_frozen_oracle() -> dict[str, Any]:
    raw = ORACLE_PATH.read_bytes()
    observed_hash = hashlib.sha256(raw).hexdigest()
    if observed_hash != ORACLE_SHA256:
        raise ScenarioOracleError(
            f"scenario oracle checksum mismatch: {observed_hash}",
        )

    payload = json.loads(raw)
    if set(payload) != {"oracle_version", "protocol_version", "scenarios"}:
        raise ScenarioOracleError("scenario oracle top-level schema mismatch")
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, dict) or not scenarios:
        raise ScenarioOracleError("scenario oracle is empty")
    for name, expected in scenarios.items():
        if not name or set(expected) != EXPECTED_FIELDS:
            raise ScenarioOracleError(
                f"scenario oracle field mismatch: {name}",
            )
    return payload


def validate_scenario_payloads(
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    oracle = load_frozen_oracle()
    expected_by_name = oracle["scenarios"]
    actual_names = [payload.get("name") for payload in payloads]
    name_error = None
    if len(actual_names) != len(set(actual_names)):
        name_error = "scenario names are duplicated"
    elif set(actual_names) != set(expected_by_name):
        name_error = "scenario set does not match frozen oracle"

    validated: list[dict[str, Any]] = []
    for raw_payload in payloads:
        payload = copy.deepcopy(raw_payload)
        name = payload.get("name")
        expected = expected_by_name.get(name)
        errors = [name_error] if name_error else []
        if expected is None:
            errors.append(f"unknown scenario: {name}")
        else:
            for actual_field, expected_field in COMPARISONS:
                if payload.get(actual_field) != expected[expected_field]:
                    errors.append(f"{actual_field} mismatch")
            payload.update(
                {
                    "expected_orders": copy.deepcopy(expected["orders"]),
                    "expected_fills": copy.deepcopy(expected["fills"]),
                    "expected_final_positions": copy.deepcopy(
                        expected["final_positions"],
                    ),
                    "expected_final_wallet": expected["final_wallet"],
                    "expected_protection_state": copy.deepcopy(
                        expected["protection_state"],
                    ),
                    "expected_exit_code": expected["exit_code"],
                    "expected_business_hash": expected["business_hash"],
                },
            )

        payload["oracle_version"] = oracle["oracle_version"]
        payload["validation_errors"] = errors
        payload["status"] = "STOP" if errors else "PASS"
        validated.append(payload)
    return validated
