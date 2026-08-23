from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from research.exploration import expl_017_horizon_preflight as preflight


ROOT = Path(__file__).resolve().parents[1]
COMMITTED = (
    ROOT / "research" / "exploration" / "expl-017-formal-002-horizon-preflight.json"
)


def _by_decision(payload):
    return {row["decision"]: row for row in payload["rows"]}


def test_identity_and_complete_input_spec():
    payload = preflight.artifact()
    assert payload["formal_run_id"] == "EXPL-017-FORMAL-002"
    assert payload["hypothesis_id"] == "EXPL-017"
    assert payload["implementation_attempt_id"] == "EXPL-017-IMPL-014"
    assert payload["input_spec"] == {
        "decision_offset_days": 1,
        "forward_horizon_days": 7,
        "anchor": "2021-01-01",
        "cadence_days": 7,
        "splits": [
            {
                "name": "train",
                "start_inclusive": "2021-01-01",
                "end_exclusive": "2022-01-01",
            },
            {
                "name": "oos",
                "start_inclusive": "2022-01-01",
                "end_exclusive": "2023-01-01",
            },
            {
                "name": "holdout",
                "start_inclusive": "2023-01-01",
                "end_exclusive": "2024-01-01",
            },
        ],
        "dataset_last_allowed_bar": "2023-12-31",
        "boundary_exclusive": "2024-01-01",
        "train_warmup_decisions": 8,
    }


def test_rows_are_unique_ordered_and_exactly_classified():
    payload = preflight.artifact()
    rows = payload["rows"]
    assert len(rows) == 157
    assert all(
        set(row) == {
            "split",
            "decision",
            "execution",
            "endpoint",
            "ic_included",
            "reason",
        }
        for row in rows
    )
    executions = [dt.date.fromisoformat(row["execution"]) for row in rows]
    assert executions == sorted(set(executions))
    assert all(
        right - left == dt.timedelta(days=7)
        for left, right in zip(executions, executions[1:])
    )
    split_stops = {
        item["name"]: dt.date.fromisoformat(item["end_exclusive"])
        for item in payload["input_spec"]["splits"]
    }
    for index, row in enumerate(rows):
        decision = dt.date.fromisoformat(row["decision"])
        execution = dt.date.fromisoformat(row["execution"])
        endpoint = dt.date.fromisoformat(row["endpoint"])
        warmup = row["split"] == "train" and index < 8
        expected_inclusion = (
            not warmup
            and endpoint < split_stops[row["split"]]
            and endpoint < dt.date(2024, 1, 1)
        )
        assert decision == execution - dt.timedelta(days=1)
        assert endpoint == execution + dt.timedelta(days=7)
        assert row["ic_included"] is expected_inclusion
    assert all(payload["invariants"].values())
    assert payload["summary"]["total"] == {
        "schedule": 157,
        "included": 146,
        "excluded": 11,
    }
    assert payload["summary"]["splits"] == preflight.EXPECTED_COUNTS


def test_every_split_boundary_has_previous_legal_and_boundary_illegal_row():
    rows = _by_decision(preflight.artifact())
    cases = (
        ("2021-12-23", "2021-12-30", preflight.HORIZON_CROSSES_SPLIT),
        ("2022-12-22", "2022-12-29", preflight.HORIZON_CROSSES_SPLIT),
        (
            "2023-12-21",
            "2023-12-28",
            preflight.HORIZON_CROSSES_SPLIT_AND_DATASET,
        ),
    )
    for legal, boundary, reason in cases:
        assert rows[legal]["ic_included"] is True
        assert rows[legal]["reason"] == preflight.FULL_HORIZON_CONTAINED
        assert rows[boundary]["ic_included"] is False
        assert rows[boundary]["reason"] == reason
    assert rows["2023-12-28"]["endpoint"] == "2024-01-05"
    boundaries = preflight.artifact()["summary"]["boundaries"]
    assert [row["decision"] for row in boundaries] == [
        "2021-12-30",
        "2022-12-29",
        "2023-12-28",
    ]


def test_first_eight_train_rows_are_warmup_and_ninth_is_eligible():
    train = [row for row in preflight.build_rows() if row["split"] == "train"]
    assert all(
        row["reason"] == preflight.TRAIN_REGIME_WARMUP
        and row["ic_included"] is False
        for row in train[:8]
    )
    assert train[8]["reason"] == preflight.FULL_HORIZON_CONTAINED
    assert train[8]["ic_included"] is True


def test_committed_json_and_cli_output_equal_generated_artifact(tmp_path):
    expected = preflight.artifact()
    assert json.loads(COMMITTED.read_text(encoding="utf-8")) == expected
    output = tmp_path / "preflight.json"
    assert preflight.main(["--output", str(output)]) == 0
    assert output.read_bytes() == COMMITTED.read_bytes()
    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_validator_rejects_schedule_or_inclusion_tampering():
    rows = preflight.build_rows()
    duplicate = copy.deepcopy(rows)
    duplicate[1]["execution"] = duplicate[0]["execution"]
    with pytest.raises(RuntimeError, match="invariant failed"):
        preflight.validate_rows(duplicate)
    wrong_inclusion = copy.deepcopy(rows)
    wrong_inclusion[-1]["ic_included"] = True
    with pytest.raises(RuntimeError, match="invariant failed"):
        preflight.validate_rows(wrong_inclusion)
    wrong_reason = copy.deepcopy(rows)
    wrong_reason[8]["reason"] = preflight.HORIZON_CROSSES_SPLIT
    with pytest.raises(RuntimeError, match="invariant failed"):
        preflight.validate_rows(wrong_reason)


def test_runtime_lifecycle_rule_is_fail_closed_without_substitutions():
    rule = preflight.artifact()["runtime_lifecycle_rule"]
    assert rule == {
        "scope": "IC_ELIGIBLE_SELECTED_SYMBOLS",
        "required_prices": ["execution_open", "endpoint_open"],
        "terminal_close_substitution": False,
        "final_liquidation_substitution": False,
        "missing_required_price_classification": "DATA_UNAVAILABLE",
    }


def test_artifact_recursively_contains_no_performance_results():
    payload = preflight.artifact()
    assert payload["performance_state"] == {
        "formal_performance_computed": False,
        "formal_performance_printed": False,
        "formal_performance_serialized": False,
    }
    banned_fragments = {
        "total_return",
        "sharpe",
        "max_drawdown",
        "mean_ic",
        "ic_t_stat",
        "pnl",
        "performance_result",
    }

    def walk(value, path=()):
        if path and path[0] == "performance_state":
            assert value is False or isinstance(value, dict)
            if isinstance(value, dict):
                for child_key, child in value.items():
                    walk(child, path + (child_key,))
            return
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                assert not any(fragment in lowered for fragment in banned_fragments)
                walk(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + (str(index),))
        elif isinstance(value, str):
            lowered = value.lower()
            assert not any(fragment in lowered for fragment in banned_fragments)

    walk(payload)
