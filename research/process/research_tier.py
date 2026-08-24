"""Fail-closed static validation for local GMAQ Research Tier v1 proposals.

The checker validates declared record structure and gates only. It cannot prove
evidence, grant canonical status, replace independent review, or authorize
trading.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Mapping
from typing import Any


class TierRecordError(ValueError):
    """Raised when a record is malformed or violates a tier boundary."""


TIER_1 = "TIER_1_EXPLORATION"
TIER_2 = "TIER_2_CONFIRMATION"
TIER_3 = "TIER_3_PRODUCTION_CANDIDATE"
CONSUMED_PROJECT_HOLDOUT_WINDOWS = frozenset({"GMAQ_2023"})

TIER_1_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "EXPLORATION_ONLY", "OUTPUT_CLASS", "DATA_LIMITATIONS",
    "PIT_LIMITATIONS", "KNOWN_BIAS", "RESULT_STATUS", "NEXT_STAGE", "READY_FOR_TINY_LIVE",
)
TIER_2_GATES = (
    "PIT_CREDIBLE", "LIFECYCLE_CREDIBLE", "GOLD_SAMPLE", "INDEPENDENT_REVIEW", "HOLDOUT",
)
TIER_2_PRIOR_LIST_FIELDS = (
    "PRIOR_TIER_1_DATA_SOURCE_IDS", "PRIOR_TIER_1_TIME_WINDOW_IDS",
    "PRIOR_TIER_1_UNIVERSE_IDS", "PRIOR_TIER_1_DATASET_FAMILY_IDS",
    "PRIOR_TIER_1_SAMPLE_IDS",
)
TIER_2_CONFIRMATION_LIST_FIELDS = (
    "CONFIRMATION_DATA_SOURCE_IDS", "CONFIRMATION_TIME_WINDOW_IDS",
    "CONFIRMATION_UNIVERSE_IDS", "CONFIRMATION_DATASET_FAMILY_IDS",
    "CONFIRMATION_SAMPLE_IDS",
)
TIER_2_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "HYPOTHESIS_ID", "PROGRAM_ID",
    "PROGRAM_FORMAL_HYPOTHESIS_COUNTED", "TIER_1_RECORD_ID",
    "CANDIDATE_REVIEW_RECORD_ID", "DATA_ADMISSION_RECORD_ID", *TIER_2_GATES,
    *TIER_2_PRIOR_LIST_FIELDS, *TIER_2_CONFIRMATION_LIST_FIELDS,
    "INDEPENDENCE_BASIS", "INDEPENDENCE_EVIDENCE", "UNIVERSE_DEFINITION",
    "COVERAGE_LIMITATIONS", "RESULT_STATUS", "BLOCK_REASON", "NEXT_STAGE",
    "READY_FOR_TINY_LIVE",
)
TIER_3_EVIDENCE_GATES = (
    "PIT_COMPLETE", "LIFECYCLE_COMPLETE", "UNIVERSE_COMPLETE", "COSTS_COMPLETE",
    "EXECUTION_ASSUMPTIONS_COMPLETE", "SLIPPAGE_COMPLETE", "CONCENTRATION_COMPLETE",
    "ROBUSTNESS_COMPLETE", "FREEZE_COMPLETE", "FORMAL_RUN_COMPLETE",
)
TIER_3_REVIEW_GATES = (
    "STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE", "RELIABILITY_REVIEW_COMPLETE",
    "RISK_REVIEW_COMPLETE",
)
TIER_3_FIELDS = (
    "RESEARCH_TIER", "RECORD_ID", "HYPOTHESIS_ID", "PROGRAM_ID",
    "PROGRAM_FORMAL_HYPOTHESIS_COUNTED", "TIER_2_RECORD_ID",
    "TIER_2_RESULT_STATUS", "FREEZE_RECORD_ID", "FORMAL_RUN_ID",
    "FORMAL_RUN_RESULT_STATUS", *TIER_3_EVIDENCE_GATES, *TIER_3_REVIEW_GATES,
    "RESULT_STATUS", "NEXT_STAGE", "READY_FOR_TINY_LIVE",
)
PROGRAM_HISTORY_FIELDS = (
    "PROGRAM_ID", "MECHANISM_FAMILY", "DATASET_FAMILY", "FORMAL_HYPOTHESIS_IDS",
    "FORMAL_HYPOTHESES_TESTED", "PASS_COUNT", "FAIL_COUNT", "HOLDOUT_WINDOWS_CONSUMED",
)
INDEPENDENCE_BASES = {
    "TEMPORAL_NEW_WINDOW": ("temporal",),
    "INDEPENDENT_SOURCE": ("source",),
    "INDEPENDENT_SAMPLE": ("sample",),
    "TEMPORAL_AND_INDEPENDENT_SOURCE": ("temporal", "source"),
    "TEMPORAL_AND_INDEPENDENT_SAMPLE": ("temporal", "sample"),
    "INSUFFICIENT": (),
}


def _require_exact_keys(record: Mapping[str, Any], expected: tuple[str, ...]) -> None:
    actual = tuple(record)
    if set(actual) != set(expected) or len(actual) != len(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise TierRecordError(f"record keys differ; missing={missing}; extra={extra}")


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


def _require_identifier_list(record: Mapping[str, Any], key: str) -> list[str]:
    value = record[key]
    if not isinstance(value, list) or not value:
        raise TierRecordError(f"{key} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TierRecordError(f"{key} must contain non-empty string identifiers")
    invalid = {"UNVERIFIED", "UNKNOWN", "TBD", "UNASSIGNED", "NONE"}
    if any(item.strip().upper() in invalid for item in value):
        raise TierRecordError(f"{key} must identify existing information")
    if len(set(value)) != len(value):
        raise TierRecordError(f"{key} must not contain duplicate identifiers")
    return value


def _require_nonnegative_integer(record: Mapping[str, Any], key: str) -> int:
    value = record[key]
    if type(value) is not int or value < 0:
        raise TierRecordError(f"{key} must be a nonnegative integer")
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


def _validate_independence(record: Mapping[str, Any]) -> bool:
    prior = {key: _require_identifier_list(record, key) for key in TIER_2_PRIOR_LIST_FIELDS}
    current = {key: _require_identifier_list(record, key) for key in TIER_2_CONFIRMATION_LIST_FIELDS}
    basis = _require_nonempty_string(record, "INDEPENDENCE_BASIS")
    if basis not in INDEPENDENCE_BASES:
        raise TierRecordError("INDEPENDENCE_BASIS is invalid")
    _require_documented_text(record, "INDEPENDENCE_EVIDENCE")
    temporal = (
        not (set(current["CONFIRMATION_TIME_WINDOW_IDS"]) & set(prior["PRIOR_TIER_1_TIME_WINDOW_IDS"]))
        and not (set(current["CONFIRMATION_TIME_WINDOW_IDS"]) & CONSUMED_PROJECT_HOLDOUT_WINDOWS)
    )
    source = not (set(current["CONFIRMATION_DATA_SOURCE_IDS"]) & set(prior["PRIOR_TIER_1_DATA_SOURCE_IDS"]))
    sample = not (set(current["CONFIRMATION_SAMPLE_IDS"]) & set(prior["PRIOR_TIER_1_SAMPLE_IDS"]))
    available = {"temporal": temporal, "source": source, "sample": sample}
    missing = [name for name in INDEPENDENCE_BASES[basis] if not available[name]]
    if missing:
        raise TierRecordError(
            f"INDEPENDENCE_BASIS cannot claim {', '.join(missing)} independence; "
            "overlapping or consumed windows, sources, or samples are not independent"
        )
    return basis != "INSUFFICIENT" and bool(INDEPENDENCE_BASES[basis])


def _validate_tier_2(record: Mapping[str, Any]) -> None:
    _require_exact_keys(record, TIER_2_FIELDS)
    for key in (
        "RECORD_ID", "HYPOTHESIS_ID", "PROGRAM_ID", "TIER_1_RECORD_ID",
        "CANDIDATE_REVIEW_RECORD_ID", "DATA_ADMISSION_RECORD_ID",
    ):
        _require_identifier(record, key)
    counted = _require_boolean(record, "PROGRAM_FORMAL_HYPOTHESIS_COUNTED")
    _require_all_true(record, TIER_2_GATES)
    independence_proven = _validate_independence(record)
    for key in ("UNIVERSE_DEFINITION", "COVERAGE_LIMITATIONS"):
        _require_documented_text(record, key)
    status = record["RESULT_STATUS"]
    next_stage = record["NEXT_STAGE"]
    block_reason = record["BLOCK_REASON"]
    if status in {"CONFIRMED_CANDIDATE", "FAIL"}:
        if not independence_proven:
            raise TierRecordError(
                "CONFIRMED_CANDIDATE and formal FAIL require independent confirmation; "
                "use CONFIRMATION_BLOCKED when evidence is insufficient"
            )
        if block_reason != "NONE":
            raise TierRecordError("completed Tier 2 outcomes require BLOCK_REASON = NONE")
        if (status, next_stage) not in {("CONFIRMED_CANDIDATE", "FREEZE"), ("FAIL", "STOP")}:
            raise TierRecordError("Tier 2 must be CONFIRMED_CANDIDATE → FREEZE or FAIL → STOP")
        if status == "FAIL" and not counted:
            raise TierRecordError("formal Tier 2 FAIL must be counted in program history")
    elif status == "CONFIRMATION_BLOCKED":
        if next_stage != "STOP" or block_reason != "ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED":
            raise TierRecordError(
                "CONFIRMATION_BLOCKED requires NEXT_STAGE = STOP and "
                "BLOCK_REASON = ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED"
            )
        if counted:
            raise TierRecordError("pre-formal CONFIRMATION_BLOCKED must not increment formal history")
    else:
        raise TierRecordError("Tier 2 RESULT_STATUS is invalid")
    if _require_boolean(record, "READY_FOR_TINY_LIVE"):
        raise TierRecordError("Tier 2 can never be READY_FOR_TINY_LIVE")


def _validate_tier_3(record: Mapping[str, Any]) -> None:
    _require_exact_keys(record, TIER_3_FIELDS)
    for key in (
        "RECORD_ID", "HYPOTHESIS_ID", "PROGRAM_ID", "TIER_2_RECORD_ID",
        "FREEZE_RECORD_ID", "FORMAL_RUN_ID",
    ):
        _require_identifier(record, key)
    if not _require_boolean(record, "PROGRAM_FORMAL_HYPOTHESIS_COUNTED"):
        raise TierRecordError("Tier 3 formal hypothesis must be counted in program history")
    if record["TIER_2_RESULT_STATUS"] != "CONFIRMED_CANDIDATE":
        raise TierRecordError("Tier 3 requires a Tier 2 CONFIRMED_CANDIDATE result")
    if record["FORMAL_RUN_RESULT_STATUS"] != "PASS":
        raise TierRecordError("Tier 3 requires PASS from the frozen formal evaluation")
    _require_all_true(record, TIER_3_EVIDENCE_GATES)
    for key in TIER_3_REVIEW_GATES:
        _require_boolean(record, key)
    if record["RESULT_STATUS"] not in {"PRODUCTION_CANDIDATE", "FAIL"}:
        raise TierRecordError("Tier 3 RESULT_STATUS is invalid")
    if record["NEXT_STAGE"] not in {"STRATEGY", "DRY_RUN", "RELIABILITY_REVIEW", "RISK_REVIEW", "LIVE", "STOP"}:
        raise TierRecordError("Tier 3 NEXT_STAGE is invalid")
    if record["RESULT_STATUS"] == "FAIL" and record["NEXT_STAGE"] != "STOP":
        raise TierRecordError("Tier 3 FAIL must set NEXT_STAGE = STOP")
    prerequisites = {
        "DRY_RUN": ("STRATEGY_REVIEW_COMPLETE",),
        "RELIABILITY_REVIEW": ("STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE"),
        "RISK_REVIEW": ("STRATEGY_REVIEW_COMPLETE", "DRY_RUN_COMPLETE", "RELIABILITY_REVIEW_COMPLETE"),
    }
    if any(not record[key] for key in prerequisites.get(record["NEXT_STAGE"], ())):
        raise TierRecordError(f"{record['NEXT_STAGE']} requires prior Tier 3 reviews")
    ready = _require_boolean(record, "READY_FOR_TINY_LIVE")
    if ready and (record["RESULT_STATUS"] != "PRODUCTION_CANDIDATE" or not all(record[key] for key in TIER_3_REVIEW_GATES)):
        raise TierRecordError("tiny-live readiness requires Tier 3 and all separate reviews")
    if record["NEXT_STAGE"] == "LIVE" and not ready:
        raise TierRecordError("LIVE requires READY_FOR_TINY_LIVE = true")


def validate_tier_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one exact Tier v1 record and return an ordinary copy on success."""
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


def validate_program_history(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a thin formal-history record without making statistical claims."""
    if not isinstance(record, Mapping):
        raise TierRecordError("program history must be a JSON object")
    _require_exact_keys(record, PROGRAM_HISTORY_FIELDS)
    for key in ("PROGRAM_ID", "MECHANISM_FAMILY", "DATASET_FAMILY"):
        _require_identifier(record, key)
    hypotheses = _require_identifier_list(record, "FORMAL_HYPOTHESIS_IDS")
    tested = _require_nonnegative_integer(record, "FORMAL_HYPOTHESES_TESTED")
    passed = _require_nonnegative_integer(record, "PASS_COUNT")
    failed = _require_nonnegative_integer(record, "FAIL_COUNT")
    _require_identifier_list(record, "HOLDOUT_WINDOWS_CONSUMED")
    if tested != len(hypotheses):
        raise TierRecordError("FORMAL_HYPOTHESES_TESTED must equal unique FORMAL_HYPOTHESIS_IDS")
    if passed + failed != tested:
        raise TierRecordError("PASS_COUNT + FAIL_COUNT must equal FORMAL_HYPOTHESES_TESTED")
    return dict(record)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise TierRecordError(f"duplicate record key: {key}")
        output[key] = value
    return output


def _load_json(path: pathlib.Path, description: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_keys)
    except OSError as exc:
        raise TierRecordError(f"cannot read {description}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TierRecordError(f"invalid {description} JSON: {exc.msg}") from exc


def load_and_validate_tier_record(path: pathlib.Path) -> dict[str, Any]:
    return validate_tier_record(_load_json(path, "tier record"))


def load_and_validate_program_history(path: pathlib.Path) -> dict[str, Any]:
    return validate_program_history(_load_json(path, "program history"))


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", nargs="?", type=pathlib.Path)
    parser.add_argument("--program-history", type=pathlib.Path)
    parsed = parser.parse_args(arguments)
    if (parsed.record is None) == (parsed.program_history is None):
        parser.error("provide exactly one tier record or --program-history PATH")
    try:
        if parsed.program_history is not None:
            load_and_validate_program_history(parsed.program_history)
        else:
            load_and_validate_tier_record(parsed.record)
    except TierRecordError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
