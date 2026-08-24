"""Contract tests for the local, fail-closed GMAQ Research Tier v1 proposal."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROCESS = ROOT / "research" / "process"


def load_tier_module():
    path = PROCESS / "research_tier.py"
    spec = importlib.util.spec_from_file_location("research_tier", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["research_tier"] = module
    spec.loader.exec_module(module)
    return module


tier = load_tier_module()


def tier_1_record() -> dict[str, object]:
    return {
        "RESEARCH_TIER": tier.TIER_1,
        "RECORD_ID": "CAND-EXAMPLE-001",
        "EXPLORATION_ONLY": True,
        "OUTPUT_CLASS": "EXPLORATION_RESULT",
        "DATA_LIMITATIONS": "Public data has limited venue coverage.",
        "PIT_LIMITATIONS": "Publication timestamps are not yet proven.",
        "KNOWN_BIAS": "Survivor bias is possible.",
        "RESULT_STATUS": "MECHANISM_VALID",
        "NEXT_STAGE": "CANDIDATE_REVIEW",
        "READY_FOR_TINY_LIVE": False,
    }


def tier_2_record() -> dict[str, object]:
    return {
        "RESEARCH_TIER": tier.TIER_2,
        "RECORD_ID": "CONFIRMATION-EXAMPLE-001",
        "HYPOTHESIS_ID": "HYPOTHESIS-EXAMPLE-001",
        "PROGRAM_ID": "GMAQ-EXAMPLE-PROGRAM-001",
        "PROGRAM_FORMAL_HYPOTHESIS_COUNTED": False,
        "TIER_1_RECORD_ID": "EXPLORATION-EXAMPLE-001",
        "CANDIDATE_REVIEW_RECORD_ID": "CANDIDATE-REVIEW-EXAMPLE-001",
        "DATA_ADMISSION_RECORD_ID": "DATA-ADMISSION-EXAMPLE-001",
        "PIT_CREDIBLE": True,
        "LIFECYCLE_CREDIBLE": True,
        "GOLD_SAMPLE": True,
        "INDEPENDENT_REVIEW": True,
        "HOLDOUT": True,
        "PRIOR_TIER_1_DATA_SOURCE_IDS": ["SOURCE_PRICE_V1"],
        "PRIOR_TIER_1_TIME_WINDOW_IDS": ["GMAQ_2021", "GMAQ_2022", "GMAQ_2023"],
        "PRIOR_TIER_1_UNIVERSE_IDS": ["PIT_PERPETUAL_V1"],
        "PRIOR_TIER_1_DATASET_FAMILY_IDS": ["PRICE_V1_CURATED"],
        "PRIOR_TIER_1_SAMPLE_IDS": ["SAMPLE_PRICE_V1"],
        "CONFIRMATION_DATA_SOURCE_IDS": ["SOURCE_PRICE_V1"],
        "CONFIRMATION_TIME_WINDOW_IDS": ["GMAQ_2024"],
        "CONFIRMATION_UNIVERSE_IDS": ["PIT_PERPETUAL_V1"],
        "CONFIRMATION_DATASET_FAMILY_IDS": ["PRICE_V1_CURATED"],
        "CONFIRMATION_SAMPLE_IDS": ["SAMPLE_PRICE_V1"],
        "INDEPENDENCE_BASIS": "TEMPORAL_NEW_WINDOW",
        "INDEPENDENCE_EVIDENCE": "A complete post-discovery GMAQ_2024 window is bound.",
        "UNIVERSE_DEFINITION": "PIT-defined eligible perpetual universe.",
        "COVERAGE_LIMITATIONS": "Only the declared venues are included.",
        "RESULT_STATUS": "CONFIRMED_CANDIDATE",
        "BLOCK_REASON": "NONE",
        "NEXT_STAGE": "FREEZE",
        "READY_FOR_TINY_LIVE": False,
    }


def tier_3_record() -> dict[str, object]:
    return {
        "RESEARCH_TIER": tier.TIER_3,
        "RECORD_ID": "FORMAL-EXAMPLE-001",
        "HYPOTHESIS_ID": "HYPOTHESIS-EXAMPLE-001",
        "PROGRAM_ID": "GMAQ-EXAMPLE-PROGRAM-001",
        "PROGRAM_FORMAL_HYPOTHESIS_COUNTED": True,
        "TIER_2_RECORD_ID": "CONFIRMATION-EXAMPLE-001",
        "TIER_2_RESULT_STATUS": "CONFIRMED_CANDIDATE",
        "FREEZE_RECORD_ID": "FREEZE-EXAMPLE-001",
        "FORMAL_RUN_ID": "FORMAL-RUN-EXAMPLE-001",
        "FORMAL_RUN_RESULT_STATUS": "PASS",
        "PIT_COMPLETE": True,
        "LIFECYCLE_COMPLETE": True,
        "UNIVERSE_COMPLETE": True,
        "COSTS_COMPLETE": True,
        "EXECUTION_ASSUMPTIONS_COMPLETE": True,
        "SLIPPAGE_COMPLETE": True,
        "CONCENTRATION_COMPLETE": True,
        "ROBUSTNESS_COMPLETE": True,
        "FREEZE_COMPLETE": True,
        "FORMAL_RUN_COMPLETE": True,
        "STRATEGY_REVIEW_COMPLETE": True,
        "DRY_RUN_COMPLETE": True,
        "RELIABILITY_REVIEW_COMPLETE": True,
        "RISK_REVIEW_COMPLETE": True,
        "RESULT_STATUS": "PRODUCTION_CANDIDATE",
        "NEXT_STAGE": "LIVE",
        "READY_FOR_TINY_LIVE": True,
    }


def test_tier_1_is_exploration_only_and_cannot_jump() -> None:
    record = tier_1_record()
    assert tier.validate_tier_record(record) == record
    with pytest.raises(tier.TierRecordError, match="never Alpha"):
        tier.validate_tier_record(dict(record, OUTPUT_CLASS="ALPHA"))
    with pytest.raises(tier.TierRecordError, match="cannot jump"):
        tier.validate_tier_record(dict(record, NEXT_STAGE="STRATEGY"))
    with pytest.raises(tier.TierRecordError, match="never be READY"):
        tier.validate_tier_record(dict(record, READY_FOR_TINY_LIVE=True))


def test_fail_results_must_stop_even_after_identifier_changes() -> None:
    tier_1_failure = dict(
        tier_1_record(),
        RECORD_ID="RENAMED-EXPLORATION-999",
        RESULT_STATUS="FAIL",
        NEXT_STAGE="CANDIDATE_REVIEW",
    )
    with pytest.raises(tier.TierRecordError, match="FAIL must set NEXT_STAGE = STOP"):
        tier.validate_tier_record(tier_1_failure)

    tier_2_failure = dict(
        tier_2_record(),
        RECORD_ID="RENAMED-CONFIRMATION-999",
        HYPOTHESIS_ID="RENAMED-HYPOTHESIS-999",
        PROGRAM_FORMAL_HYPOTHESIS_COUNTED=True,
        RESULT_STATUS="FAIL",
        NEXT_STAGE="FREEZE",
    )
    with pytest.raises(tier.TierRecordError, match="FAIL → STOP"):
        tier.validate_tier_record(tier_2_failure)

    tier_3_failure = dict(
        tier_3_record(),
        RECORD_ID="RENAMED-FORMAL-999",
        HYPOTHESIS_ID="RENAMED-HYPOTHESIS-999",
        RESULT_STATUS="FAIL",
        NEXT_STAGE="STRATEGY",
        READY_FOR_TINY_LIVE=False,
    )
    with pytest.raises(tier.TierRecordError, match="FAIL must set NEXT_STAGE = STOP"):
        tier.validate_tier_record(tier_3_failure)


def test_placeholders_and_incomplete_lineage_are_rejected() -> None:
    with pytest.raises(tier.TierRecordError, match="actual limitation"):
        tier.validate_tier_record(dict(tier_1_record(), DATA_LIMITATIONS="UNVERIFIED"))
    with pytest.raises(tier.TierRecordError, match="must identify an existing record"):
        tier.validate_tier_record(dict(tier_1_record(), RECORD_ID="UNASSIGNED"))

    for key in (
        "RECORD_ID",
        "HYPOTHESIS_ID",
        "PROGRAM_ID",
        "TIER_1_RECORD_ID",
        "CANDIDATE_REVIEW_RECORD_ID",
        "DATA_ADMISSION_RECORD_ID",
    ):
        with pytest.raises(tier.TierRecordError, match="must identify an existing record"):
            tier.validate_tier_record(dict(tier_2_record(), **{key: "UNASSIGNED"}))
    with pytest.raises(tier.TierRecordError, match="actual limitation"):
        tier.validate_tier_record(dict(tier_2_record(), UNIVERSE_DEFINITION="UNKNOWN"))
    incomplete_admission = tier_2_record()
    incomplete_admission.pop("DATA_ADMISSION_RECORD_ID")
    with pytest.raises(tier.TierRecordError, match="keys differ"):
        tier.validate_tier_record(incomplete_admission)

    for key in ("TIER_2_RECORD_ID", "FREEZE_RECORD_ID", "FORMAL_RUN_ID"):
        with pytest.raises(tier.TierRecordError, match="must identify an existing record"):
            tier.validate_tier_record(dict(tier_3_record(), **{key: "UNASSIGNED"}))
    incomplete_formal_lineage = tier_3_record()
    incomplete_formal_lineage.pop("FORMAL_RUN_ID")
    with pytest.raises(tier.TierRecordError, match="keys differ"):
        tier.validate_tier_record(incomplete_formal_lineage)


def test_tier_1_and_tier_2_cannot_enter_live_or_tiny_live() -> None:
    with pytest.raises(tier.TierRecordError, match="cannot jump"):
        tier.validate_tier_record(dict(tier_1_record(), NEXT_STAGE="LIVE"))
    with pytest.raises(tier.TierRecordError, match="never be READY"):
        tier.validate_tier_record(dict(tier_1_record(), READY_FOR_TINY_LIVE=True))

    with pytest.raises(tier.TierRecordError, match="CONFIRMED_CANDIDATE → FREEZE"):
        tier.validate_tier_record(dict(tier_2_record(), NEXT_STAGE="LIVE"))
    with pytest.raises(tier.TierRecordError, match="never be READY"):
        tier.validate_tier_record(dict(tier_2_record(), READY_FOR_TINY_LIVE=True))


def test_tier_2_accepts_a_genuinely_new_future_window() -> None:
    record = tier_2_record()
    assert tier.validate_tier_record(record) == record


@pytest.mark.parametrize("window", ["GMAQ_2022", "GMAQ_2023"])
def test_tier_2_rejects_relabelled_or_consumed_temporal_holdout(window: str) -> None:
    record = dict(tier_2_record(), CONFIRMATION_TIME_WINDOW_IDS=[window])
    with pytest.raises(tier.TierRecordError, match="cannot claim temporal independence"):
        tier.validate_tier_record(record)


def test_renamed_ids_do_not_turn_seen_information_into_confirmation() -> None:
    record = dict(
        tier_2_record(),
        RECORD_ID="RENAMED-RUN-999",
        HYPOTHESIS_ID="RENAMED-HYPOTHESIS-999",
        TIER_1_RECORD_ID="RENAMED-FILE-999",
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        INDEPENDENCE_BASIS="INSUFFICIENT",
    )
    with pytest.raises(tier.TierRecordError, match="require independent confirmation"):
        tier.validate_tier_record(record)

    renamed_formal_failure = dict(
        tier_2_record(),
        RECORD_ID="RENAMED-FORMAL-CONFIRMATION-999",
        HYPOTHESIS_ID="RENAMED-FORMAL-HYPOTHESIS-999",
        TIER_1_RECORD_ID="RENAMED-EXPLORATION-999",
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        PROGRAM_FORMAL_HYPOTHESIS_COUNTED=True,
        RESULT_STATUS="FAIL",
        NEXT_STAGE="STOP",
    )
    with pytest.raises(tier.TierRecordError, match="cannot claim temporal independence"):
        tier.validate_tier_record(renamed_formal_failure)


def test_tier_2_accepts_truly_independent_source_or_sample() -> None:
    source = dict(
        tier_2_record(),
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        CONFIRMATION_DATA_SOURCE_IDS=["SOURCE_VENDOR_B"],
        INDEPENDENCE_BASIS="INDEPENDENT_SOURCE",
        INDEPENDENCE_EVIDENCE="A separately documented source is bound.",
    )
    assert tier.validate_tier_record(source) == source
    sample = dict(
        tier_2_record(),
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        CONFIRMATION_SAMPLE_IDS=["SAMPLE_VENDOR_B"],
        INDEPENDENCE_BASIS="INDEPENDENT_SAMPLE",
        INDEPENDENCE_EVIDENCE="A separately documented sample is bound.",
    )
    assert tier.validate_tier_record(sample) == sample


def test_insufficient_information_fails_closed_to_confirmation_blocked() -> None:
    blocked = dict(
        tier_2_record(),
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        INDEPENDENCE_BASIS="INSUFFICIENT",
        RESULT_STATUS="CONFIRMATION_BLOCKED",
        BLOCK_REASON="ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED",
        NEXT_STAGE="STOP",
    )
    assert tier.validate_tier_record(blocked) == blocked
    with pytest.raises(tier.TierRecordError, match="CONFIRMATION_BLOCKED requires"):
        tier.validate_tier_record(dict(blocked, NEXT_STAGE="FREEZE"))
    with pytest.raises(tier.TierRecordError, match="require independent confirmation"):
        tier.validate_tier_record(dict(blocked, RESULT_STATUS="FAIL", BLOCK_REASON="NONE"))


def test_tier_2_preserves_pit_lifecycle_review_and_exact_schema() -> None:
    record = tier_2_record()
    with pytest.raises(tier.TierRecordError, match="LIFECYCLE_CREDIBLE is not complete"):
        tier.validate_tier_record(dict(record, LIFECYCLE_CREDIBLE=False))
    with pytest.raises(tier.TierRecordError, match="keys differ"):
        tier.validate_tier_record(dict(record, UNSUPPORTED="no"))
    with pytest.raises(tier.TierRecordError, match="must not contain duplicate"):
        tier.validate_tier_record(dict(record, CONFIRMATION_SAMPLE_IDS=["S", "S"]))
    with pytest.raises(tier.TierRecordError, match="existing information"):
        tier.validate_tier_record(dict(record, CONFIRMATION_SAMPLE_IDS=["UNASSIGNED"]))


def test_formal_fail_and_tier_3_must_be_counted_in_program_history() -> None:
    failed = dict(
        tier_2_record(),
        RESULT_STATUS="FAIL",
        BLOCK_REASON="NONE",
        NEXT_STAGE="STOP",
        PROGRAM_FORMAL_HYPOTHESIS_COUNTED=False,
    )
    with pytest.raises(tier.TierRecordError, match="must be counted in program history"):
        tier.validate_tier_record(failed)
    assert tier.validate_tier_record(
        dict(failed, PROGRAM_FORMAL_HYPOTHESIS_COUNTED=True)
    )["RESULT_STATUS"] == "FAIL"
    with pytest.raises(tier.TierRecordError, match="Tier 3 formal hypothesis must be counted"):
        tier.validate_tier_record(dict(tier_3_record(), PROGRAM_FORMAL_HYPOTHESIS_COUNTED=False))


def test_tier_3_only_allows_ready_after_all_reviews_and_formal_pass() -> None:
    record = tier_3_record()
    assert tier.validate_tier_record(record) == record
    with pytest.raises(tier.TierRecordError, match="all separate reviews"):
        tier.validate_tier_record(dict(record, RISK_REVIEW_COMPLETE=False))
    with pytest.raises(tier.TierRecordError, match="PASS from the frozen"):
        tier.validate_tier_record(dict(record, FORMAL_RUN_RESULT_STATUS="FAIL"))
    with pytest.raises(tier.TierRecordError, match="FORMAL_RUN_COMPLETE is not complete"):
        tier.validate_tier_record(dict(record, FORMAL_RUN_COMPLETE=False))
    with pytest.raises(tier.TierRecordError, match="Tier 2 CONFIRMED_CANDIDATE"):
        tier.validate_tier_record(dict(record, TIER_2_RESULT_STATUS="CONFIRMATION_BLOCKED"))
    with pytest.raises(tier.TierRecordError, match="LIVE requires"):
        tier.validate_tier_record(dict(record, READY_FOR_TINY_LIVE=False))


def test_stage_prerequisites_and_blocked_admission_cannot_be_bypassed() -> None:
    for gate in tier.TIER_2_GATES:
        with pytest.raises(tier.TierRecordError, match=f"{gate} is not complete"):
            tier.validate_tier_record(dict(tier_2_record(), **{gate: False}))

    blocked = dict(
        tier_2_record(),
        CONFIRMATION_TIME_WINDOW_IDS=["GMAQ_2023"],
        INDEPENDENCE_BASIS="INSUFFICIENT",
        RESULT_STATUS="CONFIRMATION_BLOCKED",
        BLOCK_REASON="ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED",
        NEXT_STAGE="STOP",
    )
    with pytest.raises(tier.TierRecordError, match="require independent confirmation"):
        tier.validate_tier_record(
            dict(
                blocked,
                RESULT_STATUS="CONFIRMED_CANDIDATE",
                BLOCK_REASON="NONE",
                NEXT_STAGE="FREEZE",
            )
        )

    for predecessor_status in ("FAIL", "CONFIRMATION_BLOCKED"):
        with pytest.raises(tier.TierRecordError, match="Tier 2 CONFIRMED_CANDIDATE"):
            tier.validate_tier_record(
                dict(tier_3_record(), TIER_2_RESULT_STATUS=predecessor_status)
            )

    review_prerequisites = (
        ("DRY_RUN", {"STRATEGY_REVIEW_COMPLETE": False}),
        ("RELIABILITY_REVIEW", {"DRY_RUN_COMPLETE": False}),
        ("RISK_REVIEW", {"RELIABILITY_REVIEW_COMPLETE": False}),
    )
    for next_stage, missing_review in review_prerequisites:
        incomplete = dict(
            tier_3_record(),
            NEXT_STAGE=next_stage,
            READY_FOR_TINY_LIVE=False,
            **missing_review,
        )
        with pytest.raises(tier.TierRecordError, match="requires prior Tier 3 reviews"):
            tier.validate_tier_record(incomplete)


def test_program_history_is_thin_auditable_and_retains_expl_017_failure() -> None:
    history = tier.load_and_validate_program_history(PROCESS / "GMAQ_PROGRAM_HISTORY_V1.json")
    assert history["FORMAL_HYPOTHESIS_IDS"] == ["EXPL-017"]
    assert history["FORMAL_HYPOTHESES_TESTED"] == 1
    assert history["PASS_COUNT"] == 0
    assert history["FAIL_COUNT"] == 1
    assert history["HOLDOUT_WINDOWS_CONSUMED"] == ["GMAQ_2023"]
    with pytest.raises(tier.TierRecordError, match="unique FORMAL_HYPOTHESIS_IDS"):
        tier.validate_program_history(dict(history, FORMAL_HYPOTHESES_TESTED=2))
    with pytest.raises(tier.TierRecordError, match="duplicate identifiers"):
        tier.validate_program_history(dict(history, FORMAL_HYPOTHESIS_IDS=["EXPL-017", "EXPL-017"], FORMAL_HYPOTHESES_TESTED=2))
    with pytest.raises(tier.TierRecordError, match=r"PASS_COUNT \+ FAIL_COUNT"):
        tier.validate_program_history(dict(history, PASS_COUNT=1))


def test_documents_are_canonical_neutral_and_preserve_required_rules() -> None:
    standard = (PROCESS / "GMAQ_RESEARCH_TIER_V1.md").read_text()
    for marker in (
        "LOCAL_PROPOSAL_UNTIL_MERGED",
        "83095b3a0ae575a29fde4bb538f5e346804e91a9",
        "GMAQ_RESEARCH_PROTOCOL_V2`, active",
        "no separately versioned canonical Pipeline",
        "no versioned Roadmap is canonical",
        "no Tier standard exists on canonical main",
        "GMAQ_2023` was consumed by EXPL-017 FORMAL-003",
        "CONFIRMATION_BLOCKED",
        "ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED",
        "One-shot formal evaluation is not program-level statistical independence",
        "Route A — Existing Data Exploration",
        "Route B — External Data Acquisition feasibility",
    ):
        assert marker in standard
    assert "GMAQ_RESEARCH_PROTOCOL_V3.md" not in standard
    assert not (PROCESS / "GMAQ_RESEARCH_PROTOCOL_V3.md").exists()
    assert not (ROOT / "tests" / "test_research_pipeline_v3.py").exists()
    assert "GMAQ_RESEARCH_PROTOCOL_V2.md" in (ROOT / "research" / "README.md").read_text()
    register = (PROCESS / "RESEARCH_TIER_CLASSIFICATION_REGISTER_V1.md").read_text()
    assert "EXPL-017 | Tier 2 Confirmation Completed | FAIL" in register
    assert "Candidate B — Funding/OI | Tier 1 Mechanism Valid | Tier 2 Data Admission Blocked" in register
    assert "Candidate C — Unlock/Floating Supply | Tier 1 Mechanism Valid | Tier 2 Data Admission Blocked" in register
    template = (PROCESS / "RESEARCH_TIER_RECORD_TEMPLATE.md").read_text()
    for field in (*tier.TIER_2_PRIOR_LIST_FIELDS, *tier.TIER_2_CONFIRMATION_LIST_FIELDS):
        assert field in template


def test_loaders_reject_duplicate_json_keys(tmp_path: pathlib.Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"RESEARCH_TIER": "TIER_1_EXPLORATION", "RESEARCH_TIER": "TIER_3_PRODUCTION_CANDIDATE"}')
    with pytest.raises(tier.TierRecordError, match="duplicate record key"):
        tier.load_and_validate_tier_record(duplicate)
    valid = tmp_path / "tier-1.json"
    record = tier_1_record()
    valid.write_text(json.dumps(record))
    assert tier.load_and_validate_tier_record(valid) == record
