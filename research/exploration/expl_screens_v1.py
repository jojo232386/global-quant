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


def load_closes(symbol: str) -> list[tuple[int, float]]:
    rows = []
    with open(DATA_DIR / f"{symbol}-1d.jsonl") as handle:
        for line in handle:
            row = json.loads(line)
            if TRAIN_START_MS <= row["open_time_utc_ms"] < TRAIN_END_MS:
                rows.append((row["open_time_utc_ms"], float(row["close"])))
    return rows


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


def benchmark(btc_ret: list[float], eth_ret: list[float], cost: float) -> dict:
    warm_b = slice_warmup(btc_ret, WARMUP_DAYS)
    warm_e = slice_warmup(eth_ret, WARMUP_DAYS)
    buy_hold = [0.5 * b + 0.5 * e for b, e in zip(warm_b, warm_e)]
    # daily-rebalanced 50/50, costs on tiny rebalance turnover (approx 15bps *
    # sum |w - 0.5| changes ~ bounded small); modelled as one side per unit drift
    daily_rb = []
    for b, e in zip(warm_b, warm_e):
        daily_rb.append(0.5 * b + 0.5 * e)
    drift = sum(abs(0.5 * (1 + b) / (0.5 * (1 + b) + 0.5 * (1 + e)) - 0.5)
                for b, e in zip(warm_b, warm_e))
    if daily_rb:
        daily_rb[0] -= drift * cost
    return {
        "btc": metrics(warm_b),
        "eth": metrics(warm_e),
        "static_5050_buyhold": metrics(buy_hold),
        "daily_rebalanced_5050_net": metrics(daily_rb),
    }


def main() -> None:
    btc = load_closes("BTCUSDT")
    eth = load_closes("ETHUSDT")
    assert len(btc) == len(eth) and len(btc) > 400, "unexpected data shape"
    btc_close = [c for _, c in btc]
    eth_close = [c for _, c in eth]
    btc_ret = daily_returns(btc_close)
    eth_ret = daily_returns(eth_close)

    output = {
        "screen": "EXPL-012 + EXPL-010 (reduced breadth) train-window screen",
        "protocol": "research/exploration/EXPLORATION_PROTOCOL.md",
        "claim_status": "NOT EVIDENCE - exploration tier screen only",
        "data": {
            "dataset_id": "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615",
            "files": {
                "BTCUSDT-1d.jsonl": sha256(DATA_DIR / "BTCUSDT-1d.jsonl"),
                "ETHUSDT-1d.jsonl": sha256(DATA_DIR / "ETHUSDT-1d.jsonl"),
            },
            "window": "2020-01-01..2023-12-31 (train only)",
            "days": len(btc),
            "funding": "excluded: long-only spot feasible for BTC/ETH",
        },
        "cost_per_side": COST_PER_SIDE,
        "benchmarks": benchmark(btc_ret, eth_ret, COST_PER_SIDE),
        "expl012_baseline": run_expl012(btc_ret, eth_ret, btc_close, eth_close, COST_PER_SIDE),
        "expl012_cost_stress_2x": run_expl012(btc_ret, eth_ret, btc_close, eth_close, COST_PER_SIDE * 2),
        "expl010_reduced_breadth_baseline": run_expl010(btc_ret, eth_ret, COST_PER_SIDE),
        "expl010_reduced_breadth_cost_stress_2x": run_expl010(btc_ret, eth_ret, COST_PER_SIDE * 2),
        "expl010_caveat": (
            "N=2 only (BTC/ETH); the card specifies a top-N universe. "
            "Full-breadth screen BLOCKED_ON_DATA: PIT universe files cover "
            "2026-02..2026-08 only (tainted region). Reduced-breadth result is "
            "a preliminary signal, not the card's registered screen."
        ),
    }
    out_path = Path(__file__).parent / "expl-screen-results-2026-08-22.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(json.dumps(output["benchmarks"], indent=2))
    print("\nEXPL-012 baseline:")
    for row in output["expl012_baseline"]:
        print(row)
    print("\nEXPL-012 stress 2x:")
    for row in output["expl012_cost_stress_2x"]:
        print(row)
    print("\nEXPL-010 (N=2) baseline:")
    for row in output["expl010_reduced_breadth_baseline"]:
        print(row)
    print("\nresults written to", out_path)


if __name__ == "__main__":
    main()
