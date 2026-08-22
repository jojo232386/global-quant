#!/usr/bin/env python3
"""EXPL-012 / EXPL-010-N2 exploration screens (train window only).

COMPARISON SPEC (frozen before implementation; reviews check conformance):

- Window: train-only 2020-01-01..2023-12-31, WARMUP_DAYS sliced at the front;
  every strategy and benchmark curve is measured on the identical day set.
- Data: VERIFIED curated V1 dataset 88d9ff34 (BTC/ETH 1d), loaded with a
  strict two-file timestamp-set equality check.
- Costs: spot long-only, funding excluded (feasible). Per side = taker
  5bps + slippage 10bps. Stress variant doubles per-side cost.
- Execution convention (both cards, unified): a signal or weight computed
  from data through close t takes effect for the bar starting at t; trade
  costs are charged on that bar. No lookahead anywhere.
- EXPL-012 (RS rotation): position is 100% BTC / 100% ETH / 50-50 via
  hysteresis on RS-ratio momentum; a switch costs 2 sides on the switch day.
  PRIMARY benchmark: true fixed-share buy-and-hold 50/50, rebased at the
  window start, no ongoing trades. Diagnostic benchmark: daily-rebalanced
  50/50 with per-day internal rebalancing costs.
- EXPL-010 N2 diagnostic: base and benchmark share the identical
  daily-rebalanced 50/50 construction with per-day internal rebalancing
  costs; the strategy additionally scales exposure by a banded vol target
  and pays one side per unit of executed weight change. It therefore
  differs from its benchmark ONLY by vol targeting.
- Judgments use FULL-precision metrics; rounding happens only in the
  serialized JSON output.

Per EXPLORATION_PROTOCOL.md: output is a screen result, never evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

DATA_DIR = Path(
    "/Users/ASUS/Desktop/gmaq-data/snapshots/btceth-weekly-tsmom/curated/"
    "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615/data"
)
TRAIN_START_MS = 1577836800000  # 2020-01-01
TRAIN_END_MS = 1704067200000    # 2024-01-01 (exclusive)
WARMUP_DAYS = 30
COST_PER_SIDE = 0.0015         # taker 5bps + slippage 10bps
ANN = 365.0


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_closes_checked(data_dir: Path = DATA_DIR) -> tuple[list[int], list[float], list[float]]:
    """Load both series independently; strict timestamp-set equality catches
    extra, missing, or misaligned bars in either file."""
    btc_rows: dict[int, float] = {}
    eth_rows: dict[int, float] = {}
    for symbol, rows in (("BTCUSDT", btc_rows), ("ETHUSDT", eth_rows)):
        with open(data_dir / f"{symbol}-1d.jsonl") as handle:
            for line in handle:
                row = json.loads(line)
                if TRAIN_START_MS <= row["open_time_utc_ms"] < TRAIN_END_MS:
                    rows[row["open_time_utc_ms"]] = float(row["close"])
    btc_ms, eth_ms = sorted(btc_rows), sorted(eth_rows)
    assert btc_ms == eth_ms, "BTC/ETH timestamp sets differ"
    return btc_ms, [btc_rows[m] for m in btc_ms], [eth_rows[m] for m in eth_ms]


def daily_returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def warm_window(btc_close: list[float], eth_close: list[float],
                warmup: int) -> tuple[list[float], list[float], list[float], list[float]]:
    """Post-warmup returns and closes on the identical day set.

    R[i] spans C[i] -> C[i+1]; post-warmup returns R[warmup..] involve
    closes C[warmup..], so warm closes start at index warmup (no +1).
    """
    btc_ret = slice_warmup(daily_returns(btc_close), warmup)
    eth_ret = slice_warmup(daily_returns(eth_close), warmup)
    btc_warm = btc_close[warmup:]
    eth_warm = eth_close[warmup:]
    assert len(btc_ret) == len(btc_warm) - 1, "window alignment broken"
    return btc_ret, eth_ret, btc_warm, eth_warm


def slice_warmup(seq: list, warmup: int) -> list:
    return seq[warmup:]


def metrics(returns: list[float]) -> dict:
    """Full-precision metrics; round only at serialization."""
    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1.0)
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    ann_ret = equity ** (ANN / len(returns)) - 1.0
    sharpe = mean / std * math.sqrt(ANN) if std > 0 else 0.0
    calmar = ann_ret / abs(mdd) if mdd < 0 else float("inf")
    return {
        "net_total_return": equity - 1.0,
        "annualized_return": ann_ret,
        "sharpe": sharpe,
        "calmar": calmar,
        "max_drawdown": mdd,
    }


def round_metrics(m: dict) -> dict:
    return {key: (round(value, 4) if isinstance(value, float) else value)
            for key, value in m.items()}


def buyhold_returns(btc_warm: list[float], eth_warm: list[float]) -> list[float]:
    """True fixed-share buy-and-hold 50/50, rebased at window start."""
    btc_rel = [c / btc_warm[0] for c in btc_warm]
    eth_rel = [c / eth_warm[0] for c in eth_warm]
    equity = [0.5 * b + 0.5 * e for b, e in zip(btc_rel, eth_rel)]
    return [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity))]


def daily_rb_with_costs(btc_ret: list[float], eth_ret: list[float],
                        cost: float) -> tuple[list[float], list[float]]:
    """Daily-rebalanced 50/50: gross returns plus per-day internal
    rebalancing cost. Restoring 50/50 sells |drift| of the winner and buys
    |drift| of the loser, so the traded notional is the SUM of absolute
    weight changes (2 x drift) and cost applies per side of that."""
    gross, net = [], []
    for b, e in zip(btc_ret, eth_ret):
        g = 0.5 * b + 0.5 * e
        wb = 0.5 * (1 + b) / (0.5 * (1 + b) + 0.5 * (1 + e))
        traded_notional = abs(wb - 0.5) + abs((1 - wb) - 0.5)
        gross.append(g)
        net.append(g - traded_notional * cost)
    return gross, net


def rotation_series(btc_ret: list[float], eth_ret: list[float],
                    rs_mom: list[float | None], band: float,
                    cost: float) -> tuple[list[float], int]:
    """EXPL-012 daily returns; rs_mom[i-1] gates day i (no lookahead);
    each state switch costs 2 sides on the switch day itself."""
    state = 0  # 0 = 50/50, 1 = BTC, -1 = ETH
    series, switches = [], 0
    for i in range(len(btc_ret)):
        signal = rs_mom[i - 1] if i >= 1 and rs_mom[i - 1] is not None else None
        r = 0.0
        if signal is not None:
            if signal > band:
                new_state = 1
            elif signal < -band:
                new_state = -1
            else:
                new_state = state
            if new_state != state:
                r -= 2 * cost
                switches += 1
                state = new_state
        if state == 1:
            r += btc_ret[i]
        elif state == -1:
            r += eth_ret[i]
        else:
            r += 0.5 * btc_ret[i] + 0.5 * eth_ret[i]
        series.append(r)
    return series, switches


def rs_momentum(btc_close: list[float], eth_close: list[float],
                lookback: int) -> list[float | None]:
    """RS-ratio momentum aligned to return bars: rs[j] uses closes[j+1]
    and closes[j+1-lookback], so the first computable slot is
    j = lookback - 1 (not lookback); earlier entries stay None. rs[j-1]
    gates return bar j without lookahead."""
    slots = len(btc_close) - 1
    rs: list[float | None] = [None] * slots
    for j in range(lookback - 1, slots):
        now = btc_close[j + 1] / eth_close[j + 1]
        then = btc_close[j + 1 - lookback] / eth_close[j + 1 - lookback]
        rs[j] = now / then - 1.0
    return rs


def run_expl012(btc_ret: list[float], eth_ret: list[float], btc_close: list[float],
                eth_close: list[float], cost: float) -> list[dict]:
    # closes MUST be the window-matched series: closes[i] is the start close
    # of returns[i] (len(closes) == len(returns) + 1). A misaligned pair
    # shifts every signal by the mismatch.
    assert len(btc_close) == len(btc_ret) + 1, "closes/returns misaligned"
    assert len(eth_close) == len(eth_ret) + 1, "closes/returns misaligned"
    results = []
    for lookback in (14, 30):
        for band in (0.03, 0.05):
            rs_mom = rs_momentum(btc_close, eth_close, lookback)
            series, switches = rotation_series(btc_ret, eth_ret, rs_mom, band, cost)
            results.append({
                "lookback": lookback, "band": band, "switches": switches,
                "net": metrics(series),
            })
    return results


def target_vol_series(base_gross: list[float], base_drift_cost: list[float],
                      target: float, band: float, cost: float,
                      vol_window: int = 30) -> tuple[list[float], float, list[float]]:
    """EXPL-010 N2: identical base construction as its benchmark, plus vol
    targeting. Unified execution convention (same as EXPL-012): a weight
    computed from data through close t takes effect for the bar starting at
    t; the trade cost is charged on that bar. Return (strategy net returns,
    executed turnover, weights)."""
    held = 1.0
    series, traded, weights = [], 0.0, []
    for i in range(len(base_gross)):
        trade_cost = 0.0
        if i >= vol_window:
            hist = base_gross[i - vol_window:i]
            mean = sum(hist) / len(hist)
            var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
            realized = math.sqrt(var) * math.sqrt(ANN)
            w_star = min(1.0, target / realized) if realized > 0 else 1.0
            if abs(w_star - held) / held > band:
                delta = abs(w_star - held)
                traded += delta
                trade_cost = delta * cost
                held = w_star
        series.append(held * base_gross[i] - held * base_drift_cost[i] - trade_cost)
        weights.append(held)
    return series, traded, weights


def run_expl010(btc_ret: list[float], eth_ret: list[float], cost: float) -> list[dict]:
    gross, drift_cost = daily_rb_with_costs(btc_ret, eth_ret, cost)
    results = []
    for target in (0.15, 0.20, 0.30):
        for band in (0.10, 0.20):
            series, traded, weights = target_vol_series(
                gross, drift_cost, target, band, cost)
            results.append({
                "target_vol": target, "band": band,
                "avg_weight": round(sum(weights) / len(weights), 3),
                "turnover_total": round(traded, 2),
                "net": metrics(series),
            })
    return results


def benchmark(btc_ret: list[float], eth_ret: list[float],
              btc_warm: list[float], eth_warm: list[float],
              cost: float) -> dict:
    bh = buyhold_returns(btc_warm, eth_warm)
    _, daily_rb_net = daily_rb_with_costs(btc_ret, eth_ret, cost)
    return {
        "btc": metrics(btc_ret),
        "eth": metrics(eth_ret),
        "static_5050_buyhold_primary": metrics(bh),
        "daily_rebalanced_5050_diagnostic": metrics(daily_rb_net),
    }


def main() -> None:
    _, btc_close, eth_close = load_closes_checked()
    btc_ret, eth_ret, btc_warm, eth_warm = warm_window(
        btc_close, eth_close, WARMUP_DAYS)
    benchmarks = benchmark(btc_ret, eth_ret, btc_warm, eth_warm, COST_PER_SIDE)
    bh = benchmarks["static_5050_buyhold_primary"]
    daily_rb = benchmarks["daily_rebalanced_5050_diagnostic"]

    expl012_base = run_expl012(btc_ret, eth_ret, btc_warm, eth_warm, COST_PER_SIDE)
    expl012_stress = run_expl012(btc_ret, eth_ret, btc_warm, eth_warm, COST_PER_SIDE * 2)
    expl010_base = run_expl010(btc_ret, eth_ret, COST_PER_SIDE)
    expl010_stress = run_expl010(btc_ret, eth_ret, COST_PER_SIDE * 2)

    # frozen judgment rule (EXPLORATION_PROTOCOL.md): full precision, primary
    # benchmark = true buy-and-hold; stress survivor = 2x-cost net Sharpe
    # still beats it; primary = best baseline net Sharpe among survivors.
    stress_survivors = [
        i for i, row in enumerate(expl012_stress)
        if row["net"]["sharpe"] > bh["sharpe"]
    ]
    baseline_beats = sum(
        row["net"]["sharpe"] > bh["sharpe"] for row in expl012_base
    )
    if stress_survivors:
        primary = max(
            (expl012_base[i] for i in stress_survivors),
            key=lambda row: row["net"]["sharpe"],
        )
        expl012_verdict = {
            "verdict": "KEPT_PRIMARY_SELECTED",
            "primary_config": {"lookback": primary["lookback"],
                               "band": primary["band"]},
            "baseline_beats_count": f"{baseline_beats}/{len(expl012_base)}",
            "stress_survivors_count": f"{len(stress_survivors)}/{len(expl012_stress)}",
        }
    else:
        expl012_verdict = {
            "verdict": "DROPPED_COST_FRAGILE",
            "baseline_beats_count": f"{baseline_beats}/{len(expl012_base)}",
            "stress_survivors_count": f"0/{len(expl012_stress)}",
        }

    # EXPL-010 N2: unified construction — the diagnostic benchmark IS the
    # unscaled version of the strategy's own base (daily-rb with costs).
    calmar_improves = any(
        row["net"]["calmar"] > daily_rb["calmar"] for row in expl010_base
    )
    expl010_verdict = {
        "EXPL-010_FULL": "BLOCKED_ON_DATA (pre-2024 top-N universe data absent)",
        "EXPL-010_N2_DIAGNOSTIC": (
            "DROPPED" if not calmar_improves else "KEPT_N2_ONLY"
        ),
        "note": "N=2 BTC/ETH diagnostic on unified daily-rebalanced "
                "construction; direction consistent with ASQ A5-1 but not a "
                "cross-market confirmation",
    }

    # required checks
    assert any(
        b["net"]["net_total_return"] != s["net"]["net_total_return"]
        for b, s in zip(expl012_base, expl012_stress)
    ), "cost stress did not change results"
    assert (bh["sharpe"] != daily_rb["sharpe"]
            or bh["max_drawdown"] != daily_rb["max_drawdown"]), (
        "buy-and-hold and daily-rebalanced benchmarks are semantically "
        "identical - benchmark fix regressed"
    )
    assert len(buyhold_returns(btc_warm, eth_warm)) == len(btc_ret), (
        "buy-and-hold curve length differs from strategy window"
    )

    def present(rows: list[dict]) -> list[dict]:
        return [{**row, "net": round_metrics(row["net"])} for row in rows]

    output = {
        "screen": "EXPL-012 + EXPL-010 N2 diagnostic (unified semantics)",
        "protocol": "research/exploration/EXPLORATION_PROTOCOL.md",
        "claim_status": "NOT EVIDENCE - exploration tier screen only",
        "comparison_spec": "see module docstring in expl_screens_v1.py",
        "correction": "third correction: unified EXPL-010 N2 base/benchmark "
                      "construction, full-precision judgments (rounding only "
                      "in this serialized output)",
        "data": {
            "dataset_id": "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615",
            "files": {
                "BTCUSDT-1d.jsonl": sha256(DATA_DIR / "BTCUSDT-1d.jsonl"),
                "ETHUSDT-1d.jsonl": sha256(DATA_DIR / "ETHUSDT-1d.jsonl"),
            },
            "window": "2020-01-01..2023-12-31 (train only)",
            "days": len(btc_close),
            "timestamp_alignment_checked": True,
            "funding": "excluded: long-only spot feasible for BTC/ETH",
        },
        "cost_per_side": COST_PER_SIDE,
        "checks": {
            "timestamps_aligned": True,
            "cost_stress_changes_results": True,
            "buyhold_and_daily_rb_differ": True,
            "buyhold_window_matches_strategy": True,
        },
        "benchmarks": {k: round_metrics(v) for k, v in benchmarks.items()},
        "expl012_judgment": expl012_verdict,
        "expl012_baseline": present(expl012_base),
        "expl012_cost_stress_2x": present(expl012_stress),
        "expl010_judgment": expl010_verdict,
        "expl010_n2_diagnostic_baseline": present(expl010_base),
        "expl010_n2_diagnostic_cost_stress_2x": present(expl010_stress),
        "expl010_n2_unscaled_comparator": round_metrics(daily_rb),
    }
    out_path = Path(__file__).parent / "expl-screen-results-2026-08-22.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print("EXPL-012 judgment:", expl012_verdict)
    print("EXPL-010 judgment:", expl010_verdict)
    print("results written to", out_path)


if __name__ == "__main__":
    main()
