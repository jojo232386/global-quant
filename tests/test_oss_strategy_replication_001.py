from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCREENING = ROOT / "research/exploration/oss-strategy-replication-001-screening.json"
CHECKPOINT_A = ROOT / "research/exploration/oss-strategy-replication-001-checkpoint-a.json"
CHECKPOINT_B = ROOT / "research/exploration/oss-strategy-replication-001-checkpoint-b.json"
RESULT = ROOT / "research/exploration/oss-strategy-replication-001-result.json"
HISTORY = ROOT / "research/process/oss-strategy-replication-001-program-history.json"


def _record() -> dict:
    return json.loads(SCREENING.read_text(encoding="utf-8"))


def test_blind_screening_respects_all_program_limits() -> None:
    record = _record()
    counts = record["screening_counts"]
    limits = record["screening_limits"]
    assert record["program_id"] == "OSS_STRATEGY_REPLICATION_001"
    assert counts["repositories_screened"] == limits["repositories_maximum"] == 8
    assert counts["strategy_files_screened"] == limits["strategy_files_maximum"] == 30
    assert counts["candidates_shortlisted"] == limits["shortlist_maximum"] == 5
    assert counts["candidates_admitted"] == counts["candidates_preregistered"] == 0
    assert counts["candidates_tested"] == 0
    assert sum(len(repo["strategy_files_screened"]) for repo in record["repositories"]) == 30


def test_no_performance_or_upstream_claim_selected_a_candidate() -> None:
    firewall = _record()["performance_firewall"]
    assert firewall["gmaq_performance_run"] is False
    assert firewall["gmaq_result_read"] is False
    assert firewall["upstream_performance_files_read"] is False
    assert firewall["upstream_performance_used_for_selection"] is False
    assert firewall["classification"] == "UNTRUSTED_UPSTREAM_PERFORMANCE_CLAIM_IGNORED"


def test_every_shortlist_source_is_pinned_and_rejected_before_preregistration() -> None:
    record = _record()
    assert len(record["shortlist"]) == 5
    for candidate in record["shortlist"]:
        assert candidate["repository"].startswith("https://github.com/")
        assert len(candidate["upstream_commit"]) == 40
        assert len(candidate["source_sha256"]) == 64
        assert candidate["license"] in {"GPL-3.0", "Apache-2.0", "MIT"}
        assert candidate["gmaq_compatibility"] == "FAIL"
        assert candidate["disposition"].startswith("REJECT_")


def test_fail_closed_admission_stops_without_code_or_alpha_claim() -> None:
    decision = _record()["admission_decision"]
    assert decision["result"] == "NO_ADMISSIBLE_OSS_CANDIDATE"
    assert decision["candidate_ids"] == []
    assert decision["preregistration_required"] is False
    assert decision["performance_authorized"] is False
    assert decision["parameter_rescue"] is False
    assert decision["third_party_code_committed"] is False
    assert decision["new_infrastructure_written"] is False
    assert decision["next_action"].startswith("STOP_AND_KEEP_FORWARD_CAPTURE_RUNNING")


def test_consumed_window_is_not_relabelled_as_holdout() -> None:
    record = _record()
    assert record["research_tier"] == "TIER_1_EXPLORATION"
    assert "consumed exploration data" in record["data_window_semantics"]
    assert "not a holdout" in record["data_window_semantics"]


def test_checkpoint_a_passes_only_to_stop_performance() -> None:
    checkpoint = json.loads(CHECKPOINT_A.read_text(encoding="utf-8"))
    assert checkpoint["checkpoint_a"] == "PASS"
    assert checkpoint["admission_decision"] == "NO_ADMISSIBLE_OSS_CANDIDATE"
    assert checkpoint["performance_authorization"] == "DENIED"
    assert checkpoint["request_changes"] == "NONE"
    assert checkpoint["performance_run_before_review"] is False
    assert checkpoint["source_semantic_parity_run"] is False


def test_result_and_program_history_close_without_empirical_failures() -> None:
    result = json.loads(RESULT.read_text(encoding="utf-8"))
    history = json.loads(HISTORY.read_text(encoding="utf-8"))
    assert result["result"] == history["PROGRAM_RESULT"] == "NO_ADMISSIBLE_OSS_CANDIDATE"
    assert result["candidates_tested"] == history["CANDIDATES_TESTED"] == 0
    assert result["pass_count"] == result["fail_count"] == 0
    assert history["PASS_COUNT"] == history["FAIL_COUNT"] == 0
    assert result["data_windows_viewed"] == history["DATA_WINDOWS_VIEWED"] == 0
    assert result["source_semantic_parity"] == "NOT_RUN_NO_ADMISSIBLE_CANDIDATE"
    assert result["factor_graveyard_updated"] is False
    assert history["THIRD_PARTY_CODE_COMMITTED"] is False
    assert history["NEW_INFRASTRUCTURE_WRITTEN"] is False
    assert history["VECTORBT_NEW_CODE_LOC"] == 0
    assert history["REAL_ORDER_COUNT"] == 0
    assert history["READY_FOR_STRATEGY"] is False
    assert history["READY_FOR_TINY_LIVE"] is False


def test_checkpoint_b_approves_the_fail_closed_record() -> None:
    checkpoint = json.loads(CHECKPOINT_B.read_text(encoding="utf-8"))
    verified = checkpoint["verified"]
    assert checkpoint["approve"] is True
    assert checkpoint["request_changes"] == "NONE"
    assert checkpoint["regression_risk"] == "CLEARED"
    assert checkpoint["program_result"] == "NO_ADMISSIBLE_OSS_CANDIDATE"
    assert verified["screening_limits_respected"] is True
    assert verified["zero_candidates_admitted_preregistered_or_tested"] is True
    assert verified["performance_not_run"] is True
    assert verified["upstream_performance_not_used_for_selection"] is True
    assert verified["source_and_license_metadata_recorded"] is True
    assert verified["third_party_source_committed"] is False
    assert verified["parameter_timeframe_or_direction_rescue"] is False
    assert verified["holdout_used"] is False
    assert verified["runtime_freqtrade_or_forward_capture_changed"] is False
    assert verified["factor_graveyard_empirical_failure_added"] is False
    assert verified["alpha_promotion"] is False
    assert verified["new_infrastructure_written"] is False
