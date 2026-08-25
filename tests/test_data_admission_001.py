"""Fail-closed integrity checks for DATA-ADMISSION-001."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "research" / "data" / "data-admission-001-contract.json"
FAILURE_PATH = ROOT / "research" / "data" / "data-admission-001-failure.json"
REVIEW_PATH = ROOT / "research" / "data" / "data-admission-001-independent-review.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_contract_identity_and_fixed_candidate_are_bound() -> None:
    contract = load(CONTRACT_PATH)
    failure = load(FAILURE_PATH)

    assert contract["status"] == "FROZEN_BEFORE_ANALYSIS"
    assert contract["data_admission_id"] == failure["data_admission_id"] == (
        "DATA-ADMISSION-001"
    )
    assert contract["candidate"]["candidate_id"] == failure["candidate_id"] == (
        "CAND-DERIVATIVES-POSITIONING-001"
    )
    assert contract["candidate"]["mechanism"] == (
        "OI stock + adverse funding flow + weak price confirmation -> "
        "leveraged crowding -> 3-day reversal"
    )
    assert contract["fixed_cohort"]["cohort_id"] == failure["fixed_cohort"][
        "cohort_id"
    ]
    assert contract["fixed_cohort"]["size"] == failure["fixed_cohort"]["size"] == 80
    assert failure["contract"]["sha256"] == sha256(CONTRACT_PATH)


def test_timing_and_missing_data_rules_are_fail_closed() -> None:
    contract = load(CONTRACT_PATH)

    assert contract["timing"]["required_publication_or_arrival_deadline"] == (
        "t+1 00:05:00 UTC"
    )
    assert "no earlier than t+1 00:10:00 UTC" in contract["timing"][
        "assumed_execution_timestamp"
    ]
    assert contract["timing"]["unproven_delay_policy"] == (
        "FAIL_SIGNAL_AVAILABILITY"
    )
    assert contract["data_quality_policy"]["fail_closed"] is True
    assert contract["data_quality_policy"]["imputation"] == "FORBIDDEN"
    assert contract["data_quality_policy"]["carry_forward"] == "FORBIDDEN"
    assert contract["data_quality_policy"]["symbol_replacement"] == "FORBIDDEN"
    assert contract["data_quality_policy"]["post_hoc_symbol_drop"] == "FORBIDDEN"
    assert contract["required_inputs"]["open_interest"]["lookback_contract"] == (
        "t-30 through t inclusive, exactly 31 positive endpoints"
    )
    assert contract["required_inputs"]["open_interest"]["diagnostic_only"] == (
        "288/288 intraday observations"
    )


def test_all_numeric_vintages_fail_and_no_symbol_is_admitted() -> None:
    failure = load(FAILURE_PATH)
    vintages = failure["numeric_vintage_judgments"]

    assert {family: result["verdict"] for family, result in vintages.items()} == {
        "price": "FAIL",
        "funding": "FAIL",
        "open_interest": "FAIL",
    }
    assert all(result["symbols_evaluated"] == 80 for result in vintages.values())
    assert all(result["symbols_passing_vintage"] == 0 for result in vintages.values())
    assert vintages["open_interest"]["current_archive_union_fixed_cohort_symbols"] == 40
    assert vintages["open_interest"]["monthly_old_scope_fixed_cohort_overlap_min"] == 17
    assert vintages["open_interest"]["monthly_old_scope_fixed_cohort_overlap_max"] == 25
    assert vintages["open_interest"]["archive_scope_defects"] == {
        "manifested_daily_archives": 21939,
        "valid_positive_2355_endpoints": 21908,
        "all_scope_defects": 31,
        "nonpositive_on_2022_03_07": 30,
        "missing_etcusdt_2023_02_07": 1,
        "fixed_cohort_known_defects": 23,
        "fixed_cohort_nonpositive_on_2022_03_07": 22,
        "fixed_cohort_missing_etcusdt_2023_02_07": 1,
    }
    assert failure["strict_outcome"] == {
        **failure["strict_outcome"],
        "data_admission_status": "FAIL",
        "symbols_admitted": 0,
        "admitted_interval": "NONE",
        "candidate_b_status": "PARKED_DATA_BLOCKED",
        "free_funding_oi_engineering": "STOP",
        "frequency_window_cohort_mechanism_rescue": "FORBIDDEN",
    }


def test_downstream_work_and_performance_remain_closed() -> None:
    failure = load(FAILURE_PATH)
    gates = failure["other_gate_judgments"]
    process = failure["process_integrity"]

    assert gates["signal_availability"]["verdict"] == "FAIL"
    assert gates["symbol_identity"] == {
        **gates["symbol_identity"],
        "verdict": "PASS",
        "scope": "FIXED_COHORT_BINDING_ONLY",
    }
    assert gates["coverage"]["verdict"] == "FAIL"
    assert gates["coverage"]["symbols_passing_all_requirements"] == 0
    assert gates["cost_model_input_availability"]["verdict"] == "FAIL"
    assert process["candidate_b_performance_accessed_or_generated"] is False
    assert process["effect_on_decision"].startswith("NONE;")
    assert process["expl_created"] is False
    assert process["adapter_or_platform_created"] is False
    assert process["real_order_count"] == 0


def test_independent_reviews_are_recorded() -> None:
    failure = load(FAILURE_PATH)
    reviews = load(REVIEW_PATH)

    assert failure["independent_reviews"]["checkpoint_a"] == (
        "PASS_AFTER_ONE_REPAIR"
    )
    assert failure["independent_reviews"]["checkpoint_b"] == "PASS"
    assert reviews["checkpoint_a"]["final_output"]["CHECKPOINT_A"] == "PASS"
    assert reviews["checkpoint_b"]["final_output"] == {
        "CHECKPOINT_B": "PASS",
        "PRICE_EVIDENCE": "SUPPORTED",
        "FUNDING_EVIDENCE": "SUPPORTED",
        "OI_EVIDENCE": "SUPPORTED",
        "HISTORICAL_AVAILABILITY_CLAIMS": "PASS",
        "FINDINGS": "NONE",
    }
    assert failure["independent_reviews"]["checkpoint_c"] == reviews[
        "checkpoint_c"
    ]["status"]
    assert reviews["checkpoint_c"]["status"] == "PASS_AFTER_ONE_REPAIR"
    assert reviews["checkpoint_c"]["final_output"] == {
        "CHECKPOINT_C": "PASS",
        "LOOKAHEAD": "PASS",
        "SURVIVOR_BIAS": "PASS",
        "OUTCOME_SELECTION": "PASS",
        "PARAMETER_RESCUE": "PASS",
        "VINTAGE_CLAIMS": "PASS",
        "SCOPE": "PASS",
        "FINDINGS": "NONE",
    }
