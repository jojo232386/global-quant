#!/usr/bin/env python3
"""Frozen EXPL-013 price-only inverse-volatility experiment.

This fail-closed runner models buy-and-hold drift between trades and runs one
continuous 2021--2023 path before slicing it into frozen evaluation periods.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics
import subprocess
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from price_alpha_v1 import (  # noqa: E402
    DAY_MS, DATA_ROOT, DATASET, MANIFEST_SHA256, PIT_SHA256, SNAPSHOT_ID,
    Bar, PriceAlphaError, PriceDataset, month_start_ms, sha256_file,
)

ANN = 365.0
BASE_COST, STRESS_COST = 0.0015, 0.0030
WINDOW, VOLUME_WINDOW = 30, 90
GRID = ((0.20, 10), (0.35, 10), (0.20, 30), (0.35, 30))
FREEZE_COMMIT = "9ca27eb625bae14c6df0ad798583823567eaafe1"
PREREG_SHA256 = "db81b3a59fa3962b0a1ce328b7c14a6fd4091b360e5d43c3ec9b99eecd3e7dda"
PREREG_PATH = pathlib.Path(__file__).with_name("expl-013-preregistration.json")
OUTPUT_NAME = "expl-013-report.json"


def load_dataset(data_root: pathlib.Path = DATA_ROOT) -> PriceDataset:
    from price_alpha_v1 import load_dataset as _load
    return _load(data_root)


def _finite(value: float) -> bool:
    return math.isfinite(value)


def eligible_top_n(dataset: PriceDataset, decision_ms: int, n: int) -> list[str]:
    """Select by completed PIT history, median volume desc, symbol asc."""
    if n <= 0:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid N")
    rows: list[tuple[str, float]] = []
    for symbol in dataset.universe(decision_ms + DAY_MS):
        try:
            volumes = [dataset.bar(symbol, decision_ms - i * DAY_MS).quote_volume
                       for i in range(VOLUME_WINDOW)]
            closes = [dataset.bar(symbol, decision_ms - i * DAY_MS).close
                      for i in range(WINDOW + 1)]
        except (KeyError, PriceAlphaError):
            continue
        if not all(_finite(x) and x >= 0 for x in volumes):
            continue
        if not all(_finite(x) and x > 0 for x in closes):
            continue
        rows.append((symbol, statistics.median(volumes)))
    if len(rows) < n:
        raise PriceAlphaError(f"DATA_ERROR_STOP: only {len(rows)} eligible names for top-{n}")
    return [s for s, _ in sorted(rows, key=lambda x: (-x[1], x[0]))[:n]]


def inverse_vol_weights(dataset: PriceDataset, symbols: Sequence[str],
                        decision_ms: int, window: int = WINDOW) -> dict[str, float]:
    if not symbols or window <= 1:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid volatility input")
    inverse: dict[str, float] = {}
    for symbol in symbols:
        returns = []
        for offset in reversed(range(window)):
            now = dataset.bar(symbol, decision_ms - offset * DAY_MS).close
            prior = dataset.bar(symbol, decision_ms - (offset + 1) * DAY_MS).close
            if not (_finite(now) and _finite(prior) and now > 0 and prior > 0):
                raise PriceAlphaError("DATA_ERROR_STOP: invalid close history")
            returns.append(now / prior - 1.0)
        volatility = statistics.stdev(returns) * math.sqrt(ANN)
        if not _finite(volatility) or volatility <= 0:
            raise PriceAlphaError("DATA_ERROR_STOP: zero/non-finite volatility")
        inverse[symbol] = 1.0 / volatility
    denominator = sum(inverse.values())
    if not _finite(denominator) or denominator <= 0:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid inverse-vol denominator")
    return {s: value / denominator for s, value in inverse.items()}


def equal_weight(symbols: Sequence[str]) -> dict[str, float]:
    unique = tuple(dict.fromkeys(symbols))
    if not unique:
        raise PriceAlphaError("DATA_ERROR_STOP: empty equal-weight universe")
    return {symbol: 1.0 / len(unique) for symbol in unique}


def band_rebalance(incumbent: Mapping[str, float], target: Mapping[str, float],
                   band: float) -> bool:
    """Membership-first decision-close band test."""
    if not _finite(band) or band < 0:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid band")
    if not target or any(not _finite(v) or v <= 0 for v in target.values()):
        raise PriceAlphaError("DATA_ERROR_STOP: invalid target")
    if not incumbent or set(incumbent) != set(target):
        return True
    if any(not _finite(v) or v < 0 for v in incumbent.values()):
        raise PriceAlphaError("DATA_ERROR_STOP: invalid incumbent")
    return any(abs(incumbent[s] - target[s]) / target[s] > band for s in target)


def turnover(current: Mapping[str, float], target: Mapping[str, float]) -> float:
    value = sum(abs(target.get(s, 0.0) - current.get(s, 0.0))
                for s in set(current) | set(target))
    if not _finite(value) or value < 0:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid turnover")
    return value


def cost(turnover_value: float, stress: bool = False) -> float:
    if not _finite(turnover_value) or turnover_value < 0:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid turnover")
    return turnover_value * (STRESS_COST if stress else BASE_COST)


@dataclass(frozen=True)
class Split:
    name: str
    start_ms: int
    end_ms: int


def _utc_ms(year: int, month: int, day: int = 1) -> int:
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


SPLITS = (
    Split("train", _utc_ms(2021, 1), _utc_ms(2022, 1)),
    Split("oos", _utc_ms(2022, 1), _utc_ms(2023, 1)),
    Split("holdout", _utc_ms(2023, 1), _utc_ms(2024, 1)),
)
HALF_YEARS = (
    Split("2022-H1", _utc_ms(2022, 1), _utc_ms(2022, 7)),
    Split("2022-H2", _utc_ms(2022, 7), _utc_ms(2023, 1)),
    Split("2023-H1", _utc_ms(2023, 1), _utc_ms(2023, 7)),
    Split("2023-H2", _utc_ms(2023, 7), _utc_ms(2024, 1)),
)


def continuous_split(dates: Sequence[int], splits: Sequence[Split] = SPLITS) -> dict[str, list[int]]:
    if any(b < a for a, b in zip(dates, dates[1:])):
        raise PriceAlphaError("DATA_ERROR_STOP: dates not ordered")
    return {s.name: [i for i, value in enumerate(dates) if s.start_ms <= value < s.end_ms]
            for s in splits}


def metrics(returns: Sequence[float], turnover_values: Sequence[float] = ()) -> dict[str, float | None] | None:
    values, turns = list(returns), list(turnover_values)
    if len(values) < 2 or any(not _finite(v) or v <= -1 for v in values):
        return None
    if turns and (len(turns) != len(values) or any(not _finite(v) or v < 0 for v in turns)):
        return None
    volatility = statistics.stdev(values)
    if not _finite(volatility) or volatility <= 0:
        return None
    equity = peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        if not _finite(equity) or equity <= 0:
            return None
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    total = equity - 1.0
    annualized = equity ** (ANN / len(values)) - 1.0
    sharpe = statistics.mean(values) / volatility * math.sqrt(ANN)
    calmar = annualized / abs(drawdown) if drawdown < 0 else None
    numbers = (total, annualized, sharpe, drawdown, sum(turns))
    if not all(_finite(v) for v in numbers):
        return None
    return {"total_return": total, "annualized_return": annualized, "sharpe": sharpe,
            "max_drawdown": drawdown, "calmar": calmar, "turnover_total": sum(turns)}


def _mark_weights(weights: Mapping[str, float], returns: Mapping[str, float]) -> dict[str, float]:
    if set(weights) != set(returns):
        raise PriceAlphaError("DATA_ERROR_STOP: incomplete mark return")
    marked = {s: w * (1.0 + returns[s]) for s, w in weights.items()}
    denominator = sum(marked.values())
    if not _finite(denominator) or denominator <= 0 or any(not _finite(v) or v < 0 for v in marked.values()):
        raise PriceAlphaError("DATA_ERROR_STOP: invalid marked portfolio equity")
    return {s: value / denominator for s, value in marked.items()}


def _asset_returns(dataset: PriceDataset, symbols: Sequence[str], start_ms: int,
                   end_ms: int | None) -> tuple[dict[str, float], dict[str, float]]:
    interval, close_marks = {}, {}
    for symbol in symbols:
        current = dataset.bar(symbol, start_ms)
        if not (_finite(current.open) and _finite(current.close) and current.open > 0 and current.close > 0):
            raise PriceAlphaError("DATA_ERROR_STOP: invalid held-symbol bar")
        close_marks[symbol] = current.close / current.open - 1.0
        if end_ms is None:
            interval[symbol] = close_marks[symbol]
        else:
            # A mid-period absent/terminal next open was not frozen as an exit rule.
            next_bar = dataset.bar(symbol, end_ms)
            if not _finite(next_bar.open) or next_bar.open <= 0:
                raise PriceAlphaError("DATA_ERROR_STOP: invalid next open")
            interval[symbol] = next_bar.open / current.open - 1.0
    if any(not _finite(v) or v <= -1 for v in interval.values()):
        raise PriceAlphaError("DATA_ERROR_STOP: invalid held-symbol return")
    return interval, close_marks


@dataclass
class PathResult:
    dates: list[int]
    gross: list[float]
    turnover: list[float]
    weights: list[dict[str, float]]
    decision_weights: list[dict[str, float]]
    symbol_gross: list[dict[str, float]]
    symbol_turnover: list[dict[str, float]]
    rebalanced: list[bool]

    def net(self, stress: bool = False) -> list[float]:
        rate = STRESS_COST if stress else BASE_COST
        return [gross - turn * rate for gross, turn in zip(self.gross, self.turnover)]


def simulate_path(dataset: PriceDataset, start_ms: int, end_ms: int, n: int,
                  mode: str, band: float = 0.20) -> PathResult:
    """Continuous path with real drift and exactly one final close/exit."""
    if mode not in {"banded", "unbanded", "equal"}:
        raise ValueError(mode)
    if start_ms >= end_ms or start_ms % DAY_MS or end_ms % DAY_MS:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid simulation window")
    output = PathResult([], [], [], [], [], [], [], [])
    live: dict[str, float] = {}
    preceding_decision_close: dict[str, float] = {}
    for current in range(start_ms, end_ms, DAY_MS):
        terminal = current == end_ms - DAY_MS
        symbol_turn: dict[str, float] = {}
        did_rebalance = False
        if current == month_start_ms(current):
            decision = current - DAY_MS
            selected = eligible_top_n(dataset, decision, n)
            target = equal_weight(selected) if mode == "equal" else inverse_vol_weights(dataset, selected, decision)
            should_trade = mode != "banded" or band_rebalance(preceding_decision_close, target, band)
            if should_trade:
                for symbol in sorted(set(live) | set(target)):
                    amount = abs(target.get(symbol, 0.0) - live.get(symbol, 0.0))
                    if amount > 0:
                        symbol_turn[symbol] = amount
                live = dict(target)
                did_rebalance = True
        post_trade = dict(live)
        interval, close_marks = _asset_returns(dataset, tuple(sorted(post_trade)), current,
                                                None if terminal else current + DAY_MS)
        symbol_gross = {s: post_trade[s] * interval[s] for s in post_trade}
        scheduled_decision = not terminal and current + DAY_MS == month_start_ms(current + DAY_MS)
        close_weights = _mark_weights(post_trade, close_marks) if scheduled_decision and post_trade else {}
        if terminal:
            exit_weights = _mark_weights(post_trade, close_marks) if post_trade else {}
            for symbol, weight in exit_weights.items():
                symbol_turn[symbol] = symbol_turn.get(symbol, 0.0) + abs(weight)
            live = {}
        else:
            live = _mark_weights(post_trade, interval) if post_trade else {}
            preceding_decision_close = close_weights if scheduled_decision else {}
        output.dates.append(current)
        output.gross.append(sum(symbol_gross.values()))
        output.turnover.append(sum(symbol_turn.values()))
        output.weights.append(post_trade)
        output.decision_weights.append(close_weights)
        output.symbol_gross.append(symbol_gross)
        output.symbol_turnover.append(symbol_turn)
        output.rebalanced.append(did_rebalance)
    if not output.dates:
        raise PriceAlphaError("DATA_ERROR_STOP: empty simulation")
    return output


def _slice(path: PathResult, indices: Sequence[int]) -> PathResult:
    return PathResult(
        [path.dates[i] for i in indices], [path.gross[i] for i in indices],
        [path.turnover[i] for i in indices], [path.weights[i] for i in indices],
        [path.decision_weights[i] for i in indices],
        [path.symbol_gross[i] for i in indices],
        [path.symbol_turnover[i] for i in indices], [path.rebalanced[i] for i in indices],
    )


def path_report(path: PathResult) -> dict[str, object]:
    baseline_contribution: dict[str, float] = {}
    stress_contribution: dict[str, float] = {}
    for gross_row, turn_row in zip(path.symbol_gross, path.symbol_turnover):
        for symbol in set(gross_row) | set(turn_row):
            gross, turn = gross_row.get(symbol, 0.0), turn_row.get(symbol, 0.0)
            baseline_contribution[symbol] = baseline_contribution.get(symbol, 0.0) + gross - turn * BASE_COST
            stress_contribution[symbol] = stress_contribution.get(symbol, 0.0) + gross - turn * STRESS_COST
    observed = [abs(weight) for row in path.weights + path.decision_weights for weight in row.values()]
    counts = [len(row) for row in path.weights if row]
    return {
        "metrics": {"baseline": metrics(path.net(), path.turnover),
                    "stress": metrics(path.net(True), path.turnover)},
        "turnover_total": sum(path.turnover), "rebalances": sum(path.rebalanced),
        "symbol_net_contribution": {"baseline": baseline_contribution,
                                    "stress": stress_contribution},
        "max_observed_weight": max(observed, default=0.0),
        "minimum_names": min(counts, default=0),
    }


def _segment(path: PathResult, split: Split) -> dict[str, object]:
    indices = [i for i, value in enumerate(path.dates) if split.start_ms <= value < split.end_ms]
    if not indices:
        return {"metrics": {"baseline": None, "stress": None}, "turnover_total": 0.0,
                "rebalances": 0, "symbol_net_contribution": {"baseline": {}, "stress": {}},
                "max_observed_weight": 0.0, "minimum_names": 0}
    return path_report(_slice(path, indices))


def _valid_metric_keys(value: object, keys: Sequence[str]) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(value.get(key), (int, float)) and _finite(float(value[key])) for key in keys
    )


def _invalid_metric_failures(result: Mapping[str, object],
                             required: Mapping[str, Sequence[str]]) -> list[str]:
    failures = []
    for name, keys in required.items():
        value = result.get(name)
        for key in keys:
            if not (isinstance(value, Mapping)
                    and isinstance(value.get(key), (int, float))
                    and _finite(float(value[key]))):
                failures.append(f"{name}_{key}_invalid")
    return failures


def _turnover_ratio(primary: Mapping[str, object], unbanded: Mapping[str, object]) -> float:
    numerator, denominator = primary.get("turnover_total"), unbanded.get("turnover_total")
    if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
        return math.inf
    if not _finite(float(numerator)) or not _finite(float(denominator)) or denominator <= 0:
        return math.inf
    return float(numerator) / float(denominator)


def _comparison(primary: Mapping[str, object], equal: Mapping[str, object],
                unbanded: Mapping[str, object]) -> dict[str, object]:
    return {
        "primary": primary["metrics"]["baseline"],
        "equal_weight": equal["metrics"]["baseline"],
        "unbanded": unbanded["metrics"]["baseline"],
        "stress": primary["metrics"]["stress"],
        "equal_weight_stress": equal["metrics"]["stress"],
        "unbanded_stress": unbanded["metrics"]["stress"],
        "turnover_ratio": _turnover_ratio(primary, unbanded),
    }


def _primary_failures(result: Mapping[str, object]) -> list[str]:
    required = {
        "primary": ("total_return", "sharpe", "max_drawdown", "calmar"),
        "equal_weight": ("sharpe", "max_drawdown", "calmar"),
        "unbanded": ("sharpe",),
        "stress": ("total_return", "sharpe"),
        "equal_weight_stress": ("sharpe",),
    }
    invalid = _invalid_metric_failures(result, required)
    if invalid:
        return invalid
    p, eq, ub = result["primary"], result["equal_weight"], result["unbanded"]
    ps, es = result["stress"], result["equal_weight_stress"]
    ratio = result.get("turnover_ratio", math.inf)
    checks = {
        "baseline_total_return_positive": p["total_return"] > 0,
        "baseline_sharpe_positive": p["sharpe"] > 0,
        "baseline_sharpe_margin_over_equal_weight": p["sharpe"] >= eq["sharpe"] + 0.1,
        "baseline_calmar_gte_equal_weight": p["calmar"] >= eq["calmar"],
        "baseline_sharpe_retention_vs_unbanded": p["sharpe"] >= 0.9 * ub["sharpe"],
        "max_drawdown_no_worse_than_equal_weight": p["max_drawdown"] >= eq["max_drawdown"],
        "stress_total_return_positive": ps["total_return"] > 0,
        "stress_sharpe_positive": ps["sharpe"] > 0,
        "stress_sharpe_gte_equal_weight": ps["sharpe"] >= es["sharpe"],
        "unbanded_baseline_sharpe_positive": ub["sharpe"] > 0,
        "turnover_ratio_vs_unbanded": isinstance(ratio, (int, float)) and _finite(float(ratio)) and ratio <= 0.75,
    }
    return [name for name, passed in checks.items() if not passed]


def primary_gate(result: Mapping[str, object]) -> bool:
    return not _primary_failures(result)


def _train_failures(result: Mapping[str, object]) -> list[str]:
    invalid = _invalid_metric_failures(result, {
        "primary": ("total_return", "sharpe", "calmar"),
        "equal_weight": ("sharpe", "calmar"),
    })
    if invalid:
        return invalid
    p, eq = result["primary"], result["equal_weight"]
    ratio = result.get("turnover_ratio", math.inf)
    checks = {
        "baseline_total_return_positive": p["total_return"] > 0,
        "baseline_sharpe_positive": p["sharpe"] > 0,
        "baseline_sharpe_gte_equal_weight": p["sharpe"] >= eq["sharpe"],
        "baseline_calmar_gte_equal_weight": p["calmar"] >= eq["calmar"],
        "turnover_ratio_vs_unbanded": isinstance(ratio, (int, float)) and _finite(float(ratio)) and ratio <= 0.75,
    }
    return [name for name, passed in checks.items() if not passed]


def _stability_failures(result: Mapping[str, object]) -> list[str]:
    required = {"primary": ("total_return", "sharpe"), "equal_weight": ("sharpe",),
                "stress": ("total_return", "sharpe"), "equal_weight_stress": ("sharpe",)}
    invalid = _invalid_metric_failures(result, required)
    if invalid:
        return invalid
    p, eq, ps, es = (result[name] for name in required)
    ratio = result.get("turnover_ratio", math.inf)
    checks = {
        "baseline_total_return_positive": p["total_return"] > 0,
        "baseline_sharpe_positive": p["sharpe"] > 0,
        "baseline_sharpe_gt_equal_weight": p["sharpe"] > eq["sharpe"],
        "stress_total_return_positive": ps["total_return"] > 0,
        "stress_sharpe_positive": ps["sharpe"] > 0,
        "stress_sharpe_gte_equal_weight": ps["sharpe"] >= es["sharpe"],
        "turnover_ratio_vs_unbanded": isinstance(ratio, (int, float)) and _finite(float(ratio)) and ratio <= 0.75,
    }
    return [name for name, passed in checks.items() if not passed]


def grid_gate(points: Sequence[Mapping[str, object]], minimum: int = 3) -> bool:
    return sum(not _stability_failures(point) for point in points) >= minimum


def concentration_gate(contributions: Mapping[str, float], max_observed_weight: float,
                       observed_minimum_names: int, *, max_share: float = 0.25,
                       max_weight: float = 0.20, required_minimum_names: int = 30) -> bool:
    return not _concentration_failures(
        contributions, max_observed_weight, observed_minimum_names,
        max_share=max_share, max_weight=max_weight,
        required_minimum_names=required_minimum_names,
    )


def _concentration_failures(contributions: Mapping[str, float], max_observed_weight: float,
                            observed_minimum_names: int, *, max_share: float = 0.25,
                            max_weight: float = 0.20,
                            required_minimum_names: int = 30) -> list[str]:
    values = list(contributions.values())
    if not values or any(not _finite(value) for value in values):
        return ["symbol_contribution_invalid"]
    denominator = sum(abs(value) for value in values)
    if not _finite(denominator) or denominator <= 0:
        return ["symbol_contribution_denominator_invalid"]
    failures = []
    if max(abs(value) for value in values) / denominator > max_share:
        failures.append("largest_absolute_symbol_pnl_share")
    if not _finite(max_observed_weight) or max_observed_weight > max_weight:
        failures.append("max_observed_weight")
    if observed_minimum_names < required_minimum_names:
        failures.append("primary_minimum_names")
    return failures


def half_year_gate(reports: Sequence[Mapping[str, object]], minimum: int = 3) -> bool:
    return not _half_year_failures(reports, minimum)


def _half_year_failures(reports: Sequence[Mapping[str, object]], minimum: int = 3) -> list[str]:
    sharpe_count = calmar_count = 0
    for report in reports:
        primary, equal = report.get("primary"), report.get("equal_weight")
        if not (_valid_metric_keys(primary, ("sharpe", "calmar"))
                and _valid_metric_keys(equal, ("sharpe", "calmar"))):
            continue
        sharpe_count += primary["sharpe"] >= equal["sharpe"]
        calmar_count += primary["calmar"] >= equal["calmar"]
    failures = []
    if sharpe_count < minimum:
        failures.append("primary_baseline_sharpe_gte_equal_weight_min_count")
    if calmar_count < minimum:
        failures.append("primary_baseline_calmar_gte_equal_weight_min_count")
    return failures


def _gate_record(failures: Sequence[str]) -> dict[str, object]:
    return {"passed": not failures, "failed_checks": list(failures)}


def compose_gates(reports: Mapping[str, object],
                  half_year_reports: Mapping[str, object]) -> dict[str, object]:
    """Compose all and only the frozen gates, with nonnumeric failure names."""
    primary_key = "n30_band20"
    primary, equal, unbanded = (reports["banded"][primary_key],
                                reports["equal_weight"]["n30"],
                                reports["unbanded"]["n30"])
    train = _comparison(primary["train"], equal["train"], unbanded["train"])
    oos = _comparison(primary["oos"], equal["oos"], unbanded["oos"])
    holdout = _comparison(primary["holdout"], equal["holdout"], unbanded["holdout"])

    concentration_failures = []
    for split_name in ("oos", "holdout"):
        segment = primary[split_name]
        concentration_failures.extend(
            f"{split_name}_{failure}" for failure in _concentration_failures(
                segment["symbol_net_contribution"]["baseline"],
                segment["max_observed_weight"], segment["minimum_names"]
            )
        )

    temporal_comparisons, temporal_diagnostics = [], {}
    for split in HALF_YEARS:
        comparison = _comparison(
            half_year_reports["banded"][primary_key][split.name],
            half_year_reports["equal_weight"]["n30"][split.name],
            half_year_reports["unbanded"]["n30"][split.name],
        )
        temporal_comparisons.append(comparison)
        temporal_diagnostics[split.name] = comparison
    temporal_failures = _half_year_failures(temporal_comparisons)

    stability_points, passed_points = {}, 0
    for band, n in GRID:
        key, n_key = f"n{n}_band{int(round(band * 100))}", f"n{n}"
        failures = []
        for split_name in ("oos", "holdout"):
            comparison = _comparison(reports["banded"][key][split_name],
                                     reports["equal_weight"][n_key][split_name],
                                     reports["unbanded"][n_key][split_name])
            failures.extend(f"{split_name}_{failure}"
                            for failure in _stability_failures(comparison))
        passed = not failures
        passed_points += int(passed)
        stability_points[key] = {"passed": passed, "failed_checks": failures}
    stability_failures = [] if passed_points >= 3 else ["three_of_four_grid_points"]

    gates: dict[str, object] = {
        "train_sanity": _gate_record(_train_failures(train)),
        "oos_primary": _gate_record(_primary_failures(oos)),
        "final_holdout_primary": _gate_record(_primary_failures(holdout)),
        "concentration": _gate_record(concentration_failures),
        "multi_period": _gate_record(temporal_failures),
        "parameter_stability": {**_gate_record(stability_failures),
                                "points": stability_points, "passed_points": passed_points,
                                "required_points": 3},
        "regime_diagnostics": {
            "type": "calendar_half_year_temporal_diagnostics",
            "new_volatility_threshold_or_gate_added": False,
            "used_by_frozen_multi_period_gate_only": True,
            "periods": temporal_diagnostics,
        },
    }
    required = ("train_sanity", "oos_primary", "final_holdout_primary",
                "concentration", "multi_period", "parameter_stability")
    gates["all_required"] = all(gates[name]["passed"] for name in required)
    return gates


def verify_preregistration(path: pathlib.Path = PREREG_PATH) -> str:
    try:
        digest = sha256_file(path)
    except OSError as error:
        raise PriceAlphaError(f"DATA_ERROR_STOP: preregistration unreadable: {error}") from error
    if digest != PREREG_SHA256:
        raise PriceAlphaError("DATA_ERROR_STOP: preregistration SHA differs")
    return digest


def _git_output(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise PriceAlphaError(f"DATA_ERROR_STOP: git binding failed: {error}") from error


def verify_clean_research_commit() -> str:
    """Formal computation may use only a committed tree descended from the freeze."""
    _git_output("ls-files", "--error-unmatch", "research/exploration/expl_013.py")
    _git_output("ls-files", "--error-unmatch", "research/exploration/expl-013-preregistration.json")
    dirty = _git_output("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PriceAlphaError("DATA_ERROR_STOP: tracked worktree differs from code commit")
    commit = _git_output("rev-parse", "HEAD")
    if len(commit) != 40:
        raise PriceAlphaError("DATA_ERROR_STOP: invalid code commit SHA")
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", FREEZE_COMMIT, commit],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PriceAlphaError(
            "DATA_ERROR_STOP: code commit is not descended from frozen contract"
        ) from error
    return commit


def dataset_binding(dataset: PriceDataset) -> dict[str, object]:
    if not isinstance(dataset, PriceDataset):
        raise PriceAlphaError("DATA_ERROR_STOP: dataset was not produced by verified loader")
    manifest, pit = getattr(dataset, "manifest_sha256", None), getattr(dataset, "pit_sha256", None)
    labels, artifact = tuple(getattr(dataset, "labels", ())), getattr(dataset, "artifact_path", None)
    if manifest != MANIFEST_SHA256 or pit != PIT_SHA256:
        raise PriceAlphaError("DATA_ERROR_STOP: loaded dataset SHA binding differs")
    if set(labels) != {"archive-extended", "survivor-biased", "exploration-only"}:
        raise PriceAlphaError("DATA_ERROR_STOP: loaded dataset labels differ")
    if not isinstance(artifact, pathlib.Path):
        raise PriceAlphaError("DATA_ERROR_STOP: loaded artifact path absent")
    return {"dataset_id": DATASET, "snapshot_id": SNAPSHOT_ID,
            "manifest_sha256": manifest, "pit_sha256": pit,
            "artifact_path": str(artifact), "labels": list(labels),
            "integrity": "VERIFIED", "quality": "PASS"}


def evaluate_experiment(dataset: PriceDataset, *, start_ms: int = SPLITS[0].start_ms,
                        end_ms: int = SPLITS[-1].end_ms) -> dict[str, object]:
    """Run the one frozen experiment after immutable bindings pass."""
    prereg_digest = verify_preregistration()
    code_commit = verify_clean_research_commit()
    binding = dataset_binding(dataset)
    paths: dict[str, dict[str, PathResult]] = {"banded": {}, "unbanded": {}, "equal_weight": {}}
    for n in (10, 30):
        paths["unbanded"][f"n{n}"] = simulate_path(dataset, start_ms, end_ms, n, "unbanded")
        paths["equal_weight"][f"n{n}"] = simulate_path(dataset, start_ms, end_ms, n, "equal")
    for band, n in GRID:
        key = f"n{n}_band{int(round(band * 100))}"
        paths["banded"][key] = simulate_path(dataset, start_ms, end_ms, n, "banded", band)

    reports, half_reports = {}, {}
    for mode, variants in paths.items():
        reports[mode], half_reports[mode] = {}, {}
        for key, path in variants.items():
            reports[mode][key] = {split.name: _segment(path, split) for split in SPLITS}
            half_reports[mode][key] = {split.name: _segment(path, split) for split in HALF_YEARS}
    gates = compose_gates(reports, half_reports)
    return {
        "experiment_id": "EXPL-013", "artifact_class": "EXPLORATION_ONLY_NOT_EVIDENCE",
        "outcome": "EXPLORATION_PASS" if gates["all_required"] else "FAIL_AND_STOP_PRICE_ALPHA",
        "freeze_commit": FREEZE_COMMIT, "prereg_sha256": prereg_digest,
        "code_commit_sha": code_commit, "runner_sha256": sha256_file(pathlib.Path(__file__)),
        "dataset": binding, "funding_modeled": False, "paths": reports,
        "half_year_diagnostics": half_reports, "gates": gates,
    }


def write_result(path: pathlib.Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def write_report(payload: Mapping[str, object], directory: pathlib.Path) -> pathlib.Path:
    path = directory / OUTPUT_NAME
    if path.exists():
        raise PriceAlphaError("DATA_ERROR_STOP: EXPL-013 report already exists; refusing overwrite")
    write_result(path, payload)
    return path


def main() -> int:
    try:
        payload = evaluate_experiment(load_dataset())
        write_report(payload, ROOT / "research" / "exploration")
    except (PriceAlphaError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
