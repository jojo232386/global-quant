"""Deterministic EXPL-017 formal IC-horizon containment preflight.

This module uses calendar arithmetic only. It does not load Price V1 or
calculate, print, or serialize any formal performance value.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


FORMAL_RUN_ID = "EXPL-017-FORMAL-002"
HYPOTHESIS_ID = "EXPL-017"
IMPLEMENTATION_ATTEMPT_ID = "EXPL-017-IMPL-014"

DECISION_OFFSET_DAYS = 1
FORWARD_HORIZON_DAYS = 7
CADENCE_DAYS = 7
TRAIN_WARMUP_DECISIONS = 8
EXECUTION_ANCHOR = dt.date(2021, 1, 1)
DATASET_LAST_ALLOWED_BAR = dt.date(2023, 12, 31)
DATASET_BOUNDARY_EXCLUSIVE = dt.date(2024, 1, 1)

TRAIN_REGIME_WARMUP = "TRAIN_REGIME_WARMUP"
FULL_HORIZON_CONTAINED = "FULL_HORIZON_CONTAINED"
HORIZON_CROSSES_SPLIT = "HORIZON_CROSSES_SPLIT"
HORIZON_CROSSES_SPLIT_AND_DATASET = "HORIZON_CROSSES_SPLIT_AND_DATASET"


@dataclass(frozen=True)
class Split:
    name: str
    start: dt.date
    stop: dt.date


SPLITS = (
    Split("train", dt.date(2021, 1, 1), dt.date(2022, 1, 1)),
    Split("oos", dt.date(2022, 1, 1), dt.date(2023, 1, 1)),
    Split("holdout", dt.date(2023, 1, 1), DATASET_BOUNDARY_EXCLUSIVE),
)

EXPECTED_COUNTS = {
    "train": {"schedule": 53, "included": 44, "excluded": 9},
    "oos": {"schedule": 52, "included": 51, "excluded": 1},
    "holdout": {"schedule": 52, "included": 51, "excluded": 1},
}

DEFAULT_ARTIFACT_PATH = Path(__file__).with_name(
    "expl-017-formal-002-horizon-preflight.json"
)


def _split_for(execution: dt.date) -> Split:
    matches = [split for split in SPLITS if split.start <= execution < split.stop]
    if len(matches) != 1:
        raise RuntimeError(f"execution outside unique split: {execution.isoformat()}")
    return matches[0]


def _reason(split: Split, split_index: int, endpoint: dt.date) -> str:
    if split.name == "train" and split_index < TRAIN_WARMUP_DECISIONS:
        return TRAIN_REGIME_WARMUP
    crosses_split = not (split.start <= endpoint < split.stop)
    crosses_dataset = endpoint >= DATASET_BOUNDARY_EXCLUSIVE
    if crosses_split and crosses_dataset:
        return HORIZON_CROSSES_SPLIT_AND_DATASET
    if crosses_split:
        return HORIZON_CROSSES_SPLIT
    if crosses_dataset:
        raise RuntimeError("dataset overflow without split overflow")
    return FULL_HORIZON_CONTAINED


def build_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    split_counts = {split.name: 0 for split in SPLITS}
    execution = EXECUTION_ANCHOR
    one_day = dt.timedelta(days=DECISION_OFFSET_DAYS)
    horizon = dt.timedelta(days=FORWARD_HORIZON_DAYS)
    cadence = dt.timedelta(days=CADENCE_DAYS)

    while execution < DATASET_BOUNDARY_EXCLUSIVE:
        split = _split_for(execution)
        split_index = split_counts[split.name]
        split_counts[split.name] += 1
        decision = execution - one_day
        endpoint = execution + horizon
        reason = _reason(split, split_index, endpoint)
        rows.append(
            {
                "split": split.name,
                "decision": decision.isoformat(),
                "execution": execution.isoformat(),
                "endpoint": endpoint.isoformat(),
                "ic_included": reason == FULL_HORIZON_CONTAINED,
                "reason": reason,
            }
        )
        execution += cadence
    return rows


def _counts(rows: Sequence[dict[str, object]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        split_rows = [row for row in rows if row["split"] == split.name]
        included = sum(bool(row["ic_included"]) for row in split_rows)
        result[split.name] = {
            "schedule": len(split_rows),
            "included": included,
            "excluded": len(split_rows) - included,
        }
    return result


def validate_rows(rows: Sequence[dict[str, object]]) -> dict[str, bool]:
    executions = [dt.date.fromisoformat(str(row["execution"])) for row in rows]
    decisions = [dt.date.fromisoformat(str(row["decision"])) for row in rows]
    endpoints = [dt.date.fromisoformat(str(row["endpoint"])) for row in rows]
    reasons = {
        TRAIN_REGIME_WARMUP,
        FULL_HORIZON_CONTAINED,
        HORIZON_CROSSES_SPLIT,
        HORIZON_CROSSES_SPLIT_AND_DATASET,
    }
    split_indices = {split.name: 0 for split in SPLITS}
    expected_reasons = []
    expected_inclusions = []
    split_matches = []
    for row, execution, endpoint in zip(rows, executions, endpoints):
        split = _split_for(execution)
        split_matches.append(row["split"] == split.name)
        split_index = split_indices[split.name]
        split_indices[split.name] += 1
        expected_reason = _reason(split, split_index, endpoint)
        expected_reasons.append(expected_reason)
        nonwarmup = not (
            split.name == "train" and split_index < TRAIN_WARMUP_DECISIONS
        )
        expected_inclusions.append(
            nonwarmup
            and split.start <= endpoint < split.stop
            and endpoint < DATASET_BOUNDARY_EXCLUSIVE
        )

    checks = {
        "row_count_157": len(rows) == 157,
        "execution_unique": len(set(executions)) == len(executions),
        "execution_strictly_ordered": executions == sorted(executions)
        and all(left < right for left, right in zip(executions, executions[1:])),
        "decision_equals_execution_minus_1d": all(
            decision == execution - dt.timedelta(days=DECISION_OFFSET_DAYS)
            for decision, execution in zip(decisions, executions)
        ),
        "endpoint_equals_execution_plus_7d": all(
            endpoint == execution + dt.timedelta(days=FORWARD_HORIZON_DAYS)
            for endpoint, execution in zip(endpoints, executions)
        ),
        "execution_cadence_7d": all(
            right - left == dt.timedelta(days=CADENCE_DAYS)
            for left, right in zip(executions, executions[1:])
        ),
        "split_matches_execution": all(split_matches),
        "reason_vocabulary_exact": all(row["reason"] in reasons for row in rows),
        "reason_matches_frozen_rule": all(
            row["reason"] == expected
            for row, expected in zip(rows, expected_reasons)
        ),
        "included_iff_nonwarmup_same_split_and_dataset": all(
            bool(row["ic_included"]) == expected
            for row, expected in zip(rows, expected_inclusions)
        ),
        "expected_split_counts": _counts(rows) == EXPECTED_COUNTS,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("horizon preflight invariant failed: " + ", ".join(failed))
    return checks


def artifact() -> dict[str, object]:
    rows = build_rows()
    invariants = validate_rows(rows)
    counts = _counts(rows)
    boundaries = [
        row
        for row in rows
        if row["reason"]
        in {HORIZON_CROSSES_SPLIT, HORIZON_CROSSES_SPLIT_AND_DATASET}
    ]
    included = sum(bool(row["ic_included"]) for row in rows)
    return {
        "artifact_class": "EXPL-017_FORMAL_HORIZON_PREFLIGHT",
        "artifact_version": 1,
        "status": "PASS",
        "formal_run_id": FORMAL_RUN_ID,
        "hypothesis_id": HYPOTHESIS_ID,
        "implementation_attempt_id": IMPLEMENTATION_ATTEMPT_ID,
        "input_spec": {
            "decision_offset_days": DECISION_OFFSET_DAYS,
            "forward_horizon_days": FORWARD_HORIZON_DAYS,
            "anchor": EXECUTION_ANCHOR.isoformat(),
            "cadence_days": CADENCE_DAYS,
            "splits": [
                {
                    "name": split.name,
                    "start_inclusive": split.start.isoformat(),
                    "end_exclusive": split.stop.isoformat(),
                }
                for split in SPLITS
            ],
            "dataset_last_allowed_bar": DATASET_LAST_ALLOWED_BAR.isoformat(),
            "boundary_exclusive": DATASET_BOUNDARY_EXCLUSIVE.isoformat(),
            "train_warmup_decisions": TRAIN_WARMUP_DECISIONS,
        },
        "rows": rows,
        "summary": {
            "total": {
                "schedule": len(rows),
                "included": included,
                "excluded": len(rows) - included,
            },
            "splits": counts,
            "boundaries": boundaries,
        },
        "invariants": invariants,
        "runtime_lifecycle_rule": {
            "scope": "IC_ELIGIBLE_SELECTED_SYMBOLS",
            "required_prices": ["execution_open", "endpoint_open"],
            "terminal_close_substitution": False,
            "final_liquidation_substitution": False,
            "missing_required_price_classification": "DATA_UNAVAILABLE",
        },
        "performance_state": {
            "formal_performance_computed": False,
            "formal_performance_printed": False,
            "formal_performance_serialized": False,
        },
    }


def serialize() -> str:
    return json.dumps(artifact(), indent=2, sort_keys=True) + "\n"


def write(path: Path) -> dict[str, object]:
    payload = artifact()
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args(argv)
    write(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
