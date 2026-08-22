#!/usr/bin/env python3
"""EXPL-008 / EXPL-015 exploration screens (train window only).

Implements the comparison specs frozen in hypothesis-backlog.md on
2026-08-22 (see the SPEC FROZEN blocks there; deviations are diagnostics
only). Reuses the tested primitives from expl_screens_v1.

Shared conventions (identical to v1 unless stated):
- Window: train-only 2020-01-01..2023-12-31; warm closes W[j] carry the
  day-start timestamp times[j]; ret k spans W[k] -> W[k+1]; anything
  computed through close W[k] gates bar k.
- Funding: perp shorts make funding a real cost; the day's total funding
  rate for bar k is the sum of events inside the day that STARTS at
  times[k+1]; P&L contribution = -position x rate (longs pay positive).
- Costs: |position change| x per-side cost on the change day; stress
  doubles per-side cost. Judgments full precision, rounded at output.

Per EXPLORATION_PROTOCOL.md: output is a screen result, never evidence.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from expl_screens_v1 import (
    ANN,
    COST_PER_SIDE,
    DATA_DIR,
    TRAIN_START_MS,
    TRAIN_END_MS,
    WARMUP_DAYS,
    daily_returns,
    load_closes_checked,
    metrics,
    round_metrics,
    sha256,
    slice_warmup,
    warm_window,
)

MOM_LOOKBACK = 30          # EXPL-008 momentum lookback (frozen in spec)
JUMP_VOL_LOOKBACK = 30     # EXPL-015 pre-jump vol window (frozen in spec)
FUNDING_Z_MIN_HISTORY = 30 # EXPL-015 funding z minimum history (frozen)
DAY_MS = 86_400_000


def load_daily_funding(symbol: str, day_starts: list[int],
                       data_dir: Path = DATA_DIR) -> list[float]:
    """Total funding rate per day-start, aligned to the bar whose day it is.
    day_starts[j] is the start of the day whose close is W[j]; the return
    bar k spans W[k] -> W[k+1], i.e. the day starting at day_starts[k+1]."""
    by_day: dict[int, float] = {}
    with open(data_dir / f"{symbol}-funding.jsonl") as handle:
        for line in handle:
            row = json.loads(line)
            t = row["fundingTime"]
            if TRAIN_START_MS <= t < TRAIN_END_MS + DAY_MS:
                day = t // DAY_MS * DAY_MS
                by_day[day] = by_day.get(day, 0.0) + float(row["fundingRate"])
    return [by_day.get(day_starts[k + 1], 0.0) for k in range(len(day_starts) - 1)]


def realized_vol(rets: list[float], window: int) -> list[float | None]:
    """rv[k] = annualized std of rets[k-window:k] (strictly before k, so it
    gates bar k without lookahead); None before k = window."""
    out: list[float | None] = [None] * len(rets)
    for k in range(window, len(rets)):
        hist = rets[k - window:k]
        mean = sum(hist) / len(hist)
        var = sum((r - mean) ** 2 for r in hist) / (len(hist) - 1)
        out[k] = math.sqrt(var) * math.sqrt(ANN)
    return out


def tsmom_signals(closes: list[float], lookback: int) -> list[float | None]:
    """sig[k] = sign(W[k]/W[k-lookback] - 1), known at close W[k], gates bar
    k; None before k = lookback."""
    out: list[float | None] = [None] * len(closes)
    for k in range(lookback, len(closes)):
        out[k] = math.copysign(1.0, closes[k] / closes[k - lookback] - 1.0)
    return out


def expanding_percentile_threshold(series: list[float | None], quantile: float,
                                   k: int) -> float | None:
    """Threshold at bar k from strictly prior non-None values (no lookahead).
    None until at least one prior value exists."""
    prior = [v for v in series[:k] if v is not None]
    if not prior:
        return None
    prior.sort()
    rank = quantile * (len(prior) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return prior[low]
    return prior[low] + (prior[high] - prior[low]) * (rank - low)


def position_return_series(rets: list[float], positions: list[float],
                           funding_rates: list[float], cost: float) -> list[float]:
    """Daily strategy returns: position x asset return - funding - trade
    costs. Position k was decided through close W[k] and earns bar k."""
    out = []
    prev = 0.0
    for k in range(len(rets)):
        r = positions[k] * rets[k] - positions[k] * funding_rates[k]
        r -= abs(positions[k] - prev) * cost
        prev = positions[k]
        out.append(r)
    return out


def run_expl008(btc_ret, eth_ret, btc_close, eth_close, btc_fund, eth_fund,
                cost: float) -> tuple[list[dict], dict]:
    """Gated TSMOM grid + the ungated benchmark (same construction)."""
    sig_b = tsmom_signals(btc_close, MOM_LOOKBACK)
    sig_e = tsmom_signals(eth_close, MOM_LOOKBACK)

    def build(rets, sig, closes, fund, vol_window, quantile):
        rv = realized_vol(rets, vol_window)
        positions = []
        for k in range(len(rets)):
            s = sig[k]
            if s is None:
                positions.append(0.0)
                continue
            thr = expanding_percentile_threshold(rv, quantile, k)
            gate_on = True if thr is None else rv[k] < thr
            positions.append(s if gate_on else 0.0)
        return position_return_series(rets, positions, fund, cost), positions

    results = []
    for vol_window in (14, 30):
        for quantile in (0.5, 1.0 / 3.0):
            legs = []
            for rets, sig, closes, fund in (
                (btc_ret, sig_b, btc_close, btc_fund),
                (eth_ret, sig_e, eth_close, eth_fund),
            ):
                series, _ = build(rets, sig, closes, fund, vol_window, quantile)
                legs.append(series)
            portfolio = [0.5 * a + 0.5 * b for a, b in zip(*legs)]
            results.append({
                "vol_window": vol_window,
                "gate": "median" if quantile == 0.5 else "tercile",
                "net": metrics(slice_warmup(portfolio, WARMUP_DAYS)),
            })
    # ungated benchmark: identical construction, gate always on
    legs = []
    for rets, sig, fund in ((btc_ret, sig_b, btc_fund), (eth_ret, sig_e, eth_fund)):
        positions = [s if s is not None else 0.0 for s in sig]
        legs.append(position_return_series(rets, positions, fund, cost))
    ungated = [0.5 * a + 0.5 * b for a, b in zip(*legs)]
    benchmark = {"ungated_tsmom": metrics(slice_warmup(ungated, WARMUP_DAYS))}
    return results, benchmark


def apply_events_netted(length: int, events: list[tuple[int, int, float]]) -> list[float]:
    """Net overlapping event positions with a ±1 cap (spec: 'overlapping
    events net; position capped at ±1'). Each event (k, horizon, position)
    covers bars k+1 .. k+horizon."""
    pending = [0.0] * length
    for k, horizon, position in events:
        for m in range(k + 1, min(k + 1 + horizon, length)):
            pending[m] += position
    return [max(-1.0, min(1.0, p)) for p in pending]


def run_expl015(btc_ret, eth_ret, btc_close, eth_close, btc_fund, eth_fund,
                cost: float) -> list[dict]:
    """Post-jump conditional drift grid; benchmark is cash (Sharpe > 0)."""
    results = []
    for k_sigma in (3, 4):
        for horizon in (1, 3, 5):
            legs = []
            for rets, closes, fund in (
                (btc_ret, btc_close, btc_fund),
                (eth_ret, eth_close, eth_fund),
            ):
                rv = realized_vol(rets, JUMP_VOL_LOOKBACK)
                # funding z from strictly prior daily sums
                fund_hist: list[float] = []
                events: list[tuple[int, int, float]] = []
                for k in range(len(rets)):
                    z = None
                    if len(fund_hist) >= FUNDING_Z_MIN_HISTORY:
                        mu = sum(fund_hist) / len(fund_hist)
                        var = sum((x - mu) ** 2 for x in fund_hist) / (len(fund_hist) - 1)
                        sd = math.sqrt(var)
                        if sd > 0:
                            z = (fund[k] - mu) / sd
                    if rv[k] is not None and z is not None:
                        jump = abs(rets[k]) > k_sigma * (rv[k] / math.sqrt(ANN))
                        if jump:
                            direction = math.copysign(1.0, rets[k])
                            position = direction if z <= 0 else -direction
                            events.append((k, horizon, position))
                    fund_hist.append(fund[k])
                positions = apply_events_netted(len(rets), events)
                legs.append(position_return_series(rets, positions, fund, cost))
            portfolio = [0.5 * a + 0.5 * b for a, b in zip(*legs)]
            results.append({
                "k_sigma": k_sigma, "horizon": horizon,
                "net": metrics(slice_warmup(portfolio, WARMUP_DAYS)),
            })
    return results


def judge_beats_and_stress(base_rows, stress_rows, benchmark_metrics, mode,
                           stress_benchmark_metrics=None):
    """Shared frozen judgment: beats both Sharpe and Calmar (expl008) or
    Sharpe > 0 (expl015 cash mode); primary = best baseline Sharpe among
    stress survivors. The stress regime compares against the SAME-cost
    benchmark (2x-cost strategy vs 2x-cost benchmark), not the 1x one."""
    if mode == "vs_benchmark":
        stress_bench = stress_benchmark_metrics or benchmark_metrics
        beats_base = lambda m: (
            m["net"]["sharpe"] > benchmark_metrics["sharpe"]
            and m["net"]["calmar"] > benchmark_metrics["calmar"])
        beats_stress = lambda m: (
            m["net"]["sharpe"] > stress_bench["sharpe"]
            and m["net"]["calmar"] > stress_bench["calmar"])
    else:
        beats_base = beats_stress = lambda m: m["net"]["sharpe"] > 0.0
    survivors = [i for i, row in enumerate(stress_rows) if beats_stress(row)]
    baseline_beats = sum(beats_base(row) for row in base_rows)
    if survivors:
        primary = max((base_rows[i] for i in survivors),
                      key=lambda row: row["net"]["sharpe"])
        primary_config = {k: v for k, v in primary.items() if k != "net"}
        return {"verdict": "KEPT_PRIMARY_SELECTED",
                "primary_config": primary_config,
                "baseline_beats_count": f"{baseline_beats}/{len(base_rows)}",
                "stress_survivors_count": f"{len(survivors)}/{len(stress_rows)}"}
    return {"verdict": "DROPPED_COST_FRAGILE",
            "baseline_beats_count": f"{baseline_beats}/{len(base_rows)}",
            "stress_survivors_count": f"0/{len(stress_rows)}"}


def main() -> None:
    times, btc_close_full, eth_close_full = load_closes_checked()
    btc_ret, eth_ret, btc_warm, eth_warm = warm_window(
        btc_close_full, eth_close_full, WARMUP_DAYS)
    warm_times = times[WARMUP_DAYS:]
    assert len(warm_times) == len(btc_warm)
    btc_fund = load_daily_funding("BTCUSDT", warm_times)
    eth_fund = load_daily_funding("ETHUSDT", warm_times)
    assert len(btc_fund) == len(btc_ret), "funding alignment broken"

    # closes passed to signal builders are the WARM closes (len = rets + 1)
    e8_base, e8_bench = run_expl008(
        btc_ret, eth_ret, btc_warm, eth_warm, btc_fund, eth_fund, COST_PER_SIDE)
    e8_stress, e8_stress_bench = run_expl008(
        btc_ret, eth_ret, btc_warm, eth_warm, btc_fund, eth_fund, COST_PER_SIDE * 2)
    e15_base = run_expl015(
        btc_ret, eth_ret, btc_warm, eth_warm, btc_fund, eth_fund, COST_PER_SIDE)
    e15_stress = run_expl015(
        btc_ret, eth_ret, btc_warm, eth_warm, btc_fund, eth_fund, COST_PER_SIDE * 2)

    e8_judgment = judge_beats_and_stress(
        e8_base, e8_stress, e8_bench["ungated_tsmom"], mode="vs_benchmark",
        stress_benchmark_metrics=e8_stress_bench["ungated_tsmom"])
    e15_judgment = judge_beats_and_stress(
        e15_base, e15_stress, None, mode="vs_cash")

    def present(rows):
        return [{**row, "net": round_metrics(row["net"])} for row in rows]

    output = {
        "screen": "EXPL-008 + EXPL-015 (specs frozen 2026-08-22)",
        "protocol": "research/exploration/EXPLORATION_PROTOCOL.md",
        "claim_status": "NOT EVIDENCE - exploration tier screen only",
        "data": {
            "dataset_id": "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615",
            "files": {
                "BTCUSDT-1d.jsonl": sha256(DATA_DIR / "BTCUSDT-1d.jsonl"),
                "ETHUSDT-1d.jsonl": sha256(DATA_DIR / "ETHUSDT-1d.jsonl"),
                "BTCUSDT-funding.jsonl": sha256(DATA_DIR / "BTCUSDT-funding.jsonl"),
                "ETHUSDT-funding.jsonl": sha256(DATA_DIR / "ETHUSDT-funding.jsonl"),
            },
            "window": "2020-01-01..2023-12-31 (train only)",
            "funding": "applied as position x daily rate sum (longs pay positive)",
            "formal_layer_note": "5x funding stress is NOT applied here; the "
                                 "formal gate retains it",
        },
        "expl008_judgment": e8_judgment,
        "expl008_benchmark": {
            "ungated_1x": round_metrics(e8_bench["ungated_tsmom"]),
            "ungated_2x_stress": round_metrics(e8_stress_bench["ungated_tsmom"]),
        },
        "expl008_baseline": present(e8_base),
        "expl008_cost_stress_2x": present(e8_stress),
        "expl015_judgment": e15_judgment,
        "expl015_baseline": present(e15_base),
        "expl015_cost_stress_2x": present(e15_stress),
    }
    out_path = Path(__file__).parent / "expl-screen-results-v2-2026-08-22.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print("EXPL-008 judgment:", e8_judgment)
    print("EXPL-015 judgment:", e15_judgment)
    print("results written to", out_path)


if __name__ == "__main__":
    main()
