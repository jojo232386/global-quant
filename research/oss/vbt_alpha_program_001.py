#!/usr/bin/env python3
"""Frozen Tier-1 candidate calculations for VBT Alpha Program 001."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.exploration.price_alpha_v1 import DAY_MS, Bar, PriceDataset
from research.oss.vectorbt_pit_baseline import (
    BaselineInputs,
    POCDataError,
    _bar,
    _iso,
    _master_symbols,
    _terminals,
    _timestamp,
    _universe,
    build_portfolio,
    load_frozen_inputs,
    vbt,
)


PROGRAM_ID = "VBT_ALPHA_PROGRAM_001"
BASE_FEE = 0.0005
BASE_SLIPPAGE = 0.001
STRESS_FEE = 0.0005
STRESS_SLIPPAGE = 0.0025


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    signal_kind: str
    primary_lookback: int
    neighbor_lookback: int
    rebalance_days: int
    first_execution: str
    final_execution: str
    final_exit: str


@dataclass(frozen=True)
class CandidateInputs:
    spec: CandidateSpec
    lookback: int
    inputs: BaselineInputs
    schedule: tuple[int, ...]
    rank_ics: tuple[float, ...]
    input_sha256: str


SPECS = {
    "1": CandidateSpec(
        "CAND-VBT-RANGE-VOLUME-ACCEPTANCE-001",
        "range_volume_acceptance",
        20,
        30,
        5,
        "2021-02-08T00:00:00.000Z",
        "2023-11-05T00:00:00.000Z",
        "2023-11-10T00:00:00.000Z",
    ),
    "2": CandidateSpec(
        "CAND-VBT-CORRELATION-CROWDING-001",
        "correlation_crowding",
        20,
        30,
        7,
        "2021-02-08T00:00:00.000Z",
        "2023-11-06T00:00:00.000Z",
        "2023-11-13T00:00:00.000Z",
    ),
}


def range_volume_signal(
    dataset: PriceDataset,
    ranges: Mapping[str, Mapping[int, tuple[float, float]]],
    members: tuple[str, ...],
    decision: int,
    lookback: int,
) -> dict[str, float]:
    output: dict[str, float] = {}
    for symbol in members:
        bar = _bar(dataset, symbol, decision)
        try:
            high, low = ranges[symbol][decision]
        except KeyError as error:
            raise POCDataError(f"DATA_ERROR_STOP: missing range {symbol}:{_iso(decision)}") from error
        if high <= low or bar.quote_volume <= 0:
            raise POCDataError(f"DATA_ERROR_STOP: invalid range/volume {symbol}:{_iso(decision)}")
        volumes = [
            _bar(dataset, symbol, decision - offset * DAY_MS).quote_volume
            for offset in range(1, lookback + 1)
        ]
        if any(not math.isfinite(value) or value <= 0 for value in volumes):
            raise POCDataError(f"DATA_ERROR_STOP: invalid prior volume {symbol}:{_iso(decision)}")
        clv = (2.0 * bar.close - high - low) / (high - low)
        output[symbol] = clv * math.log(bar.quote_volume / statistics.median(volumes))
    return output


def correlation_crowding_signal(
    dataset: PriceDataset, members: tuple[str, ...], decision: int, lookback: int
) -> dict[str, float]:
    returns = np.empty((lookback, len(members)), dtype=float)
    for column, symbol in enumerate(members):
        for row, offset in enumerate(range(lookback - 1, -1, -1)):
            current = _bar(dataset, symbol, decision - offset * DAY_MS).close
            previous = _bar(dataset, symbol, decision - (offset + 1) * DAY_MS).close
            returns[row, column] = current / previous - 1.0
    if not np.isfinite(returns).all():
        raise POCDataError(f"DATA_ERROR_STOP: nonfinite correlation input {_iso(decision)}")
    market = returns.mean(axis=1)
    if np.var(market, ddof=1) <= 0:
        raise POCDataError(f"DATA_ERROR_STOP: zero market variance {_iso(decision)}")
    output: dict[str, float] = {}
    for column, symbol in enumerate(members):
        series = returns[:, column]
        if np.var(series, ddof=1) <= 0:
            raise POCDataError(f"DATA_ERROR_STOP: zero symbol variance {symbol}:{_iso(decision)}")
        value = float(np.corrcoef(series, market)[0, 1])
        if not math.isfinite(value):
            raise POCDataError(f"DATA_ERROR_STOP: invalid correlation {symbol}:{_iso(decision)}")
        output[symbol] = -value
    return output


def _signal(
    spec: CandidateSpec,
    dataset: PriceDataset,
    members: tuple[str, ...],
    decision: int,
    lookback: int,
    ranges: Mapping[str, Mapping[int, tuple[float, float]]] | None,
) -> dict[str, float]:
    if spec.signal_kind == "range_volume_acceptance":
        if ranges is None:
            raise POCDataError("DATA_ERROR_STOP: range overlay missing")
        return range_volume_signal(dataset, ranges, members, decision, lookback)
    return correlation_crowding_signal(dataset, members, decision, lookback)


def load_range_overlay(
    dataset: PriceDataset, symbols: tuple[str, ...]
) -> dict[str, dict[int, tuple[float, float]]]:
    """Expose high/low from the same manifest-bound rows without altering the frozen loader."""
    output: dict[str, dict[int, tuple[float, float]]] = {}
    for symbol in symbols:
        path = dataset.artifact_path / "data" / f"validated-{symbol}.jsonl"
        points: dict[int, tuple[float, float]] = {}
        try:
            handle = path.open(encoding="utf-8")
        except OSError as error:
            raise POCDataError(f"DATA_ERROR_STOP: range overlay unreadable {symbol}") from error
        with handle:
            for line in handle:
                row = json.loads(line)
                timestamp = int(row["open_time_utc_ms"])
                opened, high, low, closed, volume = (
                    float(row[name])
                    for name in ("open", "high", "low", "close", "quote_volume")
                )
                base = _bar(dataset, symbol, timestamp)
                if (
                    timestamp in points
                    or not all(math.isfinite(value) for value in (opened, high, low, closed, volume))
                    or min(opened, high, low, closed) <= 0
                    or volume < 0
                    or high < max(opened, closed)
                    or low > min(opened, closed)
                    or (opened, closed, volume) != (base.open, base.close, base.quote_volume)
                ):
                    raise POCDataError(f"DATA_ERROR_STOP: invalid range overlay {symbol}:{timestamp}")
                points[timestamp] = (high, low)
        if set(points) != set(dataset.bars[symbol]):
            raise POCDataError(f"DATA_ERROR_STOP: range overlay coverage differs {symbol}")
        output[symbol] = points
    return output


def _weights(signal: Mapping[str, float]) -> dict[str, float]:
    if len(signal) < 20 or any(not math.isfinite(value) for value in signal.values()):
        raise POCDataError("DATA_ERROR_STOP: fewer than 20 finite signals")
    ordered = sorted(signal, key=lambda symbol: (signal[symbol], symbol))
    count = max(1, math.floor(len(ordered) * 0.20))
    output = {symbol: -0.5 / count for symbol in ordered[:count]}
    output.update({symbol: 0.5 / count for symbol in ordered[-count:]})
    return output


def _rank_ic(signal: Mapping[str, float], forward: Mapping[str, float]) -> float:
    symbols = tuple(sorted(set(signal) & set(forward)))
    if len(symbols) < 20:
        raise POCDataError("DATA_ERROR_STOP: fewer than 20 forward outcomes for rank IC")
    x = pd.Series({symbol: signal[symbol] for symbol in symbols}).rank(method="average")
    y = pd.Series({symbol: forward[symbol] for symbol in symbols}).rank(method="average")
    value = float(x.corr(y))
    if not math.isfinite(value):
        raise POCDataError("DATA_ERROR_STOP: invalid rank IC")
    return value


def _forward_return(
    dataset: PriceDataset,
    terminals: Mapping[str, int],
    symbol: str,
    execution: int,
    end: int,
) -> float:
    opened = _bar(dataset, symbol, execution).open
    terminal = terminals.get(symbol)
    if terminal is not None and execution < terminal < end:
        terminal_day = terminal // DAY_MS * DAY_MS
        return _bar(dataset, symbol, terminal_day).close / opened - 1.0
    return _bar(dataset, symbol, end).open / opened - 1.0


def _canonical_matrix(frame: pd.DataFrame) -> list[list[float | str]]:
    return [
        ["NA" if pd.isna(value) else float(value) for value in row]
        for row in frame.to_numpy()
    ]


def build_candidate_inputs(
    dataset: PriceDataset,
    master: Mapping[str, Any],
    spec: CandidateSpec,
    lookback: int,
    ranges: Mapping[str, Mapping[int, tuple[float, float]]] | None = None,
) -> CandidateInputs:
    if sys.modules.get("pandas") is not pd:
        sys.modules["pandas"] = pd
    symbols = _master_symbols(master)
    first, final_execution, final_exit = map(
        _timestamp, (spec.first_execution, spec.final_execution, spec.final_exit)
    )
    step = spec.rebalance_days * DAY_MS
    if (final_execution - first) % step or final_exit != final_execution + step:
        raise POCDataError("DATA_ERROR_STOP: frozen candidate schedule differs")
    schedule = tuple(range(first, final_execution + DAY_MS, step))
    base_dates = tuple(range(first, final_exit + DAY_MS, DAY_MS))
    terminals = _terminals(master)
    terminal_rows = tuple(value for value in terminals.values() if first <= value < final_exit)
    dates = tuple(sorted(set(base_dates) | set(terminal_rows)))
    index = pd.to_datetime(dates, unit="ms", utc=True)
    close = pd.DataFrame(np.nan, index=index, columns=symbols)
    valuation, size, price = close.copy(), close.copy(), close.copy()
    rank_ics: list[float] = []

    for current in base_dates:
        row = pd.to_datetime(current, unit="ms", utc=True)
        for symbol in symbols:
            terminal = terminals.get(symbol)
            if terminal is not None and current > terminal // DAY_MS * DAY_MS:
                continue
            closed = _bar(dataset, symbol, current).close
            close.loc[row, symbol] = closed
            valuation.loc[row, symbol] = closed

        if current in schedule:
            decision = current - DAY_MS
            decision_members = _universe(master, decision)
            signals = _signal(spec, dataset, decision_members, decision, lookback, ranges)
            targets = _weights(signals)
            execution_members = set(_universe(master, current))
            size.loc[row, :] = 0.0
            forward: dict[str, float] = {}
            for symbol in decision_members:
                if symbol not in execution_members:
                    terminal = terminals.get(symbol)
                    if terminal is None or terminal > current:
                        raise POCDataError(f"DATA_ERROR_STOP: unexplained execution removal {symbol}")
                    continue
                valuation.loc[row, symbol] = _bar(dataset, symbol, decision).close
                price.loc[row, symbol] = _bar(dataset, symbol, current).open
                if symbol in targets:
                    size.loc[row, symbol] = targets[symbol]
                forward[symbol] = _forward_return(
                    dataset, terminals, symbol, current, current + step
                )
            rank_ics.append(_rank_ic(signals, forward))
        elif current == final_exit:
            active = _universe(master, current)
            size.loc[row, :] = 0.0
            for symbol in active:
                price.loc[row, symbol] = _bar(dataset, symbol, current).open
                valuation.loc[row, symbol] = _bar(dataset, symbol, current - DAY_MS).close

    for symbol, terminal in terminals.items():
        if terminal not in terminal_rows:
            continue
        row = pd.to_datetime(terminal, unit="ms", utc=True)
        terminal_day = terminal // DAY_MS * DAY_MS
        for cohort_symbol in symbols:
            cohort_terminal = terminals.get(cohort_symbol)
            if cohort_terminal is not None and cohort_terminal < terminal:
                continue
            closed = _bar(dataset, cohort_symbol, terminal_day).close
            close.loc[row, cohort_symbol] = closed
            valuation.loc[row, cohort_symbol] = closed
        size.loc[row, symbol] = 0.0
        price.loc[row, symbol] = _bar(dataset, symbol, terminal_day).close

    inputs = BaselineInputs(
        dates,
        symbols,
        close,
        valuation,
        size,
        price,
        len(schedule),
        len(terminal_rows),
        int(size.notna().sum().sum()),
    )
    payload = {
        "candidate_id": spec.candidate_id,
        "lookback": lookback,
        "dates": [_iso(value) for value in dates],
        "symbols": list(symbols),
        "size": _canonical_matrix(size),
        "valuation": _canonical_matrix(valuation),
        "price": _canonical_matrix(price),
        "rank_ics": rank_ics,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return CandidateInputs(spec, lookback, inputs, schedule, tuple(rank_ics), fingerprint)


def _daily_returns(portfolio: Any) -> pd.Series:
    raw = portfolio.returns()
    daily = raw.groupby(raw.index.floor("D")).apply(lambda values: (1.0 + values).prod() - 1.0)
    if daily.empty or not np.isfinite(daily.to_numpy()).all():
        raise POCDataError("DATA_ERROR_STOP: invalid portfolio returns")
    return daily


def _metrics(portfolio: Any) -> tuple[dict[str, float], pd.Series]:
    daily = _daily_returns(portfolio)
    total = float((1.0 + daily).prod() - 1.0)
    mean = float(daily.mean())
    volatility = float(daily.std(ddof=1))
    sharpe = mean / volatility * math.sqrt(365.0) if volatility > 0 else float("-inf")
    wealth = (1.0 + daily).cumprod()
    drawdown = float((wealth / wealth.cummax() - 1.0).min())
    return {
        "total_return": total,
        "mean_daily_return": mean,
        "annualized_sharpe": sharpe,
        "max_drawdown": drawdown,
    }, daily


def _hac(ics: tuple[float, ...], lag: int = 3) -> dict[str, float]:
    values = np.asarray(ics, dtype=float)
    mean = float(values.mean())
    centered = values - mean
    n = len(values)
    long_run = float(np.dot(centered, centered) / n)
    for offset in range(1, min(lag, n - 1) + 1):
        covariance = float(np.dot(centered[offset:], centered[:-offset]) / n)
        long_run += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    standard_error = math.sqrt(max(long_run, 0.0) / n)
    t_stat = mean / standard_error if standard_error > 0 else math.copysign(float("inf"), mean)
    p_value = 0.5 * math.erfc(t_stat / math.sqrt(2.0))
    return {"mean_rank_ic": mean, "hac_lag3_t": t_stat, "one_sided_normal_p": p_value}


def _turnover(portfolio: Any, built: CandidateInputs) -> dict[str, Any]:
    assets = portfolio.assets()
    cash = portfolio.cash(group_by=True)
    observations: list[float] = []
    for timestamp in built.schedule:
        row = pd.to_datetime(timestamp, unit="ms", utc=True)
        location = built.inputs.size.index.get_loc(row)
        if location == 0:
            previous_assets = pd.Series(0.0, index=built.inputs.symbols)
            previous_cash = 1.0
        else:
            previous_assets = assets.iloc[location - 1]
            previous_cash = float(cash.iloc[location - 1])
        marks = built.inputs.valuation.loc[row]
        values = previous_assets * marks
        if ((previous_assets != 0) & marks.isna()).any():
            raise POCDataError("DATA_ERROR_STOP: missing drifted incumbent valuation")
        values = values.fillna(0.0)
        pre_value = previous_cash + float(values.sum())
        if not math.isfinite(pre_value) or pre_value <= 0:
            raise POCDataError("DATA_ERROR_STOP: invalid pre-order portfolio value")
        incumbent = values / pre_value
        target = built.inputs.size.loc[row]
        observations.append(float((target - incumbent).abs().sum()))
    return {
        "median_one_way_turnover": float(statistics.median(observations)),
        "scheduled_observations": len(observations),
    }


def _contributions(portfolio: Any) -> tuple[dict[str, Any], str | None]:
    trades = portfolio.trades.records_readable
    if not trades.empty and set(trades["Status"]) != {"Closed"}:
        raise POCDataError("DATA_ERROR_STOP: open trade remains after final exit")
    values = trades.groupby("Column")["PnL"].sum().to_dict()
    denominator = sum(abs(float(value)) for value in values.values())
    if denominator <= 0:
        return {"top_contributor": None, "top_absolute_share": 1.0}, None
    top = max(values, key=lambda symbol: (abs(float(values[symbol])), symbol))
    positive = [symbol for symbol, value in values.items() if float(value) > 0]
    removed = max(positive, key=lambda symbol: (float(values[symbol]), symbol)) if positive else None
    return {
        "top_contributor": top,
        "top_absolute_share": abs(float(values[top])) / denominator,
        "largest_positive_contributor": removed,
    }, removed


def _without_symbol(inputs: BaselineInputs, symbol: str) -> BaselineInputs:
    size = inputs.size.copy()
    mask = size[symbol].notna()
    size.loc[mask, symbol] = 0.0
    return dataclasses.replace(inputs, size=size)


def _subperiods(daily: pd.Series) -> dict[str, float]:
    return {
        str(year): float((1.0 + daily[daily.index.year == year]).prod() - 1.0)
        for year in (2021, 2022, 2023)
    }


def _post_terminal_fill_count(portfolio: Any, master: Mapping[str, Any]) -> int:
    count = 0
    for symbol, terminal in _terminals(master).items():
        timestamp = pd.to_datetime(terminal, unit="ms", utc=True)
        count += int(portfolio.close.loc[portfolio.close.index > timestamp, symbol].notna().sum())
    return count


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(current) for key, current in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(current) for current in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)):
            return None
        return round(float(value), 12)
    if isinstance(value, (int, np.integer)):
        return int(value)
    return value


def run_candidate(candidate: str) -> dict[str, Any]:
    spec = SPECS[candidate]
    dataset, master = load_frozen_inputs()
    ranges = (
        load_range_overlay(dataset, _master_symbols(master))
        if spec.signal_kind == "range_volume_acceptance"
        else None
    )
    primary = build_candidate_inputs(dataset, master, spec, spec.primary_lookback, ranges)
    neighbor = build_candidate_inputs(dataset, master, spec, spec.neighbor_lookback, ranges)
    gross = build_portfolio(primary.inputs, fee_rate=0.0, one_side_slippage=0.0)
    net = build_portfolio(primary.inputs, fee_rate=BASE_FEE, one_side_slippage=BASE_SLIPPAGE)
    stress = build_portfolio(primary.inputs, fee_rate=STRESS_FEE, one_side_slippage=STRESS_SLIPPAGE)
    neighbor_stress = build_portfolio(
        neighbor.inputs, fee_rate=STRESS_FEE, one_side_slippage=STRESS_SLIPPAGE
    )
    gross_metrics, _ = _metrics(gross)
    net_metrics, _ = _metrics(net)
    stress_metrics, stress_daily = _metrics(stress)
    neighbor_metrics, _ = _metrics(neighbor_stress)
    predictive = _hac(primary.rank_ics)
    neighbor_predictive = _hac(neighbor.rank_ics)
    turnover = _turnover(gross, primary)
    concentration, removed_symbol = _contributions(stress)
    if removed_symbol is None:
        removal_mean = None
    else:
        removal = build_portfolio(
            _without_symbol(primary.inputs, removed_symbol),
            fee_rate=STRESS_FEE,
            one_side_slippage=STRESS_SLIPPAGE,
        )
        removal_mean = _metrics(removal)[0]["mean_daily_return"]
    subperiods = _subperiods(stress_daily)
    fill_count = _post_terminal_fill_count(stress, master)
    gates = {
        "stress_total_return_positive": stress_metrics["total_return"] > 0,
        "stress_mean_daily_positive": stress_metrics["mean_daily_return"] > 0,
        "stress_sharpe": stress_metrics["annualized_sharpe"] >= 0.50,
        "predictive_direction": predictive["mean_rank_ic"] > 0
        and predictive["one_sided_normal_p"] <= 0.05,
        "drawdown": stress_metrics["max_drawdown"] >= -0.35,
        "turnover": turnover["median_one_way_turnover"] <= 1.25,
        "concentration": concentration["top_absolute_share"] <= 0.20,
        "single_symbol_sensitivity": removal_mean is not None and removal_mean > 0,
        "subperiods": sum(value >= 0 for value in subperiods.values()) >= 2,
        "sanity_neighbor": neighbor_metrics["total_return"] > 0
        and neighbor_metrics["mean_daily_return"] > 0
        and neighbor_predictive["mean_rank_ic"] > 0,
        "observations": len(primary.schedule) >= 100,
        "data_and_lifecycle": fill_count == 0,
    }
    result = {
        "artifact_class": "GMAQ_VBT_ALPHA_PROGRAM_001_RESULT",
        "artifact_version": "1.0.0",
        "program_id": PROGRAM_ID,
        "candidate_id": spec.candidate_id,
        "result": "TIER1_PASS" if all(gates.values()) else "TIER1_FAIL",
        "research_tier": "TIER_1_EXPLORATION",
        "exploration_only": True,
        "alpha_promotion": False,
        "framework": f"vectorbt=={vbt.__version__}",
        "framework_role": "RESEARCH_ONLY",
        "dataset_snapshot_id": "a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b",
        "input_sha256": primary.input_sha256,
        "neighbor_input_sha256": neighbor.input_sha256,
        "schedule": {
            "first_execution": spec.first_execution,
            "final_execution": spec.final_execution,
            "final_exit": spec.final_exit,
            "rebalance_days": spec.rebalance_days,
        },
        "gross": gross_metrics,
        "net": net_metrics,
        "stress_30bps": stress_metrics,
        "predictive": {**predictive, "observations": len(primary.rank_ics)},
        "turnover": turnover,
        "concentration": concentration,
        "single_symbol_removal": {
            "removed_symbol": removed_symbol,
            "stress_mean_daily_return": removal_mean,
        },
        "subperiod_stress_total_returns": subperiods,
        "sanity_neighbor": {
            "lookback": spec.neighbor_lookback,
            "stress": neighbor_metrics,
            "predictive": {**neighbor_predictive, "observations": len(neighbor.rank_ics)},
        },
        "lifecycle": {
            "canonical_terminal_events": primary.inputs.terminal_liquidations,
            "post_terminal_fill_count": fill_count,
        },
        "gates": gates,
        "all_gates_pass": all(gates.values()),
        "funding_not_modeled": True,
        "holdout_used": False,
        "real_order_count": 0,
        "ready_for_strategy": False,
        "ready_for_tiny_live": False,
    }
    return _clean(result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=tuple(SPECS), required=True)
    args = parser.parse_args()
    print(json.dumps(run_candidate(args.candidate), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
