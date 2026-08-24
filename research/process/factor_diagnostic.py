"""Pure-stdlib, diagnostic-only cross-sectional factor summaries.

The caller supplies point-in-time-safe observations and future returns.  This
module validates their shape but cannot establish that their timestamps are
PIT-safe, executable, or appropriate for a formal study.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any, Final


DIAGNOSTIC_STATUS: Final = "DIAGNOSTIC_ONLY_NOT_A_FORMAL_VERDICT"


class FactorDiagnosticError(ValueError):
    """Diagnostic input is malformed or cannot support the stated metric."""


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise FactorDiagnosticError(f"{name} must be a finite real number")
    return float(value)


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        stop = start + 1
        while stop < len(order) and values[order[stop]] == values[order[start]]:
            stop += 1
        average = (start + 1 + stop) / 2.0
        for position in range(start, stop):
            ranks[order[position]] = average
        start = stop
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float], label: str) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise FactorDiagnosticError(f"{label} requires at least two paired observations")
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    numerator = math.fsum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_scale = math.fsum((a - left_mean) ** 2 for a in left)
    right_scale = math.fsum((b - right_mean) ** 2 for b in right)
    if left_scale == 0.0 or right_scale == 0.0:
        raise FactorDiagnosticError(f"{label} is degenerate")
    return numerator / math.sqrt(left_scale * right_scale)


def _target_weights(rows: Sequence[dict[str, Any]], quantiles: int) -> tuple[dict[str, float], float]:
    if len(rows) < quantiles:
        raise FactorDiagnosticError("each timestamp needs at least one observation per quantile")
    ranked = sorted(rows, key=lambda row: (row["factor"], row["symbol"]))
    bucket_size = len(ranked) // quantiles
    if bucket_size == 0:
        raise FactorDiagnosticError("quantile bucket is empty")
    bottom = ranked[:bucket_size]
    top_start = len(ranked) - bucket_size
    top = ranked[top_start:]
    if ranked[bucket_size - 1]["factor"] == ranked[bucket_size]["factor"]:
        raise FactorDiagnosticError("bottom quantile boundary tie")
    if ranked[top_start - 1]["factor"] == ranked[top_start]["factor"]:
        raise FactorDiagnosticError("top quantile boundary tie")
    weights = {row["symbol"]: -0.5 / bucket_size for row in bottom}
    weights.update({row["symbol"]: 0.5 / bucket_size for row in top})
    spread = math.fsum(row["future_return"] for row in top) / bucket_size - math.fsum(
        row["future_return"] for row in bottom
    ) / bucket_size
    return weights, spread


def _validated_rows(observations: Sequence[Mapping[str, object]]) -> dict[object, list[dict[str, Any]]]:
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence) or not observations:
        raise FactorDiagnosticError("observations must be a non-empty sequence")
    grouped: dict[object, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[object, str]] = set()
    timestamp_type: type[object] | None = None
    for row in observations:
        if not isinstance(row, Mapping) or set(row) != {"timestamp", "symbol", "factor", "future_return"}:
            raise FactorDiagnosticError("each observation must have exactly timestamp, symbol, factor, future_return")
        timestamp = row["timestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (str, int, float)):
            raise FactorDiagnosticError("timestamp must be a string or numeric value")
        if isinstance(timestamp, float) and not math.isfinite(timestamp):
            raise FactorDiagnosticError("timestamp must be finite")
        if isinstance(timestamp, str) and (not timestamp or timestamp != timestamp.strip()):
            raise FactorDiagnosticError("timestamp string must be non-empty and stripped")
        if timestamp_type is None:
            timestamp_type = type(timestamp)
        elif type(timestamp) is not timestamp_type:
            raise FactorDiagnosticError("timestamps must use one type")
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
            raise FactorDiagnosticError("symbol must be a non-empty stripped string")
        key = (timestamp, symbol)
        if key in seen:
            raise FactorDiagnosticError("duplicate timestamp/symbol observation")
        seen.add(key)
        grouped[timestamp].append(
            {"symbol": symbol, "factor": _finite_number("factor", row["factor"]), "future_return": _finite_number("future_return", row["future_return"])}
        )
    if len(grouped) < 2:
        raise FactorDiagnosticError("turnover requires at least two timestamps")
    return grouped


def factor_diagnostic(
    observations: Sequence[Mapping[str, object]], *, quantiles: int = 5, cost_bps: Sequence[Real] = (0.0, 10.0, 25.0)
) -> dict[str, object]:
    """Compute diagnostic IC, Rank IC, quantile spread, turnover, and cost sensitivity.

    ``future_return`` is one already-defined endpoint return per decision-time
    observation.  It must not be replaced by a terminal value, a later
    observation, or an execution result.  For each timestamp, IC and Rank IC
    are Pearson correlations of respectively raw and average ranks; spread is
    mean(top-quantile return) minus mean(bottom-quantile return).  Two-leg
    turnover is the direct gross-notional transition ``sum(abs(delta weight))``
    between gross-one equal-weight long-top/short-bottom targets (long sums to
    ``+0.5`` and short sums to ``-0.5``), with no half-turnover convention. It
    excludes the initial allocation and final liquidation. The raw
    top-minus-bottom spread remains ``mean_quantile_spread``; the comparable
    gross-one diagnostic return is half of that spread. Cost sensitivity
    subtracts one-way cost times total transition turnover amortized across all
    return timestamps from that gross-one return, without compounding.

    This is not a formal gate, result classifier, or trading instruction.
    """
    if isinstance(quantiles, bool) or not isinstance(quantiles, int) or quantiles < 2:
        raise FactorDiagnosticError("quantiles must be an integer >= 2")
    if isinstance(cost_bps, (str, bytes)) or not isinstance(cost_bps, Sequence) or not cost_bps:
        raise FactorDiagnosticError("cost_bps must be a non-empty sequence")
    costs = [_finite_number("cost_bps", item) for item in cost_bps]
    if any(cost < 0.0 for cost in costs) or len(set(costs)) != len(costs):
        raise FactorDiagnosticError("cost_bps must be unique non-negative values")
    grouped = _validated_rows(observations)
    timestamps = sorted(grouped)
    ics: list[float] = []
    rank_ics: list[float] = []
    spreads: list[float] = []
    targets: list[dict[str, float]] = []
    for timestamp in timestamps:
        rows = grouped[timestamp]
        factors = [row["factor"] for row in rows]
        returns = [row["future_return"] for row in rows]
        ics.append(_correlation(factors, returns, "IC"))
        rank_ics.append(_correlation(_average_ranks(factors), _average_ranks(returns), "Rank IC"))
        weights, spread = _target_weights(rows, quantiles)
        targets.append(weights)
        spreads.append(spread)
    turnovers = [
        math.fsum(abs(current.get(symbol, 0.0) - previous.get(symbol, 0.0)) for symbol in set(previous) | set(current))
        for previous, current in zip(targets, targets[1:])
    ]
    mean_spread = math.fsum(spreads) / len(spreads)
    mean_gross_one_return = 0.5 * mean_spread
    total_transition_turnover = math.fsum(turnovers)
    amortized_transition_turnover = total_transition_turnover / len(timestamps)
    return {
        "artifact_class": "GMAQ_FACTOR_DIAGNOSTIC_V1",
        "status": DIAGNOSTIC_STATUS,
        "observation_count": sum(len(rows) for rows in grouped.values()),
        "timestamp_count": len(timestamps),
        "quantiles": quantiles,
        "mean_ic": math.fsum(ics) / len(ics),
        "mean_rank_ic": math.fsum(rank_ics) / len(rank_ics),
        "mean_quantile_spread": mean_spread,
        "mean_gross_one_return": mean_gross_one_return,
        "total_transition_two_leg_turnover": total_transition_turnover,
        "mean_amortized_transition_two_leg_turnover": amortized_transition_turnover,
        "cost_sensitivity": [
            {
                "cost_bps": cost,
                "mean_gross_one_return_after_cost": mean_gross_one_return - (cost / 10_000.0) * amortized_transition_turnover,
            }
            for cost in costs
        ],
    }
