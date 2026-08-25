"""Pre-performance freeze checks for Price/Lifecycle Sprint 001."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
    identity = contract["pit_lifecycle_identity"]
    bindings = {
        "instrument_master": "75184829cc7fb672f0bb3a26fa913c79ced177084303a1198088c76db2ba609d",
        "price_activity": "05c36891885bd48686b54af8967a6808ba311ed42579e8abbeb66192a0d121d3",
        "lifecycle_sidecar": "c7f0f9eaeee364a4c3ec07c65fcda767845845c77f3dfa8cfd07f5fe4f6c18dc",
        "supplemental_terminal_evidence": "5ec5624bf75006581d461cce92cb288c7aec27db9ea9b09791f48ec85b5c38e8",
        "price_lifecycle_composite": "5511caa8a0839fa9c0296f23e828988e6d1374d9cf3edaa2c8cb075e163050bd",
    }
    for name, expected in bindings.items():
        path = ROOT / identity[f"{name}_path"]
        assert path.is_file()
        assert identity[f"{name}_sha256"] == expected
        assert sha256(path) == expected
    assert identity["terminal_semantics"] == (
        "Use universe_at at the completed decision cutoff and execution boundary. "
        "The master includes the three supplemental cohort terminals absent from "
        "the exception-only Lifecycle V1 sidecar; no missing bar may itself imply "
        "termination."
    )
    assert "completed" in common["decision_information_cutoff"]
    assert "UTC open of t+1" in common["execution"]
    assert "not an untouched holdout" in common["holdout_disclosure"]
    assert common["costs"]["pass_uses"] == "coarse_stress_one_way_turnover_cost"
    assert common["costs"]["funding_omission"].startswith("Funding is not modeled")
    accounting = common["accounting_and_statistics"]
    assert accounting["event_booking"] == (
        "At an ordinary scheduled open, first charge target-minus-drifted turnover, "
        "then apply the new target over the following interval. A canonical terminal "
        "inside an interval replaces that symbol's open-to-next-open return with "
        "open-to-canonical-final-close return and books its liquidation turnover in "
        "that same interval after marking. At the frozen final exit open, book target "
        "zero against the drifted incumbent as a final cost-only observation with "
        "gross return zero; include that observation in the metric window."
    )
    assert accounting["weight_drift"] == (
        "Between scheduled rebalances, update each position weight by "
        "w_i*(1+r_i)/(1+portfolio_net_return), with residual cash absorbing trading "
        "costs. At a scheduled open, turnover is sum_i "
        "abs(target_weight_i - drifted_incumbent_weight_i)."
    )
    assert accounting["total_return"] == (
        "Compound daily net returns geometrically as product(1+r_d)-1."
    )
    assert accounting["maximum_drawdown"] == (
        "Minimum compounded-equity divided by its prior running peak minus 1 over "
        "the same metric window."
    )
    assert accounting["rank_ic"] == (
        "At each scheduled decision, compute Pearson correlation of ascending "
        "average ranks of the frozen signal and the same eligible symbols' "
        "fixed-horizon forward returns; canonical symbol order breaks ties. Report "
        "the arithmetic mean of event ICs."
    )
    assert accounting["symbol_contribution"] == (
        "Sum each symbol's daily gross PnL contribution over the full metric window; "
        "concentration is max absolute contribution divided by the sum of absolute "
        "contributions."
    )
    assert common["single_symbol_removal_rule"] == (
        "For each tested primary, identify the symbol with the largest strictly "
        "positive full-window gross PnL contribution using canonical-symbol order as "
        "the tie-break. If none exists, the sensitivity condition fails. Rerun the "
        "unchanged primary once with that symbol mechanically excluded at every "
        "decision and require the stress-cost net mean daily return not to become "
        "negative. This is a frozen sensitivity diagnostic, not a new candidate or "
        "sanity variant."
    )
    assert record["candidates"][0]["rebalance_schedule"].endswith(
        "the final scheduled exit is 2023-11-11T00:00:00Z"
    )
    assert record["candidates"][1]["rebalance_schedule"].endswith(
        "the final scheduled exit is 2023-11-13T00:00:00Z"
    )
    assert all(
        "partial horizons are forbidden" in candidate["execution_assumption"]
        for candidate in record["candidates"]
    )
    assert all(
        candidate["tier_1_pass_fail_criteria"]["fail_rule"].startswith(
            "Any failed condition"
        )
        for candidate in record["candidates"]
    )
    assert record["result_interpretation"]["ready_for_tiny_live"] is False


def test_every_primary_and_variant_has_a_complete_frozen_schedule() -> None:
    record = load(PREREGISTRATION)
    schedules = record["common_execution_and_accounting"]["frozen_schedules"]
    configured_ids = {
        candidate["hypothesis_id"]
        for candidate in record["candidates"]
    } | {
        variant["variant_id"]
        for candidate in record["candidates"]
        for variant in candidate["sanity_variants"]
    }

    assert {schedule["configuration_id"] for schedule in schedules} == configured_ids
    assert len(schedules) == len(configured_ids)
    support_end = utc(record["data_contract"]["support_end_exclusive_utc"])
    for schedule in schedules:
        first = utc(schedule["first_execution_utc"])
        final = utc(schedule["final_execution_utc"])
        exit_at = utc(schedule["final_exit_utc"])
        cadence = schedule["cadence_calendar_days"]
        assert (final - first).days % cadence == 0
        assert (exit_at - final).days == cadence
        assert exit_at < support_end


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
