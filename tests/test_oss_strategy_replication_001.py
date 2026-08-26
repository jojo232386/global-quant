from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCREENING = ROOT / "research/exploration/oss-strategy-replication-001-screening.json"


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
