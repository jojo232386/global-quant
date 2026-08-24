"""Contract tests for the additive, fail-closed GMAQ Research Tier v1 layer."""

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
        "RECORD_ID": "HYP-EXAMPLE-001",
        "HYPOTHESIS_ID": "HYPOTHESIS-EXAMPLE-001",
        "TIER_1_RECORD_ID": "EXPLORATION-EXAMPLE-001",
        "CANDIDATE_REVIEW_RECORD_ID": "CANDIDATE-REVIEW-EXAMPLE-001",
        "DATA_ADMISSION_RECORD_ID": "DATA-ADMISSION-EXAMPLE-001",
        "PIT_CREDIBLE": True,
        "LIFECYCLE_CREDIBLE": True,
        "GOLD_SAMPLE": True,
        "INDEPENDENT_REVIEW": True,
        "HOLDOUT": True,
        "UNIVERSE_DEFINITION": "PIT-defined eligible perpetual universe.",
        "COVERAGE_LIMITATIONS": "Only the declared venues are included.",
        "RESULT_STATUS": "CONFIRMED_CANDIDATE",
        "NEXT_STAGE": "FREEZE",
        "READY_FOR_TINY_LIVE": False,
    }


def tier_3_record() -> dict[str, object]:
    return {
        "RESEARCH_TIER": tier.TIER_3,
        "RECORD_ID": "FORMAL-EXAMPLE-001",
        "HYPOTHESIS_ID": "HYPOTHESIS-EXAMPLE-001",
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


def test_tier_1_is_exploration_only_and_cannot_use_alpha_or_jump() -> None:
    record = tier_1_record()
    assert tier.validate_tier_record(record) == record
    alpha = dict(record, OUTPUT_CLASS="ALPHA")
    with pytest.raises(tier.TierRecordError, match="never Alpha"):
        tier.validate_tier_record(alpha)
    strategy = dict(record, NEXT_STAGE="STRATEGY")
    with pytest.raises(tier.TierRecordError, match="cannot jump"):
        tier.validate_tier_record(strategy)
    ready = dict(record, READY_FOR_TINY_LIVE=True)
    with pytest.raises(tier.TierRecordError, match="never be READY"):
        tier.validate_tier_record(ready)
    undocumented_limit = dict(record, DATA_LIMITATIONS="UNVERIFIED")
    with pytest.raises(tier.TierRecordError, match="actual limitation"):
        tier.validate_tier_record(undocumented_limit)
    failed_but_advancing = dict(record, RESULT_STATUS="FAIL", NEXT_STAGE="CANDIDATE_REVIEW")
    with pytest.raises(tier.TierRecordError, match="FAIL must set NEXT_STAGE = STOP"):
        tier.validate_tier_record(failed_but_advancing)
    unassigned = dict(record, RECORD_ID="UNASSIGNED")
    with pytest.raises(tier.TierRecordError, match="must identify an existing record"):
        tier.validate_tier_record(unassigned)


def test_tier_2_requires_pit_lifecycle_universe_gold_review_holdout_and_freeze() -> None:
    record = tier_2_record()
    assert tier.validate_tier_record(record) == record
    no_lifecycle = dict(record, LIFECYCLE_CREDIBLE=False)
    with pytest.raises(tier.TierRecordError, match="LIFECYCLE_CREDIBLE is not complete"):
        tier.validate_tier_record(no_lifecycle)
    unknown_universe = dict(record, UNIVERSE_DEFINITION="UNKNOWN")
    with pytest.raises(tier.TierRecordError, match="actual limitation or definition"):
        tier.validate_tier_record(unknown_universe)
    direct_live = dict(record, NEXT_STAGE="LIVE")
    with pytest.raises(tier.TierRecordError, match="CONFIRMED_CANDIDATE"):
        tier.validate_tier_record(direct_live)
    missing_lineage = dict(record, TIER_1_RECORD_ID="UNASSIGNED")
    with pytest.raises(tier.TierRecordError, match="must identify an existing record"):
        tier.validate_tier_record(missing_lineage)


def test_only_complete_tier_3_may_be_ready_for_tiny_live() -> None:
    record = tier_3_record()
    assert tier.validate_tier_record(record) == record
    no_risk_review = dict(record, RISK_REVIEW_COMPLETE=False)
    with pytest.raises(tier.TierRecordError, match="all separate reviews"):
        tier.validate_tier_record(no_risk_review)
    no_readiness = dict(record, READY_FOR_TINY_LIVE=False)
    with pytest.raises(tier.TierRecordError, match="LIVE requires"):
        tier.validate_tier_record(no_readiness)
    wrong_predecessor = dict(record, TIER_2_RESULT_STATUS="FAIL")
    with pytest.raises(tier.TierRecordError, match="Tier 2 CONFIRMED_CANDIDATE"):
        tier.validate_tier_record(wrong_predecessor)
    incomplete_formal = dict(record, FORMAL_RUN_COMPLETE=False)
    with pytest.raises(tier.TierRecordError, match="FORMAL_RUN_COMPLETE is not complete"):
        tier.validate_tier_record(incomplete_formal)
    failed_formal = dict(record, FORMAL_RUN_RESULT_STATUS="FAIL")
    with pytest.raises(tier.TierRecordError, match="PASS from the frozen formal evaluation"):
        tier.validate_tier_record(failed_formal)
    failed_but_advancing = dict(
        record, RESULT_STATUS="FAIL", NEXT_STAGE="STRATEGY", READY_FOR_TINY_LIVE=False,
    )
    with pytest.raises(tier.TierRecordError, match="FAIL must set NEXT_STAGE = STOP"):
        tier.validate_tier_record(failed_but_advancing)


def test_tier_3_requires_explicit_predecessor_freeze_and_formal_lineage() -> None:
    record = tier_3_record()
    for key in ("HYPOTHESIS_ID", "TIER_2_RECORD_ID", "FREEZE_RECORD_ID", "FORMAL_RUN_ID"):
        missing = dict(record)
        missing.pop(key)
        with pytest.raises(tier.TierRecordError, match="keys differ"):
            tier.validate_tier_record(missing)


def test_tier_3_review_stages_cannot_skip_prerequisites() -> None:
    record = tier_3_record()
    direct_dry_run = dict(
        record,
        NEXT_STAGE="DRY_RUN",
        STRATEGY_REVIEW_COMPLETE=False,
        READY_FOR_TINY_LIVE=False,
    )
    with pytest.raises(tier.TierRecordError, match="DRY_RUN requires prior"):
        tier.validate_tier_record(direct_dry_run)


def test_loader_rejects_duplicate_or_extra_record_fields(tmp_path: pathlib.Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"RESEARCH_TIER": "TIER_1_EXPLORATION", "RESEARCH_TIER": "TIER_3_PRODUCTION_CANDIDATE"}')
    with pytest.raises(tier.TierRecordError, match="duplicate tier record key"):
        tier.load_and_validate_tier_record(duplicate)
    extra = dict(tier_1_record(), UNSUPPORTED="no")
    with pytest.raises(tier.TierRecordError, match="keys differ"):
        tier.validate_tier_record(extra)


def test_tier_documents_and_templates_preserve_required_rules() -> None:
    protocol = (PROCESS / "GMAQ_RESEARCH_PROTOCOL_V3.md").read_text()
    assert "GMAQ Research Tier System v1" in protocol
    standard = (PROCESS / "GMAQ_RESEARCH_TIER_V1.md").read_text()
    for marker in (
        "GMAQ_RESEARCH_TIER_V1",
        "Idea → Tier 1 Exploration → Candidate Review → Data Admission → Tier 2 Confirmation → Freeze → Formal Run → Tier 3 Production Candidate → Live",
        "EXPLORATION_ONLY = TRUE",
        "EXPLORATION_RESULT",
        "never be called an\nAlpha",
        "Gold Sample, Independent Review, and Holdout",
        "READY_FOR_TINY_LIVE = TRUE",
        "Route A — Existing Data Exploration",
        "Route B — External Data Acquisition feasibility",
        '"Buy first, research later" is\nprohibited',
    ):
        assert marker in standard
    vendor = (PROCESS / "VENDOR_PROOF_OF_FIT_TEMPLATE.md").read_text()
    for field in (
        "REQUIRED_FIELDS", "COVERAGE", "TIMESTAMPS", "PIT_AND_AVAILABILITY_SEMANTICS",
        "REVISIONS_AND_BACKFILL_POLICY", "COST", "EXPECTED_BLOCKER_REMOVED", "DECISION",
    ):
        assert field in vendor
    register = (PROCESS / "RESEARCH_TIER_CLASSIFICATION_REGISTER_V1.md").read_text()
    for marker in (
        "EXPL-017 | Tier 2 Confirmation Completed | FAIL | Formal confirmation failed.",
        "Candidate B — Funding/OI | Tier 1 Mechanism Valid | Tier 2 Data Admission Blocked",
        "Candidate C — Unlock/Floating Supply | Tier 1 Mechanism Valid | Tier 2 Data Admission Blocked",
        "does not amend, rename, or overwrite",
    ):
        assert marker in register
    template = (PROCESS / "RESEARCH_TIER_RECORD_TEMPLATE.md").read_text()
    assert template.count('"READY_FOR_TINY_LIVE": false') == 3
    assert "TEMPLATE_FAIL_CLOSED_NO_LIVE" in template


def test_valid_json_record_round_trips_through_loader(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "tier-1.json"
    record = tier_1_record()
    path.write_text(json.dumps(record))
    assert tier.load_and_validate_tier_record(path) == record
