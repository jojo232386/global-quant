from __future__ import annotations

from typing import Mapping


MAX_EFFECTIVE_WORK_SECONDS = 12 * 60 * 60

ALLOWED_INCONCLUSIVE_REASONS = frozenset(
    {
        "MISSING_DEMO_CREDENTIALS",
        "DEMO_OUTAGE",
        "MINIMUM_NOTIONAL_ABOVE_CAP",
        "PARTIAL_FILL_UNAVAILABLE",
        "PROTECTION_TRIGGER_UNAVAILABLE",
        "FUNDING_BOUNDARY_UNAVAILABLE",
    },
)

MANDATORY_SCENARIOS = (
    "authenticate_streams",
    "initial_long_short",
    "protection_orders_trigger",
    "close_cancel_siblings",
    "reverse_long_short_flat",
    "venue_rejection",
    "submit_cancel_race",
    "partial_fill",
    "funding",
    "reconciliation",
)

MANDATORY_RESTARTS = (
    "kill_after_intent",
    "kill_after_ack",
    "kill_after_partial_fill",
    "kill_after_cancel_request",
    "kill_with_active_protection",
    "private_stream_disconnect",
)


def decide_gate1b(candidate: Mapping[str, object]) -> dict[str, object]:
    failures = list(_strings(candidate.get("engineering_failures", [])))
    if int(candidate.get("unresolved_P0", 0)):
        failures.append("P0_UNRESOLVED")
    if int(candidate.get("unresolved_P1", 0)):
        failures.append("P1_UNRESOLVED")
    if float(candidate.get("effective_work_seconds", 0)) > MAX_EFFECTIVE_WORK_SECONDS:
        failures.append("TIME_LIMIT_EXCEEDED")
    for field, reason in (
        ("endpoint_allowlist_status", "ENDPOINT_ALLOWLIST_FAILURE"),
        ("credential_redaction_status", "CREDENTIAL_REDACTION_FAILURE"),
        ("final_flat_status", "FINAL_ACCOUNT_NOT_FLAT"),
        ("ledger_replay_status", "LEDGER_REPLAY_MISMATCH"),
    ):
        if candidate.get(field) in {"FAIL", "STOP"}:
            failures.append(reason)
    if failures:
        return {"verdict": "STOP", "reason_codes": _unique(failures)}

    blockers = list(_strings(candidate.get("external_blockers", [])))
    invalid_blockers = [
        blocker for blocker in blockers if blocker not in ALLOWED_INCONCLUSIVE_REASONS
    ]
    if invalid_blockers:
        return {
            "verdict": "STOP",
            "reason_codes": [f"UNAPPROVED_BLOCKER_{value}" for value in invalid_blockers],
        }
    if blockers:
        return {"verdict": "INCONCLUSIVE", "reason_codes": _unique(blockers)}

    completion_failures: list[str] = []
    scenarios = _status_mapping(candidate.get("scenario_results", {}))
    restarts = _status_mapping(candidate.get("restart_results", {}))
    if any(scenarios.get(name) != "PASS" for name in MANDATORY_SCENARIOS):
        completion_failures.append("MANDATORY_SCENARIO_INCOMPLETE")
    if any(restarts.get(name) != "PASS" for name in MANDATORY_RESTARTS):
        completion_failures.append("MANDATORY_RESTART_INCOMPLETE")
    if candidate.get("workbuddy_review") != "PASS":
        completion_failures.append("WORKBUDDY_REVIEW_MISSING")
    for field, reason in (
        ("endpoint_allowlist_status", "ENDPOINT_ALLOWLIST_INCOMPLETE"),
        ("credential_redaction_status", "CREDENTIAL_REDACTION_INCOMPLETE"),
        ("final_flat_status", "FINAL_FLAT_PROOF_INCOMPLETE"),
        ("ledger_replay_status", "LEDGER_REPLAY_INCOMPLETE"),
    ):
        if candidate.get(field) != "PASS":
            completion_failures.append(reason)
    if completion_failures:
        return {"verdict": "STOP", "reason_codes": completion_failures}
    return {"verdict": "PASS", "reason_codes": []}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _status_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
