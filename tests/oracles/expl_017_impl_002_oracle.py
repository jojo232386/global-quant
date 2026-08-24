"""Independent EXPL-017 gold oracle committed before the production runner.

This module intentionally imports no research or production code.  It derives
expected ranks, regimes, targets, NAV, turnover, and lifecycle cash movements
from the fixed toy inputs in the gold artifact.
"""
from __future__ import annotations

import math
import statistics


BASE_COST = 0.0015
ANN = 365.0


def _ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values, key=lambda symbol: (values[symbol], symbol))
    return {symbol: index / (len(ordered) - 1) for index, symbol in enumerate(ordered)}


def momentum_scores(closes: dict[str, list[float]]) -> dict[str, float]:
    horizon_ranks = []
    for horizon in (1, 2):
        values = {symbol: series[-1] / series[-1 - horizon] - 1.0
                  for symbol, series in closes.items()}
        horizon_ranks.append(_ranks(values))
    return {symbol: statistics.mean(ranks[symbol] for ranks in horizon_ranks)
            for symbol in closes}


def broad_volatility(closes: dict[str, list[float]]) -> float:
    per_name = []
    for series in closes.values():
        returns = [series[index] / series[index - 1] - 1.0
                   for index in range(1, len(series))]
        per_name.append(statistics.stdev(returns) * math.sqrt(ANN))
    return statistics.median(per_name)


def target(scores: dict[str, float], state: str) -> dict[str, float]:
    ordered = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
    loser, winner = ordered[0], ordered[-1]
    if state == "calm":
        return {winner: 0.5, loser: -0.5}
    if state == "high":
        return {winner: -0.5, loser: 0.5}
    raise ValueError(state)


def nav_mark(cash: float, notionals: dict[str, float], relatives: dict[str, float]) -> tuple[float, dict[str, float]]:
    marked = {symbol: notional * relatives[symbol] for symbol, notional in notionals.items()}
    nav = cash + sum(marked.values())
    return nav, {symbol: value / nav for symbol, value in marked.items()}


def turnover(weights: dict[str, float], target_weights: dict[str, float]) -> float:
    return sum(abs(target_weights.get(symbol, 0.0) - weights.get(symbol, 0.0))
               for symbol in set(weights) | set(target_weights))


def oracle_values() -> dict[str, object]:
    closes = {
        "A": [100.0, 102.0, 104.0], "B": [100.0, 101.0, 102.0],
        "C": [100.0, 100.0, 100.5], "D": [100.0, 99.0, 98.0],
        "E": [100.0, 98.0, 96.0],
    }
    scores = momentum_scores(closes)
    broad = broad_volatility(closes)

    entry_cash = 1.0 - BASE_COST
    entry_notionals = {"A": 0.5, "E": -0.5}
    drift_nav, drift_weights = nav_mark(entry_cash, entry_notionals, {"A": 2.0, "E": 1.0})
    drift_turnover = turnover(drift_weights, entry_notionals)

    terminal_nav, terminal_weights = nav_mark(
        entry_cash, entry_notionals, {"A": 1.0, "E": 0.8}
    )
    terminal_exit_turnover = abs(terminal_weights["E"])
    terminal_exit_cost = abs(-0.4) * BASE_COST
    terminal_cash_after_exit = entry_cash - 0.4 - terminal_exit_cost
    terminal_final_nav = terminal_cash_after_exit + 0.5 * 1.1

    return {
        "momentum": scores,
        "broad_volatility": broad,
        "calm_target": target(scores, "calm"),
        "high_target": target(scores, "high"),
        "drift_nav": drift_nav,
        "drift_weights": drift_weights,
        "drift_turnover": drift_turnover,
        "drift_cost_dollars": drift_turnover * BASE_COST * drift_nav,
        "terminal_nav_before_exit": terminal_nav,
        "terminal_exit_turnover": terminal_exit_turnover,
        "terminal_exit_cost_dollars": terminal_exit_cost,
        "terminal_final_nav": terminal_final_nav,
    }
