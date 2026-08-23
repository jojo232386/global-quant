"""Hand-derived contracts for the shared Price Alpha v1 runner."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "research/exploration/price_alpha_v1.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("price_alpha_v1", MODULE)
alpha = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = alpha
spec.loader.exec_module(alpha)


def toy_dataset(series: dict[str, list[tuple[int, float, float, float]]], pit=None):
    bars = {
        symbol: {
            timestamp: alpha.Bar(open_price, close, quote)
            for timestamp, open_price, close, quote in rows
        }
        for symbol, rows in series.items()
    }
    return alpha.PriceDataset(
        bars=bars,
        last_timestamp={symbol: max(points) for symbol, points in bars.items()},
        pit=pit or {},
        artifact_path=pathlib.Path("/tmp/not-used"),
        manifest_sha256="x",
        pit_sha256="y",
        labels=("survivor-biased", "exploration-only"),
    )


def test_average_rank_ties_are_deterministic():
    got = alpha.normalized_ranks({"B": 1.0, "A": 1.0, "C": 3.0})
    assert got == {"A": 0.25, "B": 0.25, "C": 1.0}


def test_top_n_uses_effective_pool_and_trailing_median_with_symbol_tie_break():
    execution = alpha.START_MS
    decision = execution - alpha.DAY_MS
    symbols = ("AAA", "BBB", "CCC")
    series = {}
    for symbol, volume in (("AAA", 10.0), ("BBB", 20.0), ("CCC", 20.0)):
        rows = [
            (decision - offset * alpha.DAY_MS, 100.0, 100.0, volume)
            for offset in range(90)
        ]
        rows.append((execution, 100.0, 100.0, volume))
        series[symbol] = rows
    dataset = toy_dataset(series, {alpha.month_start_ms(execution): symbols})
    selected, volumes, inactive = alpha.top_liquid(dataset, execution, decision, 2)
    assert selected == ["BBB", "CCC"]
    assert volumes == {"BBB": 20.0, "CCC": 20.0}
    assert inactive == 0


def test_next_open_execution_never_earns_decision_bar_close_move():
    start = alpha.START_MS
    dataset = toy_dataset(
        {
            "AAA": [
                # The decision/same-bar close move is enormous but must not
                # enter P&L. Execution starts at this row's open.
                (start, 100.0, 500.0, 10.0),
                (start + alpha.DAY_MS, 110.0, 121.0, 10.0),
            ]
        }
    )
    event = alpha.Event(
        execution_ms=start,
        decision_ms=start - alpha.DAY_MS,
        weights={"AAA": 1.0},
        signals={"AAA": 1.0},
        forward_returns={},
    )
    simulation = alpha.simulate(dataset, [event])
    assert simulation.gross[0] == pytest.approx(0.10)  # open 100 -> next open 110
    assert simulation.gross[0] != pytest.approx(4.0)   # open 100 -> same close 500
    # The terminal bar earns its observed open-to-close move, then exits.
    assert simulation.gross[1] == pytest.approx(0.10)
    assert simulation.terminal_liquidations == [
        {"symbol": "AAA", "date": alpha.iso(start + alpha.DAY_MS),
         "method": "final_open_to_close_then_exit"}
    ]


def test_long_short_flip_turnover_charges_both_legs():
    start = alpha.START_MS
    rows = [
        (start + offset * alpha.DAY_MS, 100.0, 100.0, 10.0)
        for offset in range(3)
    ]
    dataset = toy_dataset({"LONG": rows, "SHORT": rows})
    events = [
        alpha.Event(start, start - alpha.DAY_MS, {"LONG": 0.5, "SHORT": -0.5}, {}, {}),
        alpha.Event(
            start + alpha.DAY_MS,
            start,
            {"LONG": -0.5, "SHORT": 0.5},
            {},
            {},
        ),
    ]
    simulation = alpha.simulate(dataset, events)
    assert simulation.turnover[0] == pytest.approx(1.0)  # initial two legs
    assert simulation.turnover[1] == pytest.approx(2.0)  # two 1.0 sign flips


def test_signal_diagnostics_forward_quintiles_and_rank_ic_alignment():
    start = alpha.START_MS
    signals = {f"S{i}": float(i) for i in range(5)}
    forwards = {f"S{i}": float(i) / 100 for i in range(5)}
    events = [
        alpha.Event(
            start + offset * alpha.DAY_MS,
            start + (offset - 1) * alpha.DAY_MS,
            {},
            signals,
            forwards,
        )
        for offset in range(3)
    ]
    got = alpha.signal_diagnostics(
        events, start, start + 3 * alpha.DAY_MS, active_only=True
    )
    assert got["rank_ic"]["mean"] == pytest.approx(1.0)
    assert got["forward_return_quintiles_q1_to_q5"] == pytest.approx(
        [0.0, 0.01, 0.02, 0.03, 0.04]
    )
    assert got["q5_minus_q1_spread"] == pytest.approx(0.04)


def test_btc_regime_uses_only_trailing_close_information():
    execution = alpha.START_MS
    decision = execution - alpha.DAY_MS
    dataset = toy_dataset(
        {
            "BTCUSDT": [
                (decision - 90 * alpha.DAY_MS, 100.0, 100.0, 1.0),
                (decision, 121.0, 121.0, 1.0),
            ]
        }
    )
    assert alpha.btc_regime(dataset, execution) == "bull"
    dataset.bars["BTCUSDT"][decision] = alpha.Bar(100.0, 79.0, 1.0)
    assert alpha.btc_regime(dataset, execution) == "bear"
    dataset.bars["BTCUSDT"][decision] = alpha.Bar(100.0, 100.0, 1.0)
    assert alpha.btc_regime(dataset, execution) == "sideways"


def test_hac_positive_constant_is_finite_json_safe():
    got = alpha.hac_mean_test([0.1, 0.1, 0.1, 0.1])
    assert got["mean"] == pytest.approx(0.1)
    assert got["t_stat"] is None
    assert got["p_one_sided_normal"] == 0.0


def test_metrics_reports_all_day_and_active_day_win_rates():
    got = alpha.portfolio_metrics([0.0, 0.10, -0.05, 0.0], [0.0] * 4)
    assert got["win_rate"] == pytest.approx(0.25)
    assert got["active_days"] == 2
    assert got["active_day_win_rate"] == pytest.approx(0.5)
