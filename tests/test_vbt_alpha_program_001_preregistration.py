from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "research/exploration/vbt-alpha-program-001-preregistration.json"
CHECKPOINT = ROOT / "research/exploration/vbt-alpha-program-001-checkpoint-a.json"


def _record() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_program_is_frozen_before_any_performance() -> None:
    record = _record()
    assert record["program_id"] == "VBT_ALPHA_PROGRAM_001"
    assert record["status"] == "PREREGISTERED_PRE_PERFORMANCE_CHECKPOINT_A_PENDING"
    assert record["frozen_before_performance"] is True
    assert record["performance_execution_count_at_freeze"] == 0
    assert record["candidates_generated"] == 3
    assert record["candidates_preregistered"] == record["candidate_limit"] == 2
    assert record["checkpoint_a"]["status"] == "PENDING"


def test_data_timing_and_tier_boundaries_are_fail_closed() -> None:
    record = _record()
    data = record["data_contract"]
    common = record["common_execution_and_evaluation"]
    assert record["research_tier"] == "TIER_1_EXPLORATION"
    assert record["exploration_only"] is True
    assert "No untouched holdout" in data["holdout_semantics"]
    assert "next UTC daily open" in common["execution_timestamp"]
    assert "without reranking or renormalizing" in data["membership_rule"]
    assert "without forward fill" in data["lifecycle_rule"]
    assert len(data["instrument_master_sha256"]) == 64
    assert len(data["lifecycle_sidecar_sha256"]) == 64
    assert "high >= max(open,close)" in data["ohlc_loader_extension"]
    assert common["portfolio_path"].endswith(
        "ffill_val_price=false and fillna_close=false."
    )
    forbidden = " ".join(data["forbidden_fields"]).lower()
    for fragment in ("funding", "open interest", "forward capture", "exchangeinfo"):
        assert fragment in forbidden
    assert record["framework_role"] == "RESEARCH_ONLY"
    assert record["freqtrade_role"].endswith("UNCHANGED")


def test_preperformance_simulator_preflight_is_mandatory_and_metric_free() -> None:
    preflight = _record()["pre_performance_simulator_preflight"]
    assert preflight["required"] is True
    assert preflight["real_candidate_data_permitted"] is False
    assert "target-percent long and short orders" in preflight["contract"]
    assert "no candidate performance" in preflight["gate"]
    for forbidden in ("return", "pnl", "sharpe", "drawdown", "ic"):
        assert forbidden in preflight["contract"].lower()


def test_candidates_are_distinct_and_have_one_neighbor_each() -> None:
    record = _record()
    candidates = record["candidates"]
    assert [item["order"] for item in candidates] == [1, 2]
    assert len({item["mechanism_family"] for item in candidates}) == 2
    assert len({item["candidate_id"] for item in candidates}) == 2
    for item in candidates:
        assert item["signal_formula"]
        assert item["primary_lookback"]
        assert set(item["sanity_neighbor"]) == {"neighbor_id", "only_change"}
        assert item["holding_period"]
        assert item["rebalance"]
        assert item["universe"]
        assert item["candidate_missingness"]
        assert item["implementation_contract"]


def test_pass_is_all_gates_and_rescue_is_forbidden() -> None:
    record = _record()
    gates = record["pass_fail_criteria"]
    assert gates["overall"] == "TIER1_PASS only if every criterion passes; otherwise TIER1_FAIL"
    assert "p <= 0.05" in gates["predictive_direction"]
    assert "at least 100" in gates["observations"]
    assert "positive 30 bps stress" in gates["sanity_neighbor"]
    prohibitions = record["common_execution_and_evaluation"]["post_result_prohibitions"]
    for fragment in ("direction", "lookback", "universe", "costs", "success criteria", "variants"):
        assert fragment in prohibitions


def test_generated_third_idea_is_rejected_before_preregistration() -> None:
    pool = _record()["generated_candidate_pool"]
    assert len(pool) == 3
    rejected = [item for item in pool if item["selection_status"].startswith("NOT_SELECTED")]
    assert len(rejected) == 1
    assert "OLD_FACTOR_VARIANT_RISK" in rejected[0]["selection_status"]


def test_candidate_schedules_and_history_gate_are_exact() -> None:
    record = _record()
    first, second = record["candidates"]
    assert "2021-02-08" in first["rebalance"]
    assert "2023-11-10" in first["rebalance"]
    assert "k=0..L-1" in second["signal_formula"]
    assert "t-L through t" in second["signal_formula"]
    sequential = record["common_execution_and_evaluation"]["sequential_stop"]
    assert "vbt-alpha-program-001-program-history.json" in sequential
    assert "committed before Candidate 2 performance" in sequential


def test_checkpoint_a_passed_before_performance() -> None:
    checkpoint = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    assert checkpoint["program_id"] == "VBT_ALPHA_PROGRAM_001"
    assert checkpoint["preregistration_commit"] == "c2cb8661b52625234e9dd11065a05b6655cc8673"
    assert checkpoint["final_verdict"] == "PASS"
    assert checkpoint["request_changes"] == "NONE"
    assert checkpoint["regression_risk"] == "CLEARED"
    assert checkpoint["performance_run_before_review"] is False
    assert checkpoint["performance_run_before_final_verdict"] is False
    assert checkpoint["preflight_result"] == "PASS_METRIC_FREE"
