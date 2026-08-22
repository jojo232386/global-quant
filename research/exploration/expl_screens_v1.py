#!/usr/bin/env python3
"""EXPL-012 / EXPL-010 exploration screens (train window only).

Per research/exploration/EXPLORATION_PROTOCOL.md: output is a screen result,
never evidence. Data: VERIFIED curated V1 dataset 88d9ff34 (btceth-weekly-tsmom)
read read-only. Window: 2020-01-01 <= t < 2024-01-01 (train only; 2024+ is
tainted for selection). Costs: spot long-only, funding excluded (long-only
spot is feasible for BTC/ETH; per-side cost = taker 5bps + slippage 10bps =
15bps, stress x2). Pure stdlib.
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


def load_closes_checked() -> tuple[list[int], list[float], list[float]]:
    """Load both series independently; a strict timestamp-set equality
    assert catches extra, missing, or misaligned bars in either file."""
    btc_rows: dict[int, float] = {}
    eth_rows: dict[int, float] = {}
    for symbol, rows in (("BTCUSDT", btc_rows), ("ETHUSDT", eth_rows)):
        with open(DATA_DIR / f"{symbol}-1d.jsonl") as handle:
            for line in handle:
                row = json.loads(line)
                if TRAIN_START_MS <= row["open_time_utc_ms"] < TRAIN_END_MS:
                    rows[row["open_time_utc_ms"]] = float(row["close"])
    btc_ms, eth_ms = sorted(btc_rows), sorted(eth_rows)
    assert btc_ms == eth_ms, "BTC/ETH timestamp sets differ"
    return btc_ms, [btc_rows[m] for m in btc_ms], [eth_rows[m] for m in eth_ms]


def daily_returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def metrics(returns: list[float]) -> dict:
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
        "net_total_return": round(equity - 1.0, 4),
        "annualized_return": round(ann_ret, 4),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "max_drawdown": round(mdd, 4),
    }


def slice_warmup(seq: list, warmup: int) -> list:
    return seq[warmup:]


def run_expl012(btc_ret: list[float], eth_ret: list[float], btc_close: list[float],
                eth_close: list[float], cost: float) -> list[dict]:
    results = []
    for lookback in (14, 30):
        for band in (0.03, 0.05):
            # rs_mom[i] uses data up to i (signal known at close of i,
            # position takes effect from i+1 -> shift by one below).
            rs_mom = [None] * len(btc_ret)
            for i in range(lookback, len(btc_ret)):
                ratio_now = btc_close[i + 1] / eth_close[i + 1]
                ratio_then = btc_close[i + 1 - lookback] / eth_close[i + 1 - lookback]
                rs_mom[i] = ratio_now / ratio_then - 1.0
            state = 0  # 0 = 50/50, 1 = BTC, -1 = ETH
            strat, switches = [], 0
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
                        # full switch = sell one asset + buy the other = 2 sides,
                        # charged on the switch day itself
                        r -= 2 * cost
                        switches += 1
                        state = new_state
                if state == 1:
                    r += btc_ret[i]
                elif state == -1:
                    r += eth_ret[i]
                else:
                    r += 0.5 * btc_ret[i] + 0.5 * eth_ret[i]
                strat.append(r)
            warm = slice_warmup(strat, WARMUP_DAYS)
            results.append({
                "lookback": lookback, "band": band, "switches": switches,
                "net": metrics(warm),
            })
    return results


def run_expl010(btc_ret: list[float], eth_ret: list[float], cost: float) -> list[dict]:
    port = [0.5 * b + 0.5 * e for b, e in zip(btc_ret, eth_ret)]
    vol_window = 30
    results = []
    for target in (0.15, 0.20, 0.30):
        for band in (0.10, 0.20):
            weights, traded = [], 0.0
            held = 1.0  # start fully invested
            eq_ret = []
            for i in range(len(port)):
                r = held * port[i]
                if i >= vol_window:
                    hist = port[i - vol_window:i]
                    mean = sum(hist) / len(hist)
                    var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
                    realized = math.sqrt(var) * math.sqrt(ANN)
                    w_star = min(1.0, target / realized) if realized > 0 else 1.0
                    if abs(w_star - held) / held > band:
                        delta = abs(w_star - held)
                        traded += delta
                        # rebalance executes same day, cost charged then
                        r -= delta * cost
                        held = w_star
                weights.append(held)
                eq_ret.append(r)
            warm = slice_warmup(eq_ret, WARMUP_DAYS)
            results.append({
                "target_vol": target, "band": band,
                "avg_weight": round(sum(weights) / len(weights), 3),
                "turnover_total": round(traded, 2),
                "net": metrics(warm),
            })
    return results


def benchmark(btc_ret: list[float], eth_ret: list[float], cost: float,
              btc_close_warm: list[float], eth_close_warm: list[float]) -> dict:
    # True static 50/50 buy-and-hold: fixed initial notional split, weights
    # drift with prices, no trades after entry. Rebased at the post-warmup
    # start so all curves are measured on identical days.
    btc_rel = [c / btc_close_warm[0] for c in btc_close_warm]
    eth_rel = [c / eth_close_warm[0] for c in eth_close_warm]
    bh_equity = [0.5 * b + 0.5 * e for b, e in zip(btc_rel, eth_rel)]
    bh_ret = [bh_equity[i] / bh_equity[i - 1] - 1.0 for i in range(1, len(bh_equity))]
    # Daily-rebalanced 50/50 with its own rebalancing costs (diagnostic
    # benchmark; semantically different from buy-and-hold).
    daily_rb = [0.5 * b + 0.5 * e for b, e in zip(btc_ret, eth_ret)]
    drift = sum(abs(0.5 * (1 + b) / (0.5 * (1 + b) + 0.5 * (1 + e)) - 0.5)
                for b, e in zip(btc_ret, eth_ret))
    if daily_rb:
        daily_rb[0] -= drift * cost
    return {
        "btc": metrics(btc_ret),
        "eth": metrics(eth_ret),
        "static_5050_buyhold_primary": metrics(bh_ret),
        "daily_rebalanced_5050_diagnostic": metrics(daily_rb),
    }


def main() -> None:
    _, btc_close, eth_close = load_closes_checked()
    btc_ret_full = daily_returns(btc_close)
    eth_ret_full = daily_returns(eth_close)
    # post-warmup window shared by every strategy and benchmark below.
    # R[i] spans C[i] -> C[i+1], so post-warmup returns R[30..] involve
    # closes C[30..]: warm closes must start at index WARMUP_DAYS (no +1)
    # or the buy-and-hold curve loses its first day (review finding).
    btc_ret = slice_warmup(btc_ret_full, WARMUP_DAYS)
    eth_ret = slice_warmup(eth_ret_full, WARMUP_DAYS)
    btc_close_warm = btc_close[WARMUP_DAYS:]
    eth_close_warm = eth_close[WARMUP_DAYS:]
    assert len(btc_ret) == len(btc_close_warm) - 1, "window alignment broken"

    benchmarks = benchmark(btc_ret, eth_ret, COST_PER_SIDE,
                           btc_close_warm, eth_close_warm)
    bh = benchmarks["static_5050_buyhold_primary"]

    expl012_base = run_expl012(btc_ret_full, eth_ret_full, btc_close,
                               eth_close, COST_PER_SIDE)
    expl012_stress = run_expl012(btc_ret_full, eth_ret_full, btc_close,
                                 eth_close, COST_PER_SIDE * 2)
    expl010_base = run_expl010(btc_ret_full, eth_ret_full, COST_PER_SIDE)
    expl010_stress = run_expl010(btc_ret_full, eth_ret_full, COST_PER_SIDE * 2)

    # frozen judgment rule (see EXPLORATION_PROTOCOL.md graduation rules):
    # beats = net Sharpe > primary benchmark (true buy-and-hold 50/50);
    # stress survivor = grid point whose 2x-cost net Sharpe still beats it;
    # primary config = highest baseline net Sharpe among stress survivors;
    # no stress survivor -> DROPPED (cost-fragile).
    stress_survivors = [
        i for i, row in enumerate(expl012_stress)
        if row["net"]["sharpe"] > bh["sharpe"]
    ]
    baseline_beats = [
        row for row in expl012_base if row["net"]["sharpe"] > bh["sharpe"]
    ]
    if stress_survivors:
        primary = max(
            (expl012_base[i] for i in stress_survivors),
            key=lambda row: row["net"]["sharpe"],
        )
        expl012_verdict = {
            "verdict": "KEPT_PRIMARY_SELECTED",
            "primary_config": {"lookback": primary["lookback"],
                               "band": primary["band"]},
            "baseline_beats_count": f"{len(baseline_beats)}/{len(expl012_base)}",
            "stress_survivors_count": f"{len(stress_survivors)}/{len(expl012_stress)}",
        }
    else:
        expl012_verdict = {
            "verdict": "DROPPED_COST_FRAGILE",
            "baseline_beats_count": f"{len(baseline_beats)}/{len(expl012_base)}",
            "stress_survivors_count": "0/4",
        }

    # EXPL-010's card compares against unscaled buy-and-hold; use the
    # primary static benchmark, not the daily-rebalanced diagnostic.
    unscaled = bh
    calmar_improves = any(
        row["net"]["calmar"] > unscaled["calmar"] for row in expl010_base
    )
    expl010_verdict = {
        "EXPL-010_FULL": "BLOCKED_ON_DATA (pre-2024 top-N universe data absent)",
        "EXPL-010_N2_DIAGNOSTIC": (
            "DROPPED" if not calmar_improves else "INCONCLUSIVE"
        ),
        "note": "N=2 BTC/ETH diagnostic only; direction consistent with ASQ "
                "A5-1 but not a cross-market confirmation",
    }

    # required checks
    assert any(
        b["net"]["net_total_return"] != s["net"]["net_total_return"]
        for b, s in zip(expl012_base, expl012_stress)
    ), "cost stress did not change results"
    daily_rb_metrics = benchmarks["daily_rebalanced_5050_diagnostic"]
    assert (bh["sharpe"] != daily_rb_metrics["sharpe"]
            or bh["max_drawdown"] != daily_rb_metrics["max_drawdown"]), (
        "buy-and-hold and daily-rebalanced benchmarks are semantically "
        "identical - benchmark fix regressed"
    )

    output = {
        "screen": "EXPL-012 + EXPL-010 N2 diagnostic (corrected benchmarks)",
        "protocol": "research/exploration/EXPLORATION_PROTOCOL.md",
        "claim_status": "NOT EVIDENCE - exploration tier screen only",
        "correction": "supersedes the first run: true static buy-and-hold "
                      "benchmark, EXPL-010 split into FULL vs N2 diagnostic, "
                      "frozen judgment rule applied in code",
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
        },
        "benchmarks": benchmarks,
        "expl012_judgment": expl012_verdict,
        "expl012_baseline": expl012_base,
        "expl012_cost_stress_2x": expl012_stress,
        "expl010_judgment": expl010_verdict,
        "expl010_n2_diagnostic_baseline": expl010_base,
        "expl010_n2_diagnostic_cost_stress_2x": expl010_stress,
    }
    out_path = Path(__file__).parent / "expl-screen-results-2026-08-22.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print("benchmarks:", json.dumps(benchmarks, indent=2))
    print("\nEXPL-012 judgment:", expl012_verdict)
    print("\nEXPL-010 judgment:", expl010_verdict)
    print("\nresults written to", out_path)


if __name__ == "__main__":
    main()
