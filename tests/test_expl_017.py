"""Independent correctness contracts for the pre-freeze EXPL-017 runner."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "research/exploration/expl_017.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("expl_017", MODULE)
expl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = expl
spec.loader.exec_module(expl)


def cases() -> list[dict]:
    return json.loads(expl.GOLD_PATH.read_text(encoding="utf-8"))["cases"]


def test_gold_sample_replays_every_committed_case_field_by_field():
    results = expl.replay_gold_sample()
    assert len(results) == 3
    assert [item["portfolio_pnl"]["net"] for item in results] == pytest.approx(
        [0.0485, 0.072, 0.14775]
    )


def test_execution_day_close_cannot_change_high_volatility_decision():
    case = copy.deepcopy(cases()[1])
    first = expl.gold_case_result(case)
    case["input_bars"]["A"]["execution_close_not_available_at_decision"] = 0.0001
    case["input_bars"]["E"]["execution_close_not_available_at_decision"] = 999999.0
    second = expl.gold_case_result(case)
    assert second == first


def test_terminal_contract_charges_final_exit_once_without_forward_fill():
    result = expl.gold_case_result(cases()[2])
    assert result["turnover"] == pytest.approx(1.5)
    assert result["cost"] == pytest.approx(0.00225)
    assert result["next_period_return"]["E"] == pytest.approx(-0.2)
    assert result["portfolio_pnl"]["net"] == pytest.approx(0.14775)


def test_rank_mapping_reverses_only_with_broad_high_volatility_state():
    scores = {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25, "E": 0.0}
    assert expl.target_positions(scores, 0.2, "calm") == {"A": 0.5, "E": -0.5}
    assert expl.target_positions(scores, 0.2, "high") == {"E": 0.5, "A": -0.5}


def test_turnover_and_cost_count_both_legs_and_terminal_exit():
    assert expl.turnover({"A": 0.5, "E": -0.5}, {"A": -0.5, "E": 0.5}) == pytest.approx(2.0)
    assert expl.turnover({}, {"A": 0.5, "E": -0.5}) + 0.5 == pytest.approx(1.5)


def test_split_containment_is_closed_and_has_no_post_2023_path():
    assert expl.segment_for_execution(expl.dt.date(2021, 1, 1)) == "train"
    assert expl.segment_for_execution(expl.dt.date(2022, 1, 1)) == "oos"
    assert expl.segment_for_execution(expl.dt.date(2023, 1, 1)) == "final_holdout"
    with pytest.raises(expl.Expl017Error, match="outside formal containment"):
        expl.segment_for_execution(expl.dt.date(2024, 1, 1))


def test_formal_execution_is_fail_closed_before_a_separate_freeze():
    with pytest.raises(expl.FormalRunLocked, match="FORMAL_RUN_LOCKED"):
        expl.formal_run()


def test_wrong_dataset_identity_is_data_unavailable_before_any_formal_path(monkeypatch, tmp_path):
    monkeypatch.setattr(expl, "load_dataset", lambda _root: (_ for _ in ()).throw(
        expl.PriceAlphaError("bad snapshot")
    ))
    with pytest.raises(expl.Expl017Error, match="DATA_UNAVAILABLE"):
        expl.validate_dataset(tmp_path)


def test_parameter_surface_is_not_exposed_to_callers():
    assert expl.momentum_scores.__defaults__ is None
    assert expl.target_positions.__defaults__ is None
    assert expl.BASE_COST == pytest.approx(0.0015)
