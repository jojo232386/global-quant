"""Independent correctness contracts for the pre-freeze EXPL-017 runner."""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import pathlib
import statistics
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


def day(value: dt.date) -> int:
    return int(dt.datetime.combine(value, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)


def injected_price_dataset(
    *, terminal_symbol: str | None = None, terminal_date: dt.date | None = None
):
    """21-symbol PIT toy with 90 completed bars and no performance outputs."""
    first = dt.date(2020, 10, 1)
    last = dt.date(2022, 1, 15)
    all_days = [first + dt.timedelta(days=index) for index in range((last - first).days + 1)]
    bars, last_timestamp = {}, {}
    symbols = [f"S{index:02d}" for index in range(21)]
    for number, symbol in enumerate(symbols):
        terminal = (terminal_date or dt.date(2021, 3, 1)) if symbol == terminal_symbol else last
        points = {}
        for index, current in enumerate(all_days):
            if current > terminal:
                break
            # Distinct completed-close paths create deterministic rank/vol
            # inputs without any future bar being needed by the engine.
            close = 100.0 + number * 0.25 + index * (0.03 + number * 0.001) + ((index % 5) - 2) * 0.02
            points[day(current)] = expl.Bar(close, close, 1_000.0 - number)
        bars[symbol] = points
        last_timestamp[symbol] = max(points)
    pit = {}
    current_month = dt.date(2020, 10, 1)
    while current_month <= dt.date(2022, 1, 1):
        pit[day(current_month)] = tuple(symbols)
        current_month = (current_month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return expl.PriceDataset(
        bars=bars, last_timestamp=last_timestamp, pit=pit,
        artifact_path=pathlib.Path("/tmp/expl017-toy"), manifest_sha256="toy", pit_sha256="toy",
        labels=("archive-extended", "survivor-biased", "exploration-only"),
    )


def review_schedule() -> list[int]:
    train = [day(dt.date(2021, 1, 1) + dt.timedelta(days=7 * index)) for index in range(10)]
    return train + [day(dt.date(2022, 1, 7))]


def test_gold_sample_replays_every_committed_case_field_by_field():
    results = expl.replay_gold_sample()
    assert len(results) == 3
    assert [item["portfolio_pnl"]["net"] for item in results] == pytest.approx(
        [0.0485, 0.072, 0.14775]
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.__setitem__("signal", "continuation: long E, short A"), "signal"),
        (lambda item: item.__setitem__("execution_timestamp", "2021-01-07T00:00:00Z-open"), "execution_timestamp"),
        (lambda item: item["parameters"].__setitem__("volatility_window", 999), "volatility window"),
    ],
)
def test_gold_replay_rejects_mutated_derived_fields(mutate, match, tmp_path):
    case = copy.deepcopy(cases()[0])
    mutate(case)
    path = tmp_path / "mutated-gold.json"
    path.write_text(json.dumps({"artifact_class": "PRE_IMPLEMENTATION_HAND_CALCULATED_GOLD_SAMPLE", "experiment_id": "EXPL-017", "cases": [case, cases()[1], cases()[2]]}), encoding="utf-8")
    with pytest.raises(expl.Expl017Error, match=match):
        expl.replay_gold_sample(path)


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


def test_injected_engine_uses_pit_90_bar_selection_and_fixed_weekly_timing():
    data = injected_price_dataset()
    execution = day(dt.date(2021, 1, 1))
    selected = expl._eligible_top_n(data, execution, 20)
    assert selected == tuple(f"S{index:02d}" for index in range(20))
    assert expl.weekly_schedule()[:3] == [execution, execution + 7 * expl.DAY_MS, execution + 14 * expl.DAY_MS]
    # Effective January membership, not a later membership, controls selection.
    data.pit[day(dt.date(2021, 1, 1))] = tuple(f"S{index:02d}" for index in range(1, 21))
    assert expl._eligible_top_n(data, execution, 20)[0] == "S01"


def test_injected_engine_is_lookahead_free_and_warms_then_uses_train_state():
    data = injected_price_dataset()
    schedule = review_schedule()
    first = expl.build_correctness_plan(data, config=expl.EngineConfig(20, 21), executions=schedule)
    assert all(event.state == "warmup" and event.target == {} for event in first[:8])
    assert first[8].threshold is not None and first[8].target
    assert first[-1].segment == "oos"
    assert first[-1].threshold == pytest.approx(
        statistics.median(event.volatility_statistic for event in first[:-1])
    )
    # A t+1 close is unavailable at t and must not alter the decision result.
    execution = schedule[8]
    data.bars["S00"][execution] = expl.Bar(data.bars["S00"][execution].open, 1e9, data.bars["S00"][execution].quote_volume)
    second = expl.build_correctness_plan(data, config=expl.EngineConfig(20, 21), executions=schedule)
    assert second[8].scores == first[8].scores
    assert second[8].volatility_statistic == pytest.approx(first[8].volatility_statistic)
    assert second[8].target == first[8].target
    # OOS state statistics may move with OOS data, but the state boundary is
    # the completed train median and cannot be contaminated by that change.
    oos_decision = schedule[-1] - expl.DAY_MS
    data.bars["S00"][oos_decision] = expl.Bar(data.bars["S00"][oos_decision].open, 1e9, data.bars["S00"][oos_decision].quote_volume)
    third = expl.build_correctness_plan(data, config=expl.EngineConfig(20, 21), executions=schedule)
    assert third[-1].threshold == pytest.approx(first[-1].threshold)


def test_injected_engine_carries_incumbent_and_exits_terminal_once_without_forward_fill():
    data = injected_price_dataset(terminal_symbol="S00")
    schedule = review_schedule()[:-1]
    plan = expl.build_correctness_plan(data, config=expl.EngineConfig(20, 21), executions=schedule)
    terminal_event = next(event for event in plan if "S00" in event.terminal_exits)
    assert terminal_event.terminal_exit_turnover > 0
    assert terminal_event.turnover == pytest.approx(
        terminal_event.trade_turnover + terminal_event.terminal_exit_turnover
    )
    assert terminal_event.cost == pytest.approx(terminal_event.turnover * expl.BASE_COST)
    assert "S00" not in plan[plan.index(terminal_event) + 1].target


def test_injected_engine_marks_asymmetric_drift_before_unchanged_target_rebalance():
    schedule = review_schedule()
    baseline = expl.build_correctness_plan(
        injected_price_dataset(), config=expl.EngineConfig(20, 21), executions=schedule
    )
    drifted_data = injected_price_dataset()
    next_open = drifted_data.bars["S00"][schedule[9]]
    drifted_data.bars["S00"][schedule[9]] = expl.Bar(
        next_open.open * 2, next_open.close, next_open.quote_volume
    )
    drifted = expl.build_correctness_plan(
        drifted_data, config=expl.EngineConfig(20, 21), executions=schedule
    )
    first_invested, unchanged_rebalance = drifted[8], drifted[9]
    assert first_invested.target == unchanged_rebalance.target
    assert "S00" in unchanged_rebalance.target
    assert unchanged_rebalance.terminal_exit_turnover == 0
    assert unchanged_rebalance.trade_turnover > 0
    assert unchanged_rebalance.trade_turnover > baseline[9].trade_turnover
    assert unchanged_rebalance.turnover == pytest.approx(unchanged_rebalance.trade_turnover)


def test_terminal_at_next_scheduled_execution_exits_once_at_that_event():
    schedule = review_schedule()[:-1]
    terminal_execution = schedule[9]
    data = injected_price_dataset(
        terminal_symbol="S00", terminal_date=dt.datetime.fromtimestamp(
            terminal_execution / 1000, tz=dt.UTC
        ).date(),
    )
    plan = expl.build_correctness_plan(data, config=expl.EngineConfig(20, 21), executions=schedule)
    prior_event, terminal_event = plan[8], plan[9]
    assert "S00" in prior_event.target
    assert "S00" not in prior_event.terminal_exits
    assert "S00" not in terminal_event.target
    assert terminal_event.terminal_exits.count("S00") == 1
    assert sum(event.terminal_exits.count("S00") for event in plan) == 1


def test_injected_engine_rejects_parameter_escape_and_split_outside_contract():
    data = injected_price_dataset()
    with pytest.raises(expl.Expl017Error, match="volatility window outside"):
        expl.build_correctness_plan(data, config=expl.EngineConfig(20, 999), executions=review_schedule())
    with pytest.raises(expl.Expl017Error, match="outside formal containment"):
        expl.build_correctness_plan(data, executions=[day(dt.date(2024, 1, 1))])
