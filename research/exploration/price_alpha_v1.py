#!/usr/bin/env python3
"""Shared, price-only exploration runner for EXPL-001 / 003 / 004.

The comparison contract is frozen in hypothesis-backlog.md.  This module is
deliberately self-contained and standard-library-only: it reads one verified
curated snapshot, builds no database, fetches nothing, and writes only
``expl-*`` JSON artifacts beside itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable, Mapping


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gmaq_data import DataLayerError, verify_snapshot  # noqa: E402


DAY_MS = 86_400_000
ANN = 365.0
DATA_ROOT = ROOT.parent / "gmaq-data"
DATASET = "pre2024-usdm-archive-extended-1d"
SNAPSHOT_ID = "a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b"
MANIFEST_SHA256 = "cd2ae988fac8bca1b4c67d5985d93d3dcc145c7b7c598a9e5a0377c7c49bf166"
PIT_SHA256 = "b006eae7dde9514e656156749d9891edf5fe70c2e12811b0395e15e1b4ef643e"
# Captured in the first successful result artifacts before backlog statuses
# were changed from DRAFT to FAIL.  The live backlog hash is also recorded so
# the post-result negative-memory update remains transparent.
FROZEN_BACKLOG_SHA256 = "1afd9cbd272a663c357eed96a97147025fb5de8825a959c27a7c2152f4fcfae9"
START_MS = int(dt.datetime(2021, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
OOS_START_MS = int(dt.datetime(2022, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
END_MS = int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
FINAL_OPEN_MS = END_MS - DAY_MS
BASE_COST = 0.0015
STRESS_COST = 0.0030
SURCHARGE = {1: 0.0015, 2: 0.0010, 3: 0.0005}
MOMENTUM_GRID = tuple((n, days) for n in (20, 30, 50) for days in (7, 14))
VOL_GRID = tuple((window, months) for window in (14, 30) for months in (1, 2))


class PriceAlphaError(RuntimeError):
    """Fail-closed error raised before any result artifact is accepted."""


@dataclass(frozen=True)
class Bar:
    open: float
    close: float
    quote_volume: float
    high: float | None = None
    low: float | None = None


@dataclass
class PriceDataset:
    bars: dict[str, dict[int, Bar]]
    last_timestamp: dict[str, int]
    pit: dict[int, tuple[str, ...]]
    artifact_path: pathlib.Path
    manifest_sha256: str
    pit_sha256: str
    labels: tuple[str, ...]
    inactive_member_decisions: int = 0

    def universe(self, execution_ms: int) -> tuple[str, ...]:
        month = month_start_ms(execution_ms)
        if month not in self.pit:
            raise PriceAlphaError(f"DATA_ERROR_STOP: PIT record absent at {iso(month)}")
        return self.pit[month]

    def bar(self, symbol: str, timestamp: int) -> Bar:
        try:
            return self.bars[symbol][timestamp]
        except KeyError as error:
            raise PriceAlphaError(
                f"DATA_ERROR_STOP: required {symbol} bar absent at {iso(timestamp)}"
            ) from error

    def close_return(self, symbol: str, decision_ms: int, lookback: int) -> float:
        now = self.bar(symbol, decision_ms).close
        then = self.bar(symbol, decision_ms - lookback * DAY_MS).close
        return now / then - 1.0

    def median_volume(self, symbol: str, decision_ms: int, days: int = 90) -> float:
        values = [
            self.bar(symbol, decision_ms - offset * DAY_MS).quote_volume
            for offset in range(days)
        ]
        return statistics.median(values)


@dataclass
class Event:
    execution_ms: int
    decision_ms: int
    weights: dict[str, float]
    signals: dict[str, float]
    forward_returns: dict[str, float]
    volume_deciles: dict[str, int] = field(default_factory=dict)
    gate_active: bool = True
    gate_statistic: float | None = None
    gate_threshold: float | None = None
    inactive_members: int = 0


@dataclass
class Simulation:
    dates: list[int]
    gross: list[float]
    turnover: list[float]
    liquidity_cost: list[float]
    trade_count: list[int]
    rebalance_count: list[int]
    symbol_gross: list[dict[str, float]]
    symbol_turnover: list[dict[str, float]]
    symbol_liquidity_cost: list[dict[str, float]]
    terminal_liquidations: list[dict[str, object]]


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC).date().isoformat()


def month_start_ms(timestamp: int) -> int:
    value = dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC)
    return int(value.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def add_months_ms(timestamp: int, months: int) -> int:
    value = dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC)
    index = value.year * 12 + value.month - 1 + months
    return int(dt.datetime(index // 12, index % 12 + 1, 1, tzinfo=dt.UTC).timestamp() * 1000)


def load_dataset(data_root: pathlib.Path = DATA_ROOT) -> PriceDataset:
    try:
        record = verify_snapshot(
            data_root, SNAPSHOT_ID, expected_dataset=DATASET, minimum_stage="curated"
        )
    except (DataLayerError, OSError, ValueError) as error:
        raise PriceAlphaError(f"DATA_ERROR_STOP: registry verification failed: {error}") from error
    if record["integrity_verdict"] != "VERIFIED" or record["quality_verdict"] != "PASS":
        raise PriceAlphaError("DATA_ERROR_STOP: curated snapshot is not VERIFIED/PASS")
    artifact = pathlib.Path(record["artifact_path"])
    manifest = artifact / "snapshot.manifest.json"
    pit_path = artifact / "data/pit-universe.jsonl"
    if sha256_file(manifest) != MANIFEST_SHA256 or sha256_file(pit_path) != PIT_SHA256:
        raise PriceAlphaError("DATA_ERROR_STOP: frozen manifest or PIT SHA differs")

    summary = json.loads((artifact / "data/summary.json").read_text(encoding="utf-8"))
    labels = tuple(summary.get("labels", []))
    if (
        summary.get("symbols_curated") != 208
        or summary.get("quarantined_symbols") != []
        or set(labels) != {"archive-extended", "survivor-biased", "exploration-only"}
    ):
        raise PriceAlphaError("DATA_ERROR_STOP: frozen summary contract differs")

    bars: dict[str, dict[int, Bar]] = {}
    last: dict[str, int] = {}
    kline_files = [item for item in record["files"] if item["role"].startswith("kline.")]
    if len(kline_files) != 208:
        raise PriceAlphaError("DATA_ERROR_STOP: curated kline role count differs from 208")
    for item in sorted(kline_files, key=lambda value: value["role"]):
        symbol = item["role"].split(".", 1)[1]
        path = artifact / item["relpath"]
        points: dict[int, Bar] = {}
        previous: int | None = None
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                row = json.loads(line)
                timestamp = int(row["open_time_utc_ms"])
                if timestamp in points or timestamp % DAY_MS:
                    raise PriceAlphaError(
                        f"DATA_ERROR_STOP: duplicate/non-UTC {symbol}:{line_no}"
                    )
                if previous is not None and timestamp != previous + DAY_MS:
                    raise PriceAlphaError(
                        f"DATA_ERROR_STOP: internal timestamp gap {symbol}:{iso(previous)}"
                    )
                opened, high, low, closed, quote_volume = (
                    float(row[name])
                    for name in ("open", "high", "low", "close", "quote_volume")
                )
                values = (opened, high, low, closed, quote_volume)
                if (
                    not all(math.isfinite(value) for value in values)
                    or min(opened, high, low, closed) <= 0
                    or quote_volume < 0
                    or high < max(opened, closed)
                    or low > min(opened, closed)
                    or high < low
                ):
                    raise PriceAlphaError(f"DATA_ERROR_STOP: invalid price/volume {symbol}:{line_no}")
                points[timestamp] = Bar(opened, closed, quote_volume, high, low)
                previous = timestamp
        if not points:
            raise PriceAlphaError(f"DATA_ERROR_STOP: empty curated series {symbol}")
        bars[symbol], last[symbol] = points, max(points)

    pit: dict[int, tuple[str, ...]] = {}
    with pit_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            effective = int(row["effective_month_start_utc_ms"])
            symbols = tuple(row["symbols"])
            if effective in pit or effective != month_start_ms(effective):
                raise PriceAlphaError("DATA_ERROR_STOP: duplicate/non-month PIT timestamp")
            if symbols != tuple(sorted(set(symbols))) or any(symbol not in bars for symbol in symbols):
                raise PriceAlphaError("DATA_ERROR_STOP: malformed PIT membership")
            if row.get("completed_bars") != 90:
                raise PriceAlphaError("DATA_ERROR_STOP: PIT completed-bar contract differs")
            for symbol in symbols:
                required = range(effective - 90 * DAY_MS, effective, DAY_MS)
                if not all(timestamp in bars[symbol] for timestamp in required):
                    raise PriceAlphaError(
                        f"DATA_ERROR_STOP: PIT membership predates history {symbol}:{iso(effective)}"
                    )
            pit[effective] = symbols
    return PriceDataset(
        bars=bars,
        last_timestamp=last,
        pit=pit,
        artifact_path=artifact,
        manifest_sha256=sha256_file(manifest),
        pit_sha256=sha256_file(pit_path),
        labels=labels,
    )


def normalized_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Ascending average ranks normalized to [0, 1], with deterministic ties."""
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    output: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (index + end - 1) / 2
        normalized = average / (len(ordered) - 1) if len(ordered) > 1 else 0.5
        for offset in range(index, end):
            output[ordered[offset][0]] = normalized
        index = end
    return output


def quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise PriceAlphaError("empty quantile")
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def top_liquid(
    dataset: PriceDataset, execution_ms: int, decision_ms: int, count: int
) -> tuple[list[str], dict[str, float], int]:
    members = dataset.universe(execution_ms)
    volumes: dict[str, float] = {}
    inactive = 0
    for symbol in members:
        # A verified series may end mid-month. At the decision close its
        # absence is observable and means it is no longer orderable; internal
        # gaps were already rejected by load_dataset.
        if decision_ms not in dataset.bars[symbol]:
            inactive += 1
            continue
        volumes[symbol] = dataset.median_volume(symbol, decision_ms)
    if len(volumes) < count:
        raise PriceAlphaError(
            f"DATA_ERROR_STOP: only {len(volumes)} active PIT members for top-{count}"
        )
    selected = [
        symbol
        for symbol, _ in sorted(volumes.items(), key=lambda item: (-item[1], item[0]))[:count]
    ]
    for symbol in selected:
        if execution_ms not in dataset.bars[symbol]:
            raise PriceAlphaError(
                f"DATA_ERROR_STOP: selected symbol lacks entry open {symbol}:{iso(execution_ms)}"
            )
    return selected, {symbol: volumes[symbol] for symbol in selected}, inactive


def volume_deciles(volumes: Mapping[str, float]) -> dict[str, int]:
    ranks = normalized_ranks(volumes)
    return {symbol: min(10, int(rank * 10) + 1) for symbol, rank in ranks.items()}


def momentum_snapshot(
    dataset: PriceDataset, execution_ms: int, count: int
) -> tuple[dict[str, float], float, dict[str, int], int]:
    decision = execution_ms - DAY_MS
    selected, volumes, inactive = top_liquid(dataset, execution_ms, decision, count)
    horizons = {
        horizon: {
            symbol: dataset.close_return(symbol, decision, horizon) for symbol in selected
        }
        for horizon in (7, 14, 28)
    }
    ranks = {horizon: normalized_ranks(values) for horizon, values in horizons.items()}
    signal = {
        symbol: sum(ranks[horizon][symbol] for horizon in (7, 14, 28)) / 3
        for symbol in selected
    }
    raw_composite = {
        symbol: sum(horizons[horizon][symbol] for horizon in (7, 14, 28)) / 3
        for symbol in selected
    }
    dispersion = quantile(raw_composite.values(), 0.75) - quantile(raw_composite.values(), 0.25)
    return signal, dispersion, volume_deciles(volumes), inactive


def long_short_weights(
    signal: Mapping[str, float], fraction: float, deciles: Mapping[str, int] | None = None,
    tilted: bool = False,
) -> dict[str, float]:
    ordered = sorted(signal, key=lambda symbol: (signal[symbol], symbol))
    leg_count = max(1, int(math.floor(len(ordered) * fraction)))
    shorts, longs = ordered[:leg_count], ordered[-leg_count:]

    def leg_weights(symbols: list[str], gross: float) -> dict[str, float]:
        multipliers = {}
        for symbol in symbols:
            decile = (deciles or {}).get(symbol, 10)
            multipliers[symbol] = (
                {1: 1.5, 2: 4 / 3, 3: 7 / 6}.get(decile, 1.0) if tilted else 1.0
            )
        total = sum(multipliers.values())
        return {symbol: gross * value / total for symbol, value in multipliers.items()}

    weights = leg_weights(longs, 0.5)
    weights.update(leg_weights(shorts, -0.5))
    return weights


def forward_open_return(
    dataset: PriceDataset, symbol: str, execution_ms: int, end_ms: int
) -> float:
    start = dataset.bar(symbol, execution_ms).open
    if end_ms in dataset.bars[symbol]:
        return dataset.bars[symbol][end_ms].open / start - 1.0
    terminal = dataset.last_timestamp[symbol]
    if terminal < execution_ms:
        raise PriceAlphaError(f"DATA_ERROR_STOP: forward start after terminal {symbol}")
    if terminal >= end_ms:
        raise PriceAlphaError(f"DATA_ERROR_STOP: internal forward endpoint absent {symbol}")
    return dataset.bars[symbol][terminal].close / start - 1.0


def execution_schedule(days: int) -> list[int]:
    return list(range(START_MS, FINAL_OPEN_MS + 1, days * DAY_MS))


def build_momentum_events(
    dataset: PriceDataset, count: int, rebalance_days: int, *, tilted: bool = False
) -> list[Event]:
    snapshots = []
    for execution in execution_schedule(rebalance_days):
        signal, dispersion, deciles, inactive = momentum_snapshot(dataset, execution, count)
        snapshots.append((execution, signal, dispersion, deciles, inactive))
    train_dispersions = [value[2] for value in snapshots if value[0] < OOS_START_MS]
    if len(train_dispersions) < 4:
        raise PriceAlphaError("DATA_ERROR_STOP: insufficient train dispersion history")
    fixed_oos_threshold = statistics.median(train_dispersions)
    prior_train: list[float] = []
    events: list[Event] = []
    for execution, signal, dispersion, deciles, inactive in snapshots:
        if execution < OOS_START_MS:
            threshold = statistics.median(prior_train) if len(prior_train) >= 4 else None
            active = threshold is not None and dispersion > threshold
            prior_train.append(dispersion)
        else:
            threshold = fixed_oos_threshold
            active = dispersion > threshold
        end = execution + rebalance_days * DAY_MS
        forward = {
            symbol: forward_open_return(dataset, symbol, execution, end)
            for symbol in signal
            if end < END_MS
        }
        events.append(
            Event(
                execution_ms=execution,
                decision_ms=execution - DAY_MS,
                weights=(
                    long_short_weights(signal, 0.2, deciles, tilted=tilted) if active else {}
                ),
                signals=signal,
                forward_returns=forward,
                volume_deciles=deciles,
                gate_active=active,
                gate_statistic=dispersion,
                gate_threshold=threshold,
                inactive_members=inactive,
            )
        )
    return events


def realized_volatility(dataset: PriceDataset, symbol: str, decision_ms: int, window: int) -> float:
    returns = [
        dataset.bar(symbol, decision_ms - offset * DAY_MS).close
        / dataset.bar(symbol, decision_ms - (offset + 1) * DAY_MS).close
        - 1.0
        for offset in reversed(range(window))
    ]
    return statistics.stdev(returns) * math.sqrt(ANN)


def monthly_schedule(months: int) -> list[int]:
    dates = []
    current = START_MS
    while current <= FINAL_OPEN_MS:
        dates.append(current)
        current = add_months_ms(current, months)
    return dates


def build_vol_events(dataset: PriceDataset, window: int, rebalance_months: int) -> list[Event]:
    events: list[Event] = []
    for execution in monthly_schedule(rebalance_months):
        decision = execution - DAY_MS
        members = dataset.universe(execution)
        eligible: list[str] = []
        inactive = 0
        for symbol in members:
            if decision not in dataset.bars[symbol]:
                inactive += 1
                continue
            # Missing internal history raises instead of silently shrinking the
            # window; a terminal series absent at decision is already inactive.
            realized_volatility(dataset, symbol, decision, window)
            if execution not in dataset.bars[symbol]:
                raise PriceAlphaError(
                    f"DATA_ERROR_STOP: vol-selected universe lacks entry open {symbol}:{iso(execution)}"
                )
            eligible.append(symbol)
        if len(eligible) < 6:
            raise PriceAlphaError("DATA_ERROR_STOP: fewer than six vol-eligible PIT members")
        signal = {
            symbol: -realized_volatility(dataset, symbol, decision, window)
            for symbol in eligible
        }
        end = add_months_ms(execution, rebalance_months)
        forward = {
            symbol: forward_open_return(dataset, symbol, execution, end)
            for symbol in eligible
            if end < END_MS
        }
        events.append(
            Event(
                execution_ms=execution,
                decision_ms=decision,
                weights=long_short_weights(signal, 1 / 3),
                signals=signal,
                forward_returns=forward,
                inactive_members=inactive,
            )
        )
    return events


def simulate(dataset: PriceDataset, events: list[Event]) -> Simulation:
    by_date = {event.execution_ms: event for event in events}
    dates = list(range(START_MS, FINAL_OPEN_MS, DAY_MS))
    gross: list[float] = []
    turnover: list[float] = []
    liquidity_cost: list[float] = []
    trade_count: list[int] = []
    rebalance_count: list[int] = []
    symbol_gross: list[dict[str, float]] = []
    symbol_turnover: list[dict[str, float]] = []
    symbol_liquidity_cost: list[dict[str, float]] = []
    terminal_liquidations: list[dict[str, object]] = []
    held: dict[str, float] = {}
    held_deciles: dict[str, int] = {}

    for current in dates:
        day_turnover: dict[str, float] = {}
        day_liquidity: dict[str, float] = {}
        trades = 0
        rebalanced = 0

        def transact(symbol: str, amount: float, decile: int) -> None:
            nonlocal trades
            if abs(amount) <= 1e-15:
                return
            absolute = abs(amount)
            day_turnover[symbol] = day_turnover.get(symbol, 0.0) + absolute
            day_liquidity[symbol] = day_liquidity.get(symbol, 0.0) + absolute * (
                BASE_COST + SURCHARGE.get(decile, 0.0)
            )
            trades += 1

        if current in by_date:
            event = by_date[current]
            target = event.weights
            for symbol in sorted(set(held) | set(target)):
                decile = event.volume_deciles.get(symbol, held_deciles.get(symbol, 10))
                transact(symbol, target.get(symbol, 0.0) - held.get(symbol, 0.0), decile)
            held = dict(target)
            held_deciles = {
                symbol: event.volume_deciles.get(symbol, held_deciles.get(symbol, 10))
                for symbol in held
            }
            rebalanced = 1

        day_gross: dict[str, float] = {}
        terminal: list[str] = []
        for symbol, weight in sorted(held.items()):
            bar = dataset.bar(symbol, current)
            next_timestamp = current + DAY_MS
            if next_timestamp in dataset.bars[symbol]:
                asset_return = dataset.bars[symbol][next_timestamp].open / bar.open - 1.0
            elif dataset.last_timestamp[symbol] == current:
                asset_return = bar.close / bar.open - 1.0
                terminal.append(symbol)
            else:
                raise PriceAlphaError(
                    f"DATA_ERROR_STOP: missing non-terminal next open {symbol}:{iso(current)}"
                )
            day_gross[symbol] = weight * asset_return

        for symbol in terminal:
            transact(symbol, -held[symbol], held_deciles.get(symbol, 10))
            terminal_liquidations.append(
                {"symbol": symbol, "date": iso(current), "method": "final_open_to_close_then_exit"}
            )
            held.pop(symbol, None)
            held_deciles.pop(symbol, None)

        gross.append(sum(day_gross.values()))
        turnover.append(sum(day_turnover.values()))
        liquidity_cost.append(sum(day_liquidity.values()))
        trade_count.append(trades)
        rebalance_count.append(rebalanced)
        symbol_gross.append(day_gross)
        symbol_turnover.append(day_turnover)
        symbol_liquidity_cost.append(day_liquidity)

    # Value exists at the final 2023-12-31 open. Close every remaining book
    # there and charge the exit to the final measured interval.
    for symbol, weight in sorted(held.items()):
        amount = abs(weight)
        decile = held_deciles.get(symbol, 10)
        turnover[-1] += amount
        liquidity_cost[-1] += amount * (BASE_COST + SURCHARGE.get(decile, 0.0))
        symbol_turnover[-1][symbol] = symbol_turnover[-1].get(symbol, 0.0) + amount
        symbol_liquidity_cost[-1][symbol] = (
            symbol_liquidity_cost[-1].get(symbol, 0.0)
            + amount * (BASE_COST + SURCHARGE.get(decile, 0.0))
        )
        trade_count[-1] += 1
    return Simulation(
        dates,
        gross,
        turnover,
        liquidity_cost,
        trade_count,
        rebalance_count,
        symbol_gross,
        symbol_turnover,
        symbol_liquidity_cost,
        terminal_liquidations,
    )


def portfolio_metrics(returns: list[float], turnover: list[float]) -> dict[str, float | int]:
    if len(returns) < 2:
        raise PriceAlphaError("metric window has fewer than two returns")
    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        if value <= -1.0:
            raise PriceAlphaError("portfolio return reached or crossed -100%")
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum_drawdown = min(maximum_drawdown, equity / peak - 1.0)
    mean = statistics.mean(returns)
    volatility = statistics.stdev(returns)
    active_returns = [value for value in returns if abs(value) > 1e-15]
    return {
        "days": len(returns),
        "total_return": equity - 1.0,
        "annualized_return": equity ** (ANN / len(returns)) - 1.0,
        "annualized_volatility": volatility * math.sqrt(ANN),
        "sharpe": mean / volatility * math.sqrt(ANN) if volatility else 0.0,
        "max_drawdown": maximum_drawdown,
        "win_rate": sum(value > 0 for value in returns) / len(returns),
        "win_rate_definition": "positive_net_days_divided_by_all_days",
        "active_days": len(active_returns),
        "active_day_win_rate": (
            sum(value > 0 for value in active_returns) / len(active_returns)
            if active_returns else None
        ),
        "turnover_total": sum(turnover),
        "turnover_annualized": sum(turnover) * ANN / len(returns),
    }


def cost_series(simulation: Simulation, indices: list[int], lens: str) -> list[float]:
    output = []
    for index in indices:
        gross = simulation.gross[index]
        if lens == "gross_0bps":
            cost = 0.0
        elif lens == "flat_15bps":
            cost = simulation.turnover[index] * BASE_COST
        elif lens == "flat_30bps":
            cost = simulation.turnover[index] * STRESS_COST
        elif lens == "liquidity_baseline":
            cost = simulation.liquidity_cost[index]
        elif lens == "liquidity_stress":
            cost = simulation.liquidity_cost[index] * 2
        else:
            raise ValueError(lens)
        output.append(gross - cost)
    return output


def btc_regime(dataset: PriceDataset, execution_ms: int) -> str:
    decision = execution_ms - DAY_MS
    trailing = dataset.close_return("BTCUSDT", decision, 90)
    if trailing > 0.20:
        return "bull"
    if trailing < -0.20:
        return "bear"
    return "sideways"


def contribution(
    simulation: Simulation, indices: list[int], lens: str
) -> tuple[dict[str, float], dict[str, object]]:
    values: dict[str, float] = {}
    for index in indices:
        symbols = set(simulation.symbol_gross[index]) | set(simulation.symbol_turnover[index])
        for symbol in symbols:
            value = simulation.symbol_gross[index].get(symbol, 0.0)
            if lens == "flat_15bps":
                value -= simulation.symbol_turnover[index].get(symbol, 0.0) * BASE_COST
            elif lens == "flat_30bps":
                value -= simulation.symbol_turnover[index].get(symbol, 0.0) * STRESS_COST
            elif lens == "liquidity_baseline":
                value -= simulation.symbol_liquidity_cost[index].get(symbol, 0.0)
            elif lens == "liquidity_stress":
                value -= simulation.symbol_liquidity_cost[index].get(symbol, 0.0) * 2
            values[symbol] = values.get(symbol, 0.0) + value
    denominator = sum(abs(value) for value in values.values())
    largest = max(values, key=lambda symbol: abs(values[symbol])) if values else None
    summary = {
        "largest_symbol": largest,
        "largest_absolute_share": abs(values[largest]) / denominator if largest and denominator else 0.0,
        "symbols_with_contribution": len(values),
    }
    return dict(sorted(values.items())), summary


def segment_report(
    dataset: PriceDataset, simulation: Simulation, start: int, end: int, *, liquidity: bool
) -> dict[str, object]:
    indices = [index for index, timestamp in enumerate(simulation.dates) if start <= timestamp < end]
    lenses = ["gross_0bps", "flat_15bps", "flat_30bps"]
    if liquidity:
        lenses += ["liquidity_baseline", "liquidity_stress"]
    result: dict[str, object] = {
        "range": f"{iso(start)}..{iso(end - DAY_MS)}",
        "rebalance_count": sum(simulation.rebalance_count[index] for index in indices),
        "trade_count": sum(simulation.trade_count[index] for index in indices),
        "cost_lenses": {},
    }
    for lens in lenses:
        returns = cost_series(simulation, indices, lens)
        metrics = portfolio_metrics(returns, [simulation.turnover[index] for index in indices])
        symbol_values, concentration_summary = contribution(simulation, indices, lens)
        regimes: dict[str, object] = {}
        positive_pnl = 0.0
        for regime in ("bull", "bear", "sideways"):
            selected = [
                offset
                for offset, index in enumerate(indices)
                if btc_regime(dataset, simulation.dates[index]) == regime
            ]
            subset = [returns[offset] for offset in selected]
            pnl = sum(subset)
            positive_pnl += max(0.0, pnl)
            regimes[regime] = {
                "days": len(subset),
                "arithmetic_pnl": pnl,
                "win_rate": sum(value > 0 for value in subset) / len(subset) if subset else None,
            }
        for regime in regimes.values():
            pnl = regime["arithmetic_pnl"]
            regime["positive_pnl_share"] = max(0.0, pnl) / positive_pnl if positive_pnl else 0.0
        result["cost_lenses"][lens] = {
            "metrics": metrics,
            "symbol_contribution": symbol_values,
            "concentration": concentration_summary,
            "regimes": regimes,
        }
    return result


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = statistics.mean(left), statistics.mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    return numerator / denominator if denominator else None


def hac_mean_test(values: list[float], lag: int = 3) -> dict[str, float | int | None]:
    if len(values) < 3:
        return {"n": len(values), "mean": None, "t_stat": None, "p_one_sided_normal": None}
    mean = statistics.mean(values)
    centered = [value - mean for value in values]
    count = len(values)
    long_run = sum(value * value for value in centered) / count
    for offset in range(1, min(lag, count - 1) + 1):
        covariance = sum(centered[index] * centered[index - offset] for index in range(offset, count)) / count
        long_run += 2 * (1 - offset / (lag + 1)) * covariance
    standard_error = math.sqrt(max(long_run, 0.0) / count)
    if standard_error == 0:
        t_stat = None
        p_value = 0.0 if mean > 0 else (1.0 if mean < 0 else 0.5)
    else:
        t_stat = mean / standard_error
        p_value = 0.5 * math.erfc(t_stat / math.sqrt(2))
    return {"n": count, "mean": mean, "t_stat": t_stat, "p_one_sided_normal": p_value}


def signal_diagnostics(
    events: list[Event], start: int, end: int, *, active_only: bool
) -> dict[str, object]:
    rank_ics: list[float] = []
    raw_ics: list[float] = []
    quintile_events: list[list[float]] = []
    pairs = 0
    used_events = 0
    for event in events:
        if not (start <= event.execution_ms < end) or (active_only and not event.gate_active):
            continue
        common = sorted(set(event.signals) & set(event.forward_returns))
        if len(common) < 5:
            continue
        signals = [event.signals[symbol] for symbol in common]
        forwards = [event.forward_returns[symbol] for symbol in common]
        raw_ic = pearson(signals, forwards)
        signal_ranks = normalized_ranks({symbol: event.signals[symbol] for symbol in common})
        forward_ranks = normalized_ranks({symbol: event.forward_returns[symbol] for symbol in common})
        rank_ic = pearson(
            [signal_ranks[symbol] for symbol in common],
            [forward_ranks[symbol] for symbol in common],
        )
        if raw_ic is None or rank_ic is None:
            continue
        raw_ics.append(raw_ic)
        rank_ics.append(rank_ic)
        ordered = sorted(common, key=lambda symbol: (event.signals[symbol], symbol))
        groups = [[] for _ in range(5)]
        for index, symbol in enumerate(ordered):
            group = min(4, index * 5 // len(ordered))
            groups[group].append(event.forward_returns[symbol])
        quintile_events.append([statistics.mean(group) for group in groups])
        pairs += len(common)
        used_events += 1
    quintiles = [
        statistics.mean(event_values[index] for event_values in quintile_events)
        if quintile_events else None
        for index in range(5)
    ]
    return {
        "events": used_events,
        "cross_sectional_pairs": pairs,
        "pearson_ic_mean": statistics.mean(raw_ics) if raw_ics else None,
        "rank_ic": hac_mean_test(rank_ics, 3),
        "forward_return_quintiles_q1_to_q5": quintiles,
        "q5_minus_q1_spread": (
            quintiles[4] - quintiles[0] if quintiles[0] is not None else None
        ),
    }


def config_report(
    dataset: PriceDataset,
    events: list[Event],
    parameters: Mapping[str, object],
    *,
    liquidity: bool = False,
) -> dict[str, object]:
    simulation = simulate(dataset, events)
    return {
        "parameters": dict(parameters),
        "train": segment_report(dataset, simulation, START_MS, OOS_START_MS, liquidity=liquidity),
        "oos": segment_report(dataset, simulation, OOS_START_MS, END_MS, liquidity=liquidity),
        "full": segment_report(dataset, simulation, START_MS, END_MS, liquidity=liquidity),
        "signal_diagnostics": {
            "train_active": signal_diagnostics(events, START_MS, OOS_START_MS, active_only=True),
            "train_all": signal_diagnostics(events, START_MS, OOS_START_MS, active_only=False),
            "oos_active": signal_diagnostics(events, OOS_START_MS, END_MS, active_only=True),
            "oos_all": signal_diagnostics(events, OOS_START_MS, END_MS, active_only=False),
        },
        "gate": {
            "events": len(events),
            "active": sum(event.gate_active for event in events),
            "inactive_member_decisions": sum(event.inactive_members for event in events),
        },
        "terminal_liquidations": simulation.terminal_liquidations,
    }


def lens(config: Mapping[str, object], segment: str, name: str) -> Mapping[str, object]:
    return config[segment]["cost_lenses"][name]


def common_checks(config: Mapping[str, object], *, liquidity: bool = False) -> dict[str, bool]:
    train = lens(config, "train", "flat_15bps")
    oos_base = lens(config, "oos", "flat_15bps")
    oos_stress = lens(config, "oos", "flat_30bps")
    rank_train = config["signal_diagnostics"]["train_active"]["rank_ic"]
    rank_oos = config["signal_diagnostics"]["oos_active"]["rank_ic"]
    regimes = oos_base["regimes"]
    checks = {
        "train_baseline_sharpe_positive": train["metrics"]["sharpe"] > 0,
        "train_rank_ic_positive": (rank_train["mean"] or 0.0) > 0,
        "oos_baseline_annual_return_positive": oos_base["metrics"]["annualized_return"] > 0,
        "oos_baseline_sharpe_at_least_0_5": oos_base["metrics"]["sharpe"] >= 0.5,
        "oos_stress_total_return_positive": oos_stress["metrics"]["total_return"] > 0,
        "oos_stress_sharpe_positive": oos_stress["metrics"]["sharpe"] > 0,
        "oos_rank_ic_positive": (rank_oos["mean"] or 0.0) > 0,
        "oos_rank_ic_p_at_most_0_05": (
            rank_oos["p_one_sided_normal"] is not None
            and rank_oos["p_one_sided_normal"] <= 0.05
        ),
        "oos_symbol_concentration_at_most_0_25": oos_base["concentration"]["largest_absolute_share"] <= 0.25,
        "oos_two_regimes_positive": sum(
            value["arithmetic_pnl"] > 0 for value in regimes.values()
        ) >= 2,
        "oos_positive_regime_share_at_most_0_75": max(
            value["positive_pnl_share"] for value in regimes.values()
        ) <= 0.75,
    }
    if liquidity:
        liquid_base = lens(config, "oos", "liquidity_baseline")
        liquid_stress = lens(config, "oos", "liquidity_stress")
        liquid_regimes = liquid_base["regimes"]
        checks.update(
            {
                "oos_liquidity_baseline_annual_return_positive": liquid_base["metrics"]["annualized_return"] > 0,
                "oos_liquidity_baseline_sharpe_at_least_0_5": liquid_base["metrics"]["sharpe"] >= 0.5,
                "oos_liquidity_stress_total_return_positive": liquid_stress["metrics"]["total_return"] > 0,
                "oos_liquidity_stress_sharpe_positive": liquid_stress["metrics"]["sharpe"] > 0,
                "oos_liquidity_concentration_at_most_0_25": liquid_base["concentration"]["largest_absolute_share"] <= 0.25,
                "oos_liquidity_two_regimes_positive": sum(
                    value["arithmetic_pnl"] > 0 for value in liquid_regimes.values()
                ) >= 2,
                "oos_liquidity_positive_regime_share_at_most_0_75": max(
                    value["positive_pnl_share"] for value in liquid_regimes.values()
                ) <= 0.75,
            }
        )
    return checks


def base_artifact(experiment_id: str, runner_sha: str, current_backlog_sha: str) -> dict[str, object]:
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_sha = "UNKNOWN"
    return {
        "experiment_id": experiment_id,
        "artifact_class": "EXPLORATION_ONLY_NOT_EVIDENCE",
        "code_binding": {
            "code_sha": runner_sha,
            "runner_sha256": runner_sha,
            "frozen_backlog_sha256": FROZEN_BACKLOG_SHA256,
            "current_backlog_sha256": current_backlog_sha,
            "base_git_sha": git_sha,
        },
        "dataset_binding": {
            "dataset_id": DATASET,
            "dataset_sha": MANIFEST_SHA256,
            "snapshot_id": SNAPSHOT_ID,
            "snapshot_manifest_sha256": MANIFEST_SHA256,
            "pit_universe_sha256": PIT_SHA256,
            "registry_integrity": "VERIFIED",
            "quality_verdict": "PASS",
            "symbols_curated": 208,
            "quarantine": 0,
            "labels": ["archive-extended", "survivor-biased", "exploration-only"],
        },
        "time_range": {
            "common": "2021-01-01..2023-12-31",
            "train": "2021-01-01..2021-12-31",
            "oos": "2022-01-01..2023-12-31",
            "decision_to_execution": "close_t_to_open_t_plus_1",
            "return_convention": "open_to_next_open; terminal open_to_close",
        },
        "limitations": {
            "FUNDING_NOT_MODELED": True,
            "LIVE_PROMOTION": "BLOCKED",
            "survivor_biased": True,
            "formal_pass_eligible": False,
            "maximum_conclusion": "EXPLORATION_PASS",
            "ALPHA_PROMOTION_PASS": False,
            "LIVE_READY": False,
            "PRODUCTION_READY": False,
        },
    }


def run(dataset: PriceDataset) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    runner_sha = sha256_file(pathlib.Path(__file__))
    backlog_sha = sha256_file(pathlib.Path(__file__).parent / "hypothesis-backlog.md")

    expl001_configs = []
    momentum_events: dict[tuple[int, int], list[Event]] = {}
    for count, days in MOMENTUM_GRID:
        events = build_momentum_events(dataset, count, days)
        momentum_events[(count, days)] = events
        expl001_configs.append(
            config_report(dataset, events, {"N": count, "rebalance_days": days})
        )
    primary001 = next(
        config for config in expl001_configs if config["parameters"] == {"N": 30, "rebalance_days": 7}
    )
    checks001 = common_checks(primary001)
    checks001.update(
        {
            "neighborhood_4_of_6_oos_baseline_sharpe_positive": sum(
                lens(config, "oos", "flat_15bps")["metrics"]["sharpe"] > 0
                for config in expl001_configs
            ) >= 4,
            "neighborhood_3_of_6_oos_stress_sharpe_positive": sum(
                lens(config, "oos", "flat_30bps")["metrics"]["sharpe"] > 0
                for config in expl001_configs
            ) >= 3,
        }
    )
    report001 = base_artifact("EXPL-001", runner_sha, backlog_sha)
    report001.update(
        {
            "signal_definition": {
                "formula": "mean normalized ranks of 7d, 14d, 28d close returns; raw composite-return IQR gate",
                "primary": {"N": 30, "rebalance_days": 7},
                "grid": [{"N": n, "rebalance_days": days} for n, days in MOMENTUM_GRID],
                "direction": "long top quintile, short bottom quintile, 0.5 gross per leg",
            },
            "configs": expl001_configs,
            "primary_checks": checks001,
            "classification": "EXPLORATION_PASS" if all(checks001.values()) else "FAIL",
            "failure_reasons": [name for name, passed in checks001.items() if not passed],
        }
    )

    expl003_configs = []
    for window, months in VOL_GRID:
        events = build_vol_events(dataset, window, months)
        expl003_configs.append(
            config_report(
                dataset, events, {"vol_window_days": window, "rebalance_months": months}
            )
        )
    primary003 = next(
        config
        for config in expl003_configs
        if config["parameters"] == {"vol_window_days": 30, "rebalance_months": 1}
    )
    checks003 = common_checks(primary003)
    checks003.update(
        {
            "neighborhood_3_of_4_oos_baseline_sharpe_positive": sum(
                lens(config, "oos", "flat_15bps")["metrics"]["sharpe"] > 0
                for config in expl003_configs
            ) >= 3,
            "neighborhood_2_of_4_oos_stress_sharpe_positive": sum(
                lens(config, "oos", "flat_30bps")["metrics"]["sharpe"] > 0
                for config in expl003_configs
            ) >= 2,
        }
    )
    report003 = base_artifact("EXPL-003", runner_sha, backlog_sha)
    report003.update(
        {
            "signal_definition": {
                "formula": "negative annualized sample volatility of trailing 14d or 30d close returns",
                "primary": {"vol_window_days": 30, "rebalance_months": 1},
                "grid": [
                    {"vol_window_days": window, "rebalance_months": months}
                    for window, months in VOL_GRID
                ],
                "direction": "long lowest-vol tercile, short highest-vol tercile, 0.5 gross per leg",
            },
            "configs": expl003_configs,
            "primary_checks": checks003,
            "classification": "EXPLORATION_PASS" if all(checks003.values()) else "FAIL",
            "failure_reasons": [name for name, passed in checks003.items() if not passed],
        }
    )

    untilted_events = momentum_events[(30, 7)]
    tilted_events = build_momentum_events(dataset, 30, 7, tilted=True)
    untilted004 = config_report(
        dataset, untilted_events, {"N": 30, "rebalance_days": 7, "tilt": "none"}, liquidity=True
    )
    tilted004 = config_report(
        dataset,
        tilted_events,
        {"N": 30, "rebalance_days": 7, "tilt": "bottom-3-decays-only"},
        liquidity=True,
    )
    checks004 = common_checks(tilted004, liquidity=True)
    comparison_lenses = ("flat_15bps", "flat_30bps", "liquidity_baseline", "liquidity_stress")
    checks004["tilt_beats_none_oos_sharpe_in_3_of_4_cost_lenses"] = sum(
        lens(tilted004, "oos", name)["metrics"]["sharpe"]
        > lens(untilted004, "oos", name)["metrics"]["sharpe"]
        for name in comparison_lenses
    ) >= 3
    report004 = base_artifact("EXPL-004", runner_sha, backlog_sha)
    report004.update(
        {
            "signal_definition": {
                "formula": "EXPL-001 N30 weekly signal with low-volume-decile leg multipliers",
                "primary": {"N": 30, "rebalance_days": 7, "tilt": "bottom-3-decays-only"},
                "grid": [
                    {"tilt": "none"},
                    {"tilt": "bottom-3-decays-only", "multipliers": {"decile_1": 1.5, "decile_2": 4 / 3, "decile_3": 7 / 6}},
                ],
                "direction": "long momentum winners, short losers; tilt weights inside both legs",
            },
            "configs": [untilted004, tilted004],
            "primary_checks": checks004,
            "classification": "EXPLORATION_PASS" if all(checks004.values()) else "FAIL",
            "failure_reasons": [name for name, passed in checks004.items() if not passed],
        }
    )

    reports = [report001, report003, report004]
    ranking = sorted(
        (
            {
                "rank_key_experiment": report["experiment_id"],
                "classification": report["classification"],
                "primary_oos_flat_15bps_sharpe": lens(
                    next(
                        config
                        for config in report["configs"]
                        if config["parameters"] == report["signal_definition"]["primary"]
                    ),
                    "oos",
                    "flat_15bps",
                )["metrics"]["sharpe"],
            }
            for report in reports
        ),
        key=lambda item: (
            item["classification"] != "EXPLORATION_PASS",
            -item["primary_oos_flat_15bps_sharpe"],
        ),
    )
    for position, item in enumerate(ranking, 1):
        item["rank"] = position
    summary = {
        "batch": "GMAQ Price Alpha Exploration v1",
        "artifact_class": "EXPLORATION_ONLY_NOT_EVIDENCE",
        "code_sha": runner_sha,
        "dataset_id": SNAPSHOT_ID,
        "dataset_sha": MANIFEST_SHA256,
        "ranking_basis": "frozen-primary OOS flat-15bps Sharpe; reporting only, never parameter selection",
        "candidate_ranking": ranking,
        "classifications": {report["experiment_id"]: report["classification"] for report in reports},
        "FUNDING_NOT_MODELED": True,
        "LIVE_PROMOTION": "BLOCKED",
        "STOP_AFTER_EXPLORATION": True,
    }
    return report001, report003, report004, summary


def write_json(path: pathlib.Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    output_dir = pathlib.Path(__file__).parent
    try:
        dataset = load_dataset()
        report001, report003, report004, summary = run(dataset)
    except (PriceAlphaError, DataLayerError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "DATA_ERROR_STOP", "detail": str(error)}, ensure_ascii=False))
        return 2
    write_json(output_dir / "expl-001-report.json", report001)
    write_json(output_dir / "expl-003-report.json", report003)
    write_json(output_dir / "expl-004-report.json", report004)
    write_json(output_dir / "expl-price-alpha-v1-summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
