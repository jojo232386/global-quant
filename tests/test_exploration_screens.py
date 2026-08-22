"""Contract tests for the exploration-tier screen implementation.

Hand-computed toy fixtures pin the semantics that three review rounds
found drifting: benchmark construction, window alignment, cost placement,
full-precision judgment inputs, and timestamp validation.
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "research" / "exploration" / "expl_screens_v1.py"

spec = importlib.util.spec_from_file_location("expl_screens_v1", MODULE_PATH)
expl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expl)


def test_buyhold_differs_from_daily_rebalanced():
    # BTC doubles on day 2 and rises again on day 3; ETH flat. Only from
    # day 3 do the two constructions actually diverge: buy-and-hold now
    # carries 2/3 BTC weight, the rebalanced book went back to 50/50.
    btc = [100.0, 100.0, 200.0, 220.0]  # 4 closes -> 3 return days
    eth = [100.0, 100.0, 100.0, 100.0]
    bh = expl.buyhold_returns(btc, eth)
    # day 1 flat -> 0.0; day 2: (0.5*2+0.5*1)/1 - 1 = 0.5;
    # day 3: (0.5*2.2+0.5*1)/(0.5*2+0.5*1) - 1 = 1.6/1.5 - 1 = 1/15
    assert bh == pytest.approx([0.0, 0.5, 1.0 / 15.0])
    gross, rb_costs = expl.daily_rb_with_costs(
        [0.0, 1.0, 0.10], [0.0, 0.0, 0.0], cost=0.01)
    # daily rebalanced: day 3 earns only 0.5*10% = 0.05 -> diverges from bh
    assert gross == pytest.approx([0.0, 0.5, 0.05])
    assert bh[2] != pytest.approx(gross[2])  # the actual divergence pin
    # restoring 50/50 after day 2's move trades BOTH legs: sell 1/6 BTC,
    # buy 1/6 ETH -> traded notional 1/3, not 1/6
    assert rb_costs[1] == pytest.approx((1.0 / 3.0) * 0.01)
    # day 3: both legs drift by 11/21 - 1/2 = 1/42 each -> traded 1/21 total
    assert rb_costs[2] == pytest.approx((1.0 / 21.0) * 0.01)


def test_target_vol_subtracts_cost_not_net():
    # regression: a prior version returned NET from daily_rb_with_costs and
    # the strategy subtracted it as if it were the cost, collapsing returns
    # to ~ -cost. The cost series must enter as a small deduction only.
    rets_b = [0.0, 1.0, 0.10]
    gross, rb_costs = expl.daily_rb_with_costs(rets_b, [0.0, 0.0, 0.0], cost=0.01)
    series, traded, weights = expl.target_vol_series(
        gross, rb_costs, target=1.0, band=10.0, cost=0.0, vol_window=30)
    # target=1.0 with a huge band -> weight stays 1, no vol trades; every
    # day must equal gross - cost, NOT gross - (gross - cost)
    assert weights == [1.0, 1.0, 1.0]
    for i in range(3):
        assert series[i] == pytest.approx(gross[i] - rb_costs[i])
    for i in (1, 2):  # nonzero days: a net-as-cost bug collapses these to ~-cost
        assert series[i] != pytest.approx(rb_costs[i])


def test_rs_momentum_first_legal_signal_boundary():
    btc = [100.0 * (1.01 ** i) for i in range(20)]  # steadily rising
    eth = [100.0] * 20
    rs = expl.rs_momentum(btc, eth, lookback=3)
    assert rs[1] is None      # j = lookback - 2: interval incomplete
    assert rs[2] is not None  # j = lookback - 1: first legal slot
    assert rs[2] == pytest.approx((btc[3] / eth[3]) / (btc[0] / eth[0]) - 1.0)


def test_first_tradeable_bar_end_to_end():
    btc = [100.0 * (1.01 ** i) for i in range(10)]
    eth = [100.0] * 10
    rets_b = expl.daily_returns(btc)
    rets_e = expl.daily_returns(eth)
    rs = expl.rs_momentum(btc, eth, lookback=3)
    series, switches = expl.rotation_series(rets_b, rets_e, rs, band=0.01, cost=0.0)
    # rs[lookback-1] gates bar `lookback`: the switch is felt on bar 3,
    # never on bar 2
    assert switches >= 1
    assert series[2] == pytest.approx(0.5 * rets_b[2])  # still 50/50
    assert series[3] == pytest.approx(rets_b[3])        # switched to BTC


def test_warm_window_alignment_no_off_by_one():
    closes = [100.0 * (1.01 ** i) for i in range(35)]
    rets, _, warm, _ = expl.warm_window(closes, closes, warmup=30)
    # R[i] spans C[i] -> C[i+1]; post-warmup returns R[30..] involve
    # closes C[30..]; warm closes must therefore start at index 30.
    assert warm[0] == closes[30]
    assert len(rets) == len(warm) - 1


def test_rotation_cost_lands_on_switch_day():
    # 5 days. rs_mom[1] is known at the close of day 1 and gates day 2
    # (rs_mom[i-1] -> day i, no lookahead): exactly one switch ON day 2,
    # charged 2 sides that day and only that day.
    btc_ret = [0.0, 0.0, 0.10, 0.0, 0.0]
    eth_ret = [0.0, 0.0, 0.0, 0.0, 0.0]
    rs_mom = [None, 0.10, None, None, None]  # known at close of day 1
    cost = 0.02
    series, switches = expl.rotation_series(btc_ret, eth_ret, rs_mom, 0.03, cost)
    assert switches == 1
    assert series[1] == pytest.approx(0.0)        # signal arrives after close
    assert series[2] == pytest.approx(0.10 - 2 * cost)  # switch day: in BTC, cost applied
    assert series[3] == pytest.approx(0.0)


def test_metrics_full_precision_not_prematurely_rounded():
    returns = [0.01, -0.02, 0.03, 0.005]
    m = expl.metrics(returns)
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    expected_sharpe = mean / math.sqrt(var) * math.sqrt(365.0)
    # judgment inputs must carry more digits than the 4-decimal display
    assert m["sharpe"] == pytest.approx(expected_sharpe, rel=1e-12)
    rounded = expl.round_metrics(m)
    assert rounded["sharpe"] == round(expected_sharpe, 4)


def test_target_vol_strategy_shares_base_with_benchmark():
    # Alternating returns give nonzero realized vol; a target far below
    # that vol forces the weight to target/realized. The strategy must
    # equal the SAME base construction scaled by the held weight (the
    # only difference vs its benchmark is the vol targeting itself).
    gross = [0.01 if i % 2 == 0 else -0.01 for i in range(40)]
    drift = [0.0] * 40
    series, traded, weights = expl.target_vol_series(
        gross, drift, target=0.0001, band=0.10, cost=0.01, vol_window=30)
    realized = 0.01 * math.sqrt(30.0 / 29.0) * math.sqrt(365.0)  # ddof=1
    w_star = min(1.0, 0.0001 / realized)
    assert weights[29] == 1.0                      # inside vol window: w=1
    assert weights[30] == pytest.approx(w_star)    # first vol-gated day
    assert series[29] == pytest.approx(gross[29])   # odd index -> -0.01
    # rebalance day: scaled base return minus one side on executed weight change
    assert series[30] == pytest.approx(w_star * gross[30] - (1.0 - w_star) * 0.01)
    assert traded == pytest.approx(1.0 - w_star)


def test_timestamp_set_mismatch_raises(tmp_path: pathlib.Path):
    def write(name: str, times: list[int]) -> None:
        rows = [{"open_time_utc_ms": t, "close": "100", "open": "100",
                 "high": "100", "low": "100", "volume": "1"} for t in times]
        (tmp_path / name).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    write("BTCUSDT-1d.jsonl", [expl.TRAIN_START_MS, expl.TRAIN_START_MS + 86_400_000])
    # ETH has an EXTRA bar the BTC file lacks -> must fail loudly
    write("ETHUSDT-1d.jsonl", [expl.TRAIN_START_MS, expl.TRAIN_START_MS + 86_400_000,
                               expl.TRAIN_START_MS + 2 * 86_400_000])
    with pytest.raises(AssertionError, match="timestamp sets differ"):
        expl.load_closes_checked(tmp_path)
