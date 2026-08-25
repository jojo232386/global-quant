"""Pre-performance freeze checks for Price/Lifecycle Sprint 001."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION = (
    ROOT
    / "research"
    / "exploration"
    / "price-lifecycle-sprint-001-preregistration.json"
)
PROTECTION = ROOT / "research" / "process" / "main-protection-sprint-001.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_main_protection_is_minimal_and_active() -> None:
    record = load(PROTECTION)
    rules = record["ruleset"]["rules"]

    assert record["canonical_main_at_configuration"] == (
        "7cbfd12c40dea4cd678e389208934909e7d2ff22"
    )
    assert record["method"] == "REPOSITORY_BRANCH_RULESET"
    assert record["ruleset"]["id"] == 21399360
    assert record["ruleset"]["target_ref"] == "refs/heads/main"
    assert record["ruleset"]["enforcement"] == "active"
    assert record["ruleset"]["bypass_actors"] == []
    assert rules["require_pull_request"] is True
    assert rules["required_approving_review_count"] == 0
    assert rules["required_status_checks"] == [
        {
            "context": "pytest",
            "integration_id": 15368,
            "integration": "github-actions",
        }
    ]
    assert rules["strict_required_status_checks_policy"] is False
    assert rules["block_force_push"] is True
    assert rules["block_deletion"] is True


def test_preregistration_freezes_a_bounded_two_candidate_program() -> None:
    record = load(PREREGISTRATION)
    candidates = record["candidates"]

    assert record["program_id"] == "PRICE_LIFECYCLE_SPRINT_001"
    assert record["research_tier"] == "TIER_1_EXPLORATION"
    assert record["status"] == "PREREGISTERED_PRE_PERFORMANCE"
    assert record["performance_firewall"]["status"] == (
        "PASS_VIA_CLEAN_DESIGNER_QUARANTINE"
    )
    assert record["performance_firewall"]["clean_designer_performance_accessed"] is False
    assert len(record["screened_candidates"]) <= 5
    assert 1 <= len(candidates) <= 3
    assert [candidate["order"] for candidate in candidates] == list(
        range(1, len(candidates) + 1)
    )
    assert len({candidate["mechanism_family"] for candidate in candidates}) == len(
        candidates
    )
    assert all(len(candidate["sanity_variants"]) <= 2 for candidate in candidates)
    assert record["common_execution_and_accounting"]["program_accounting"][
        "candidates_preregistered"
    ] == len(candidates)


def test_allowed_data_timing_and_stop_rules_are_fail_closed() -> None:
    record = load(PREREGISTRATION)
    contract = record["data_contract"]
    common = record["common_execution_and_accounting"]

    assert contract["cohort_id"] == "BINANCE_USDM_PERPETUAL_TRADING_20210104_195102Z"
    assert contract["support_start_inclusive_utc"] == "2021-01-04T19:51:02.039Z"
    assert contract["support_end_exclusive_utc"] == "2023-11-14T00:00:00Z"
    assert contract["forbidden_data"] == [
        "Funding",
        "open interest",
        "external data",
        "current exchangeInfo",
        "post-support observations",
    ]
    assert "completed" in common["decision_information_cutoff"]
    assert "UTC open of t+1" in common["execution"]
    assert "not an untouched holdout" in common["holdout_disclosure"]
    assert common["costs"]["pass_uses"] == "coarse_stress_one_way_turnover_cost"
    assert all(
        candidate["tier_1_pass_fail_criteria"]["fail_rule"].startswith(
            "Any failed condition"
        )
        for candidate in record["candidates"]
    )
    assert record["result_interpretation"]["ready_for_tiny_live"] is False


def test_volume_share_is_never_mislabeled_as_capital_flow() -> None:
    record = load(PREREGISTRATION)
    volume = next(
        candidate
        for candidate in record["candidates"]
        if candidate["mechanism_family"] == "volume-share migration"
    )

    guardrail = volume["signal_interpretation_guardrail"]
    assert "turnover and attention proxy only" in guardrail
    assert "never be labeled net inflow" in guardrail
