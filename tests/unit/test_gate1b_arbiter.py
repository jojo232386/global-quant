from __future__ import annotations

from global_quant.gate1b.arbiter import MANDATORY_RESTARTS
from global_quant.gate1b.arbiter import MANDATORY_SCENARIOS
from global_quant.gate1b.arbiter import decide_gate1b


def passing_candidate() -> dict[str, object]:
    return {
        "external_blockers": [],
        "engineering_failures": [],
        "scenario_results": {name: "PASS" for name in MANDATORY_SCENARIOS},
        "restart_results": {name: "PASS" for name in MANDATORY_RESTARTS},
        "endpoint_allowlist_status": "PASS",
        "credential_redaction_status": "PASS",
        "final_flat_status": "PASS",
        "ledger_replay_status": "PASS",
        "workbuddy_review": "PASS",
        "unresolved_P0": 0,
        "unresolved_P1": 0,
        "effective_work_seconds": 1_000,
    }


def test_missing_demo_credentials_is_inconclusive() -> None:
    candidate = passing_candidate()
    candidate["external_blockers"] = ["MISSING_DEMO_CREDENTIALS"]
    candidate["scenario_results"] = {}
    candidate["restart_results"] = {}
    candidate["workbuddy_review"] = "NOT_OBTAINED"

    verdict = decide_gate1b(candidate)

    assert verdict["verdict"] == "INCONCLUSIVE"
    assert verdict["reason_codes"] == ["MISSING_DEMO_CREDENTIALS"]


def test_engineering_failure_overrides_external_blocker() -> None:
    candidate = passing_candidate()
    candidate["external_blockers"] = ["MISSING_DEMO_CREDENTIALS"]
    candidate["engineering_failures"] = ["SECRET_IN_EVIDENCE"]

    verdict = decide_gate1b(candidate)

    assert verdict["verdict"] == "STOP"
    assert verdict["reason_codes"] == ["SECRET_IN_EVIDENCE"]


def test_pass_requires_every_scenario_restart_and_workbuddy_review() -> None:
    candidate = passing_candidate()
    candidate["scenario_results"] = {"authenticate_streams": "PASS"}
    candidate["workbuddy_review"] = "NOT_OBTAINED"

    verdict = decide_gate1b(candidate)

    assert verdict["verdict"] == "STOP"
    assert "MANDATORY_SCENARIO_INCOMPLETE" in verdict["reason_codes"]
    assert "WORKBUDDY_REVIEW_MISSING" in verdict["reason_codes"]


def test_unresolved_p1_and_time_limit_prevent_pass() -> None:
    candidate = passing_candidate()
    candidate["unresolved_P1"] = 1
    candidate["effective_work_seconds"] = 43_201

    verdict = decide_gate1b(candidate)

    assert verdict["verdict"] == "STOP"
    assert verdict["reason_codes"] == ["P1_UNRESOLVED", "TIME_LIMIT_EXCEEDED"]


def test_complete_clean_candidate_can_pass_engineering_gate() -> None:
    verdict = decide_gate1b(passing_candidate())

    assert verdict == {"verdict": "PASS", "reason_codes": []}
