from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION = ROOT / "research" / "exploration" / "price-lifecycle-sprint-002-preregistration.json"
HISTORY = ROOT / "research" / "process" / "price-lifecycle-sprint-002-program-history.json"
OSS_PRECHECK = ROOT / "research" / "process" / "price-lifecycle-sprint-002-oss-precheck.md"


def _timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_sprint_002_preserves_checkpoint_a_stop_without_performance() -> None:
    payload = json.loads(PREREGISTRATION.read_text())
    assert payload["program_id"] == "PRICE_LIFECYCLE_SPRINT_002"
    assert payload["research_tier"] == "TIER_1_EXPLORATION"
    assert payload["status"] == "CHECKPOINT_A_FAIL_CUSTOM_PATH_STOPPED"
    assert payload["frozen_before_performance"] is True
    assert [item["candidate_id"] for item in payload["candidates"]] == [
        "CAND-MARKET-BETA-RESIDUAL-MOMENTUM-001",
        "CAND-RANGE-VOLUME-ABSORPTION-001",
    ]
    assert payload["common_execution_and_accounting"]["candidate_limit"] == 2
    assert "If and only if" in payload["common_execution_and_accounting"]["sequential_stop"]
    assert [item["execution_status"] for item in payload["candidates"]] == [
        "REJECTED_AS_VARIANT_AT_CHECKPOINT_A_NO_PERFORMANCE_RUN",
        "NOT_RUN_CUSTOM_PATH_STOPPED_NO_PERFORMANCE_RUN",
    ]
    assert [item["selection_status"] for item in payload["screened_candidates"][:2]] == [
        "REJECTED_AS_VARIANT_AT_CHECKPOINT_A",
        "NOT_RUN_AFTER_CHECKPOINT_A_STOP",
    ]
    assert {item["selection_status"] for item in payload["screened_candidates"] if item["candidate_id"].startswith("CAND-INTRADAY") or item["candidate_id"].startswith("CAND-BREADTH")} == {"NOT_SELECTED_OLD_VARIANT_RISK"}


def test_schedules_have_warmup_and_complete_exits_inside_fixed_support() -> None:
    payload = json.loads(PREREGISTRATION.read_text())
    support_end = _timestamp(payload["data_contract"]["support_end_exclusive_utc"])
    first = _timestamp("2021-03-08T00:00:00Z")
    final = _timestamp("2023-11-06T00:00:00Z")
    assert (final - first).days % 7 == 0
    assert final + dt.timedelta(days=7) < support_end
    absorption_first = _timestamp("2021-02-01T00:00:00Z")
    absorption_final = _timestamp("2023-11-03T00:00:00Z")
    assert (absorption_final - absorption_first).days % 5 == 0
    assert absorption_final + dt.timedelta(days=5) < support_end
    assert payload["candidates"][0]["lookback"]["beta_returns"] == 60
    assert payload["candidates"][1]["lookback"]["trailing_quote_volume_median_days"] == 20


def test_data_lineage_is_pinned_and_fail_closed() -> None:
    payload = json.loads(PREREGISTRATION.read_text())
    identity = payload["data_contract"]["pit_lifecycle_identity"]
    for path_key, digest_key in (
        ("instrument_master_path", "instrument_master_sha256"),
        ("price_activity_path", "price_activity_sha256"),
        ("lifecycle_sidecar_path", "lifecycle_sidecar_sha256"),
        ("supplemental_terminal_evidence_path", "supplemental_terminal_evidence_sha256"),
        ("price_lifecycle_composite_path", "price_lifecycle_composite_sha256"),
    ):
        actual = hashlib.sha256((ROOT / identity[path_key]).read_bytes()).hexdigest()
        assert actual == identity[digest_key]
    text = json.dumps(payload, sort_keys=True)
    assert "DATA_ERROR_STOP" in text
    assert "Funding" in payload["data_contract"]["forbidden_observations"]
    assert "open interest" in payload["data_contract"]["forbidden_observations"]
    assert "Strategy" in payload["result_semantics"]
    assert "live" in payload["result_semantics"]


def test_tier1_gate_bundle_and_oss_precheck_are_complete() -> None:
    payload = json.loads(PREREGISTRATION.read_text())
    assert set(payload["tier1_pass_checks"]) == {
        "positive_stress_mean", "sharpe_at_least_0_50", "positive_rank_ic", "hac_p_at_most_0_05",
        "two_nonnegative_subperiods", "drawdown_no_worse_than_minus_0_35", "concentration_at_most_0_20",
        "single_positive_removal", "median_turnover_at_most_1_25", "both_variants_positive",
        "pit_lifecycle_missingness_checks",
    }
    history = json.loads(HISTORY.read_text())
    assert history["PERFORMANCE_EXECUTION_COUNT"] == 0
    assert history["CANDIDATES_TESTED"] == 0
    assert history["CUSTOM_ALPHA_PATH_EXHAUSTED"] is True
    assert history["OSS_FALLBACK_TRIGGERED"] is True
    assert history["REJECTED_AS_VARIANT_COUNT"] == 1
    assert history["NOT_RUN_COUNT"] == 1
    assert history["RESULT"] == "SWITCH_TO_OPEN_SOURCE_RESEARCH_STACK"
    assert history["SELECTED_FRAMEWORK"] == "vectorbt==1.1.0"
    assert history["OSS_POC_RESULT"] == "BENCHMARK_ONLY_NOT_ALPHA"
    precheck = OSS_PRECHECK.read_text()
    for marker in ("Freqtrade", "Jesse", "vectorbt", "NautilusTrader", "Hummingbot", "NO_EXACT_READY_REUSE"):
        assert marker in precheck
