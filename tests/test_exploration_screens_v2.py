"""Contract tests for the v2 exploration screens (EXPL-008 / EXPL-015).

All expectations are HAND-DERIVED constants; none reference the module's
own outputs (the round-5 lesson). Funding alignment, vol/momentum warmup
boundaries, expanding-percentile no-lookahead, cost/funding placement in
the position P&L, and both judgment modes are pinned.
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "research" / "exploration" / "expl_screens_v2.py"

sys.path.insert(0, str(MODULE_PATH.parent))  # so v2 can import expl_screens_v1
spec = importlib.util.spec_from_file_location("expl_screens_v2", MODULE_PATH)
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)


def test_funding_day_alignment(tmp_path: pathlib.Path):
    # two days; bar k earns the funding of the day starting at
    # day_starts[k+1] (the day the bar spans)
    day0, day1 = v2.TRAIN_START_MS, v2.TRAIN_START_MS + v2.DAY_MS
    rows = [
        {"fundingTime": day0 + 1000, "fundingRate": "0.0001"},   # day0
        {"fundingTime": day0 + v2.DAY_MS - 1000, "fundingRate": "0.0002"},  # day0
        {"fundingTime": day1 + 5000, "fundingRate": "-0.0003"},  # day1
        {"fundingTime": v2.TRAIN_END_MS + v2.DAY_MS, "fundingRate": "9"},  # out
    ]
    (tmp_path / "SYM-funding.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    got = v2.load_daily_funding("SYM", [day0, day1], tmp_path)
    assert got == [pytest.approx(-0.0003)]        # only bar 0 (day1) exists
    got3 = v2.load_daily_funding("SYM", [day0, day1, day1 + v2.DAY_MS], tmp_path)
    # hand-check: bar k -> day_starts[k+1]; bar0 -> day1 sum = -0.0003
    assert got3[0] == pytest.approx(-0.0003)
    assert got3[1] == pytest.approx(0.0)          # day beyond file -> 0


def test_realized_vol_boundary_and_value():
    rets = [0.01, -0.01, 0.01, -0.01, 0.02]
    rv = v2.realized_vol(rets, window=2)
    assert rv[0] is None and rv[1] is None
    # rv[2] uses rets[0:2]; hand: mean 0, var = (1e-4 + 1e-4)/1 = 2e-4
    assert rv[2] == pytest.approx(math.sqrt(2e-4) * math.sqrt(365.0))
    # rv[4] uses rets[2:4] (strictly before bar 4), NOT the 0.02 jump
    assert rv[4] == pytest.approx(math.sqrt(2e-4) * math.sqrt(365.0))


def test_tsmom_signal_boundary_and_sign():
    closes = [100.0] * 31 + [110.0]  # flat then +10% at index 31
    sig = v2.tsmom_signals(closes, lookback=30)
    assert sig[29] is None          # k = lookback - 1: interval incomplete
    assert sig[30] is not None      # first legal slot: W[30]/W[0] - 1
    assert sig[30] == 1.0
    assert sig[31] == 1.0


def test_expanding_percentile_uses_only_prior_values():
    series = [None, 1.0, 2.0, 3.0, 100.0]
    # at k=4, prior non-None values are [1, 2, 3]; median = 2 regardless
    # of the current 100
    assert v2.expanding_percentile_threshold(series, 0.5, 4) == pytest.approx(2.0)
    # tercile (1/3) with linear interpolation: rank = 2/3 -> 1 + 2/3
    assert v2.expanding_percentile_threshold(series, 1.0 / 3.0, 4) == pytest.approx(1 + 2.0 / 3.0)
    # no prior values -> None (gate defaults on)
    assert v2.expanding_percentile_threshold(series, 0.5, 0) is None


def test_position_return_costs_and_funding():
    rets = [0.10, 0.05, 0.0]
    positions = [1.0, 1.0, -1.0]      # flip on bar 2
    funding = [0.0001, 0.0002, 0.0003]
    cost = 0.001
    got = v2.position_return_series(rets, positions, funding, cost)
    # bar0: +1 * 0.10 - 1*0.0001 - |1-0|*0.001
    assert got[0] == pytest.approx(0.10 - 0.0001 - 0.001)
    # bar1: hold, funding only
    assert got[1] == pytest.approx(0.05 - 0.0002)
    # bar2: flip +1 -> -1 = 2 units of cost; short pays nothing, receives
    # positive funding: -(-1)*0.0003 = +0.0003
    assert got[2] == pytest.approx(0.0 + 0.0003 - 2 * 0.001)


def test_overlapping_events_net_with_cap():
    # event A (k=1, h=3, +1) covers bars 2,3,4; event B (k=2, h=3, -1)
    # covers bars 3,4,5. Netting: bar2 +1, bars 3-4 cancel to 0, bar5 -1.
    got = v2.apply_events_netted(7, [(1, 3, 1.0), (2, 3, -1.0)])
    assert got == [0.0, 0.0, 1.0, 0.0, 0.0, -1.0, 0.0]
    # same-direction overlap nets to +2 then caps at +1
    got2 = v2.apply_events_netted(5, [(0, 2, 1.0), (1, 2, 1.0)])
    assert got2 == [0.0, 1.0, 1.0, 1.0, 0.0]


def test_judgment_uses_same_cost_benchmark_for_stress():
    # stress comparison must use the 2x-cost benchmark: a strategy at 2x
    # costs beating only the 1x benchmark is NOT a stress survivor.
    base = [{"net": {"sharpe": 1.5, "calmar": 1.2}, "tag": "a"}]
    stress = [{"net": {"sharpe": 1.05, "calmar": 1.0}, "tag": "a"}]
    bench_1x = {"sharpe": 1.0, "calmar": 0.9}
    bench_2x = {"sharpe": 1.1, "calmar": 0.95}
    out = v2.judge_beats_and_stress(
        base, stress, bench_1x, mode="vs_benchmark",
        stress_benchmark_metrics=bench_2x)
    # 1.05 < 1.1 stress benchmark -> dropped even though it beats 1x
    assert out["verdict"] == "DROPPED_COST_FRAGILE"


def test_judgment_modes():
    bench = {"sharpe": 1.0, "calmar": 0.9}
    base = [
        {"net": {"sharpe": 1.5, "calmar": 1.2}, "tag": "a"},
        {"net": {"sharpe": 0.8, "calmar": 1.5}, "tag": "b"},
    ]
    stress = [
        {"net": {"sharpe": 1.2, "calmar": 1.0}, "tag": "a"},
        {"net": {"sharpe": 0.5, "calmar": 1.4}, "tag": "b"},
    ]
    out = v2.judge_beats_and_stress(base, stress, bench, mode="vs_benchmark")
    # only grid point a beats Sharpe AND Calmar at baseline and under stress
    assert out["verdict"] == "KEPT_PRIMARY_SELECTED"
    assert out["primary_config"] == {"tag": "a"}
    # cash mode: Sharpe > 0 only
    out2 = v2.judge_beats_and_stress(base, stress, None, mode="vs_cash")
    assert out2["primary_config"] == {"tag": "a"}  # both >0; best baseline wins
    out3 = v2.judge_beats_and_stress(
        [{"net": {"sharpe": 0.5, "calmar": 0.1}, "tag": "x"}],
        [{"net": {"sharpe": -0.1, "calmar": 0.1}, "tag": "x"}],
        None, mode="vs_cash")
    assert out3["verdict"] == "DROPPED_COST_FRAGILE"
