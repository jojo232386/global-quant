"""Fail-closed static validation for GMAQ Research Tier v1 records.

The checker only validates declared record structure and gates. It cannot prove
the underlying evidence and is not an admission, review, or trading authority.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Mapping
from typing import Any


class TierRecordError(ValueError):
    """Raised when a tier record is malformed or violates a tier boundary."""


TIER_1 = "TIER_1_EXPLORATION"
TIER_2 = "TIER_2_CONFIRMATION"
TIER_3 = "TIER_3_PRODUCTION_CANDIDATE"

TIER_1_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "EXPLORATION_ONLY", "OUTPUT_CLASS", "DATA_LIMITATIONS",
    "PIT_LIMITATIONS", "KNOWN_BIAS", "RESULT_STATUS", "NEXT_STAGE",
    "READY_FOR_TINY_LIVE",
)
TIER_2_GATES = (
    "PIT_CREDIBLE", "LIFECYCLE_CREDIBLE", "GOLD_SAMPLE",
    "INDEPENDENT_REVIEW", "HOLDOUT",
)
TIER_2_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "HYPOTHESIS_ID", "TIER_1_RECORD_ID",
    "CANDIDATE_REVIEW_RECORD_ID", "DATA_ADMISSION_RECORD_ID", *TIER_2_GATES,
    "UNIVERSE_DEFINITION", "COVERAGE_LIMITATIONS", "RESULT_STATUS", "NEXT_STAGE",
    "READY_FOR_TINY_LIVE",
)
TIER_3_EVIDENCE_GATES = (
    "PIT_COMPLETE", "LIFECYCLE_COMPLETE", "UNIVERSE_COMPLETE", "COSTS_COMPLETE",
    "EXECUTION_ASSUMPTIONS_COMPLETE", "SLIPPAGE_COMPLETE",
    "CONCENTRATION_COMPLETE", "ROBUSTNESS_COMPLETE", "FREEZE_COMPLETE",
    "FORMAL_RUN_COMPLETE",
)
TIER_3_REVIEW_GATES = (
    "STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE",
    "RELIABILITY_REVIEW_COMPLETE", "RISK_REVIEW_COMPLETE",
)
TIER_3_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "HYPOTHESIS_ID", "TIER_2_RECORD_ID",
    "TIER_2_RESULT_STATUS", "FREEZE_RECORD_ID", "FORMAL_RUN_ID",
    "FORMAL_RUN_RESULT_STATUS", *TIER_3_EVIDENCE_GATES,
    *TIER_3_REVIEW_GATES, "RESULT_STATUS", "NEXT_STAGE", "READY_FOR_TINY_LIVE",
)


def _require_exact_keys(record: Mapping[str, Any], expected: tuple[str, ...]) -> None:
    actual = tuple(record)
    if set(actual) != set(expected) or len(actual) != len(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise TierRecordError(f"tier record keys differ; missing={missing}; extra={extra}")


def _require_boolean(record: Mapping[str, Any], key: str) -> bool:
    value = record[key]
    if type(value) is not bool:
        raise TierRecordError(f"{key} must be a boolean")
    return value


def _require_nonempty_string(record: Mapping[str, Any], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise TierRecordError(f"{key} must be a non-empty string")
    return value


def _require_documented_text(record: Mapping[str, Any], key: str) -> str:
    value = _require_nonempty_string(record, key)
    if value.strip().upper() in {"UNVERIFIED", "UNKNOWN", "TBD", "UNASSIGNED"}:
        raise TierRecordError(f"{key} must document the actual limitation or definition")
    return value


def _require_identifier(record: Mapping[str, Any], key: str) -> str:
    value = _require_nonempty_string(record, key)
    if value.strip().upper() in {"UNVERIFIED", "UNKNOWN", "TBD", "UNASSIGNED", "NONE"}:
        raise TierRecordError(f"{key} must identify an existing record")
    return value


def _require_all_true(record: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if not _require_boolean(record, key):
            raise TierRecordError(f"{key} is not complete")


def _validate_tier_1(record: Mapping[str, Any]) -> None:
    _require_exact_keys(record, TIER_1_FIELDS)
    _require_identifier(record, "RECORD_ID")
    if _require_boolean(record, "EXPLORATION_ONLY") is not True:
        raise TierRecordError("Tier 1 requires EXPLORATION_ONLY = true")
    if record["OUTPUT_CLASS"] != "EXPLORATION_RESULT":
        raise TierRecordError("Tier 1 output must be EXPLORATION_RESULT, never Alpha")
    for key in ("DATA_LIMITATIONS", "PIT_LIMITATIONS", "KNOWN_BIAS"):
        _require_documented_text(record, key)
    if record["RESULT_STATUS"] not in {"MECHANISM_VALID", "FAIL"}:
        raise TierRecordError("Tier 1 RESULT_STATUS is invalid")
    if record["NEXT_STAGE"] not in {"CANDIDATE_REVIEW", "STOP"}:
        raise TierRecordError("Tier 1 cannot jump to Strategy, Dry-run, Live, or Tier 3")
    if record["RESULT_STATUS"] == "FAIL" and record["NEXT_STAGE"] != "STOP":
        raise TierRecordError("Tier 1 FAIL must set NEXT_STAGE = STOP")
    if _require_boolean(record, "READY_FOR_TINY_LIVE"):
        raise TierRecordError("Tier 1 can never be READY_FOR_TINY_LIVE")


def _validate_tier_2(record: Mapping[str, Any]) -> None:
    _require_exact_keys(record, TIER_2_FIELDS)
    for key in (
        "RECORD_ID", "HYPOTHESIS_ID", "TIER_1_RECORD_ID",
        "CANDIDATE_REVIEW_RECORD_ID", "DATA_ADMISSION_RECORD_ID",
    ):
        _require_identifier(record, key)
    _require_all_true(record, TIER_2_GATES)
    for key in ("UNIVERSE_DEFINITION", "COVERAGE_LIMITATIONS"):
        _require_documented_text(record, key)
    status = record["RESULT_STATUS"]
    next_stage = record["NEXT_STAGE"]
    if status == "CONFIRMED_CANDIDATE" and next_stage == "FREEZE":
        pass
    elif status == "FAIL" and next_stage == "STOP":
        pass
    else:
        raise TierRecordError("Tier 2 must be CONFIRMED_CANDIDATE → FREEZE or FAIL → STOP")
    if _require_boolean(record, "READY_FOR_TINY_LIVE"):
        raise TierRecordError("Tier 2 can never be READY_FOR_TINY_LIVE")


def _validate_tier_3(record: Mapping[str, Any]) -> None:
    _require_exact_keys(record, TIER_3_FIELDS)
    for key in (
        "RECORD_ID", "HYPOTHESIS_ID", "TIER_2_RECORD_ID", "FREEZE_RECORD_ID",
        "FORMAL_RUN_ID",
    ):
        _require_identifier(record, key)
    if record["TIER_2_RESULT_STATUS"] != "CONFIRMED_CANDIDATE":
        raise TierRecordError("Tier 3 requires a Tier 2 CONFIRMED_CANDIDATE result")
    if record["FORMAL_RUN_RESULT_STATUS"] != "PASS":
        raise TierRecordError("Tier 3 requires PASS from the frozen formal evaluation")
    _require_all_true(record, TIER_3_EVIDENCE_GATES)
    for key in TIER_3_REVIEW_GATES:
        _require_boolean(record, key)
    if record["RESULT_STATUS"] not in {"PRODUCTION_CANDIDATE", "FAIL"}:
        raise TierRecordError("Tier 3 RESULT_STATUS is invalid")
    if record["NEXT_STAGE"] not in {
        "STRATEGY", "DRY_RUN", "RELIABILITY_REVIEW", "RISK_REVIEW", "LIVE", "STOP",
    }:
        raise TierRecordError("Tier 3 NEXT_STAGE is invalid")
    if record["RESULT_STATUS"] == "FAIL" and record["NEXT_STAGE"] != "STOP":
        raise TierRecordError("Tier 3 FAIL must set NEXT_STAGE = STOP")
    stage_prerequisites = {
        "DRY_RUN": ("STRATEGY_REVIEW_COMPLETE",),
        "RELIABILITY_REVIEW": ("STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE"),
        "RISK_REVIEW": (
            "STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE", "RELIABILITY_REVIEW_COMPLETE",
        ),
    }
    required_reviews = stage_prerequisites.get(record["NEXT_STAGE"], ())
    if any(not record[key] for key in required_reviews):
        raise TierRecordError(f"{record['NEXT_STAGE']} requires prior Tier 3 reviews")
    ready = _require_boolean(record, "READY_FOR_TINY_LIVE")
    all_reviews_complete = all(record[key] for key in TIER_3_REVIEW_GATES)
    if ready and (record["RESULT_STATUS"] != "PRODUCTION_CANDIDATE" or not all_reviews_complete):
        raise TierRecordError("tiny-live readiness requires Tier 3 and all separate reviews")
    if record["NEXT_STAGE"] == "LIVE" and not ready:
        raise TierRecordError("LIVE requires READY_FOR_TINY_LIVE = true")


def validate_tier_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one exact tier record and return an ordinary copy on success."""
    if not isinstance(record, Mapping):
        raise TierRecordError("tier record must be a JSON object")
    tier = record.get("RESEARCH_TIER")
    if tier == TIER_1:
        _validate_tier_1(record)
    elif tier == TIER_2:
        _validate_tier_2(record)
    elif tier == TIER_3:
        _validate_tier_3(record)
    else:
        raise TierRecordError("RESEARCH_TIER is invalid")
    return dict(record)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise TierRecordError(f"duplicate tier record key: {key}")
        output[key] = value
    return output


def load_and_validate_tier_record(path: pathlib.Path) -> dict[str, Any]:
    """Load an ordinary JSON record, rejecting duplicate keys before validation."""
    try:
        payload = json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise TierRecordError(f"cannot read tier record: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TierRecordError(f"invalid tier record JSON: {exc.msg}") from exc
    return validate_tier_record(payload)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=pathlib.Path)
    parsed = parser.parse_args(arguments)
    try:
        load_and_validate_tier_record(parsed.record)
    except TierRecordError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
