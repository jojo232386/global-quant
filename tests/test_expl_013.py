import copy
import datetime as dt
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "research" / "exploration"))
import expl_013 as runner  # noqa: E402
from expl_013 import (  # noqa: E402
    Bar, DAY_MS, PriceAlphaError, band_rebalance, compose_gates,
    cost, eligible_top_n, inverse_vol_weights, metrics, path_report,
    simulate_path, turnover, verify_preregistration, write_result,
)


def ms(year, month, day=1):
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


class Toy:
    """Gapless PIT toy whose future opens/closes can be changed per test."""

    def __init__(self, start, end, symbols=("A", "B", "C")):
        self.bars = {symbol: {} for symbol in symbols}
        first = start - 100 * DAY_MS
        for index, timestamp in enumerate(range(first, end, DAY_MS)):
            for rank, symbol in enumerate(symbols):
                # Non-constant, deterministic completed returns keep vol valid.
                price = 100.0 * (1.0 + 0.0005 * index + 0.0001 * rank * (index % 7))
                self.bars[symbol][timestamp] = Bar(price, price, 1000.0)
        self.members = {}

    def bar(self, symbol, timestamp):
        try:
            return self.bars[symbol][timestamp]
        except KeyError as error:
            raise PriceAlphaError("DATA_ERROR_STOP: toy bar absent") from error

    def universe(self, execution_ms):
        return tuple(self.members.get(runner.month_start_ms(execution_ms), tuple(self.bars)))

    def set_bar(self, symbol, timestamp, *, open=None, close=None, volume=None):
        old = self.bars[symbol][timestamp]
        self.bars[symbol][timestamp] = Bar(
            old.open if open is None else open,
            old.close if close is None else close,
            old.quote_volume if volume is None else volume,
        )


def constant_targets(monkeypatch):
    monkeypatch.setattr(
        runner, "inverse_vol_weights",
        lambda dataset, symbols, decision_ms, window=30: {
            symbol: 1.0 / len(symbols) for symbol in symbols
        },
    )


def test_eligible_topn_and_vol_are_decision_time_only():
    toy = Toy(ms(2021, 1), ms(2021, 4), ("A", "B"))
    decision = ms(2021, 1) - DAY_MS
    assert eligible_top_n(toy, decision, 2) == ["A", "B"]
    before = inverse_vol_weights(toy, ["A", "B"], decision, window=10)
    toy.set_bar("A", ms(2021, 1), close=1.0, volume=1e12)
    assert inverse_vol_weights(toy, ["A", "B"], decision, window=10) == before


def test_buy_and_hold_weights_drift_without_free_daily_rebalance(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 1, 4)
    toy = Toy(start, end, ("A", "B"))
    toy.set_bar("A", start, open=100, close=100)
    toy.set_bar("A", start + DAY_MS, open=200, close=200)
    toy.set_bar("B", start, open=100, close=100)
    toy.set_bar("B", start + DAY_MS, open=100, close=100)
    path = simulate_path(toy, start, end, 2, "equal")
    assert path.weights[0] == {"A": 0.5, "B": 0.5}
    assert path.weights[1]["A"] == pytest.approx(2 / 3)
    assert path.weights[1]["B"] == pytest.approx(1 / 3)
    assert path.turnover[1] == 0


def test_band_uses_decision_close_not_execution_open(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 2, 3)
    toy = Toy(start, end, ("A", "B"))
    jan31, feb1 = ms(2021, 1, 31), ms(2021, 2)
    for timestamp in range(start, jan31 + DAY_MS, DAY_MS):
        for symbol in ("A", "B"):
            toy.set_bar(symbol, timestamp, open=100, close=100)
    # Decision-close incumbent remains 50/50, then A gaps at execution open.
    toy.set_bar("A", jan31, open=100, close=100)
    toy.set_bar("B", jan31, open=100, close=100)
    toy.set_bar("A", feb1, open=200, close=200)
    toy.set_bar("B", feb1, open=100, close=100)
    path = simulate_path(toy, start, end, 2, "banded", 0.20)
    index = path.dates.index(feb1)
    assert not path.rebalanced[index]
    assert path.turnover[index] == 0
    assert path.weights[index]["A"] == pytest.approx(2 / 3)


def test_trigger_turnover_uses_execution_open_incumbent(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 2, 3)
    toy = Toy(start, end, ("A", "B"))
    jan31, feb1 = ms(2021, 1, 31), ms(2021, 2)
    for timestamp in range(start, jan31 + DAY_MS, DAY_MS):
        for symbol in ("A", "B"):
            toy.set_bar(symbol, timestamp, open=100, close=100)
    toy.set_bar("A", jan31, open=100, close=200)  # known trigger
    toy.set_bar("B", jan31, open=100, close=100)
    toy.set_bar("A", feb1, open=300, close=300)   # actual execution incumbent 75/25
    toy.set_bar("B", feb1, open=100, close=100)
    path = simulate_path(toy, start, end, 2, "banded", 0.20)
    index = path.dates.index(feb1)
    assert path.rebalanced[index]
    assert path.turnover[index] == pytest.approx(0.5)
    assert path.weights[index] == {"A": 0.5, "B": 0.5}


def test_membership_change_forces_full_rebalance(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 2, 3)
    toy = Toy(start, end, ("A", "B", "C"))
    toy.members[ms(2021, 1)] = ("A", "B")
    toy.members[ms(2021, 2)] = ("A", "C")
    path = simulate_path(toy, start, end, 2, "banded", 0.35)
    index = path.dates.index(ms(2021, 2))
    assert path.rebalanced[index]
    assert set(path.symbol_turnover[index]) == {"A", "B", "C"}
    assert "B" not in path.weights[index] and "C" in path.weights[index]


def test_entry_exit_costs_and_terminal_interval_are_counted_once(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 1, 3)
    toy = Toy(start, end, ("A", "B"))
    for symbol in ("A", "B"):
        toy.set_bar(symbol, start, open=100, close=100)
        toy.set_bar(symbol, start + DAY_MS, open=110, close=110)
    toy.set_bar("A", start + DAY_MS, open=110, close=121)
    path = simulate_path(toy, start, end, 2, "equal")
    assert len(path.dates) == 2
    assert path.gross == pytest.approx([0.10, 0.05])
    assert path.turnover == pytest.approx([1.0, 1.0])
    assert path.symbol_turnover[-1]["A"] == pytest.approx(0.55 / 1.05)
    assert path.symbol_turnover[-1]["B"] == pytest.approx(0.50 / 1.05)
    assert path.net() == pytest.approx([0.10 - 0.0015, 0.05 - 0.0015])
    report = path_report(path)
    assert sum(report["symbol_net_contribution"]["baseline"].values()) == pytest.approx(0.15 - 0.003)


def test_continuous_state_is_not_reset_at_oos_boundary(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 12), ms(2022, 1, 3)
    toy = Toy(start, end, ("A", "B"))
    path = simulate_path(toy, start, end, 2, "banded", 0.20)
    index = path.dates.index(ms(2022, 1))
    assert not path.rebalanced[index]
    assert path.turnover[index] == 0
    assert path.turnover[0] == pytest.approx(1.0)


def test_midperiod_missing_held_open_is_data_stop(monkeypatch):
    constant_targets(monkeypatch)
    start, end = ms(2021, 1), ms(2021, 1, 4)
    toy = Toy(start, end, ("A", "B"))
    del toy.bars["B"][start + DAY_MS]
    with pytest.raises(PriceAlphaError, match="DATA_ERROR_STOP"):
        simulate_path(toy, start, end, 2, "banded")


def test_band_turnover_cost_and_invalid_metrics_fail_closed():
    assert not band_rebalance({"A": 0.52, "B": 0.48}, {"A": 0.5, "B": 0.5}, 0.20)
    assert band_rebalance({"A": 0.5, "B": 0.5}, {"A": 0.5, "C": 0.5}, 0.35)
    value = turnover({"A": 0.5, "B": 0.5}, {"A": 1.0})
    assert value == pytest.approx(1.0)
    assert cost(value) == pytest.approx(0.0015)
    assert cost(value, True) == pytest.approx(0.003)
    assert metrics([]) is None
    assert metrics([0.01, 0.01]) is None
    assert metrics([math.nan, 0.01]) is None


def metric(sharpe=1.2, total=0.2, drawdown=-0.10, calmar=2.0):
    return {"sharpe": sharpe, "total_return": total,
            "max_drawdown": drawdown, "calmar": calmar}


def segment(kind):
    if kind == "primary":
        base, stress, turn = metric(), metric(1.0, 0.15), 75.0
    elif kind == "equal":
        base, stress, turn = metric(1.0, 0.16, -0.12, 1.5), metric(0.9, 0.12, -0.13, 1.3), 100.0
    else:
        base, stress, turn = metric(1.25, 0.22), metric(1.05, 0.17), 100.0
    contributions = {f"S{i:02d}": 1.0 for i in range(30)}
    return {"metrics": {"baseline": base, "stress": stress},
            "turnover_total": turn, "rebalances": 1,
            "symbol_net_contribution": {"baseline": contributions, "stress": contributions},
            "max_observed_weight": 0.1, "minimum_names": 30}


def gate_fixture():
    reports = {"banded": {}, "equal_weight": {}, "unbanded": {}}
    half = {"banded": {}, "equal_weight": {}, "unbanded": {}}
    for band, n in runner.GRID:
        key = f"n{n}_band{int(band * 100)}"
        reports["banded"][key] = {name: segment("primary") for name in ("train", "oos", "holdout")}
        half["banded"][key] = {split.name: segment("primary") for split in runner.HALF_YEARS}
    for n in (10, 30):
        nkey = f"n{n}"
        reports["equal_weight"][nkey] = {name: segment("equal") for name in ("train", "oos", "holdout")}
        reports["unbanded"][nkey] = {name: segment("unbanded") for name in ("train", "oos", "holdout")}
        half["equal_weight"][nkey] = {split.name: segment("equal") for split in runner.HALF_YEARS}
        half["unbanded"][nkey] = {split.name: segment("unbanded") for split in runner.HALF_YEARS}
    return reports, half


def test_full_gate_composition_and_exact_failure_reasons():
    reports, half = gate_fixture()
    gates = compose_gates(reports, half)
    assert gates["all_required"]
    assert gates["parameter_stability"]["passed_points"] == 4
    assert not gates["regime_diagnostics"]["new_volatility_threshold_or_gate_added"]

    broken = copy.deepcopy(reports)
    broken["banded"]["n30_band20"]["holdout"]["metrics"]["baseline"]["total_return"] = -0.1
    gates = compose_gates(broken, half)
    assert not gates["all_required"]
    assert "baseline_total_return_positive" in gates["final_holdout_primary"]["failed_checks"]


def test_unassociated_stress_calmar_is_not_an_extra_gate():
    comparison = {"primary": metric(), "equal_weight": metric(1.0, 0.16, -0.12, 1.5),
                  "unbanded": metric(1.25), "stress": {**metric(1.0, 0.15), "calmar": None},
                  "equal_weight_stress": {**metric(0.9, 0.12), "calmar": None},
                  "unbanded_stress": None, "turnover_ratio": 0.75}
    assert runner.primary_gate(comparison)


def test_parameter_stability_requires_three_of_four_in_both_periods():
    reports, half = gate_fixture()
    for key in ("n10_band20", "n10_band35"):
        reports["banded"][key]["oos"]["metrics"]["stress"]["total_return"] = -0.1
    gates = compose_gates(reports, half)
    assert gates["parameter_stability"]["passed_points"] == 2
    assert not gates["parameter_stability"]["passed"]


def test_prereg_binding_and_writer_nan_rejection(tmp_path):
    assert verify_preregistration() == runner.PREREG_SHA256
    with pytest.raises(ValueError):
        write_result(tmp_path / "report.json", {"value": float("nan")})
    report = runner.write_report({"outcome": "test"}, tmp_path)
    assert report.exists()
    with pytest.raises(PriceAlphaError, match="refusing overwrite"):
        runner.write_report({"outcome": "test2"}, tmp_path)


def test_code_binding_rejects_dirty_tree_or_missing_freeze(monkeypatch):
    responses = {
        ("ls-files", "--error-unmatch", "research/exploration/expl_013.py"): "research/exploration/expl_013.py",
        ("ls-files", "--error-unmatch", "research/exploration/expl-013-preregistration.json"): "research/exploration/expl-013-preregistration.json",
        ("status", "--porcelain", "--untracked-files=no"): " M gmaq_data/__init__.py",
    }
    monkeypatch.setattr(runner, "_git_output", lambda *args: responses[args])
    with pytest.raises(PriceAlphaError, match="tracked worktree differs"):
        runner.verify_clean_research_commit()

    responses[("status", "--porcelain", "--untracked-files=no")] = ""
    responses[("rev-parse", "HEAD")] = "a" * 40
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            runner.subprocess.CalledProcessError(1, args[0])
        ),
    )
    with pytest.raises(PriceAlphaError, match="not descended from frozen contract"):
        runner.verify_clean_research_commit()


def test_main_returns_nonzero_on_data_error(monkeypatch, capsys):
    monkeypatch.setattr(runner, "load_dataset",
                        lambda: (_ for _ in ()).throw(PriceAlphaError("DATA_ERROR_STOP: expected")))
    assert runner.main() == 2
    assert "DATA_ERROR_STOP" in capsys.readouterr().err
