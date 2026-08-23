#!/usr/bin/env python3
"""Correctness-only implementation gate for EXPL-017.

This module deliberately has no formal-performance execution path.  It can
only (1) replay the committed hand-calculated cases and (2) verify that the
already-admitted Price V1 snapshot still satisfies the EXPL-017 data contract.
The separate freeze required for a formal experiment has not happened yet.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Mapping


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from price_alpha_v1 import (  # noqa: E402
    ANN,
    DATA_ROOT,
    DAY_MS,
    MANIFEST_SHA256,
    PIT_SHA256,
    SNAPSHOT_ID,
    Bar,
    PriceAlphaError,
    PriceDataset,
    load_dataset,
    normalized_ranks,
)


EXPERIMENT_ID = "EXPL-017"
GOLD_PATH = pathlib.Path(__file__).with_name("expl-017-gold-sample.json")
ADMISSION_PATH = pathlib.Path(__file__).with_name("expl-017-data-admission.json")
BASE_COST = 0.0015
FORMAL_START = dt.date(2021, 1, 1)
FORMAL_END = dt.date(2023, 12, 31)
TRAIN_END = dt.date(2021, 12, 31)
OOS_END = dt.date(2022, 12, 31)
MOMENTUM_HORIZONS = (7, 14, 28)
VOLUME_WINDOW = 90
WARMUP_DECISIONS = 8
REBALANCE_DAYS = 7
ALLOWED_TOP_N = frozenset({20, 30})
ALLOWED_VOLATILITY_WINDOWS = frozenset({21, 30})


class Expl017Error(RuntimeError):
    """A correctness or data-admission failure for EXPL-017."""


class FormalRunLocked(Expl017Error):
    """Raised before any formal OOS or holdout computation can begin."""


@dataclass(frozen=True)
class EngineConfig:
    """The only reviewable pre-freeze parameter surface for the frozen design."""

    top_n: int = 30
    volatility_window: int = 30

    def validate(self) -> None:
        if self.top_n not in ALLOWED_TOP_N:
            raise Expl017Error("PROCESS_DEFECT: top-N outside frozen neighborhood")
        if self.volatility_window not in ALLOWED_VOLATILITY_WINDOWS:
            raise Expl017Error("PROCESS_DEFECT: volatility window outside frozen neighborhood")


PRIMARY_CONFIG = EngineConfig()


@dataclass(frozen=True)
class Decision:
    """A signal/position correctness record; it intentionally has no PnL fields."""

    execution_ms: int
    decision_ms: int
    segment: str
    selected: tuple[str, ...]
    scores: dict[str, float]
    volatility_statistic: float
    threshold: float | None
    state: str
    target: dict[str, float]
    trade_turnover: float
    terminal_exit_turnover: float
    turnover: float
    cost: float
    terminal_exits: tuple[str, ...]


def _require_finite_positive(values: list[float], label: str) -> None:
    if not values or not all(math.isfinite(value) and value > 0 for value in values):
        raise Expl017Error(f"PROCESS_DEFECT: invalid {label}")


def _gold_payload(path: pathlib.Path = GOLD_PATH) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("artifact_class") != "PRE_IMPLEMENTATION_HAND_CALCULATED_GOLD_SAMPLE"
        or payload.get("experiment_id") != EXPERIMENT_ID
        or len(payload.get("cases", [])) != 3
    ):
        raise Expl017Error("PROCESS_DEFECT: malformed EXPL-017 gold artifact")
    return payload


def momentum_scores(closes: Mapping[str, list[float]], horizons: tuple[int, ...]) -> dict[str, float]:
    """Average ascending cross-sectional ranks of completed-close returns."""
    if not horizons or min(horizons) < 1:
        raise Expl017Error("PROCESS_DEFECT: invalid momentum horizons")
    raw = {}
    for horizon in horizons:
        values = {}
        for symbol, series in closes.items():
            _require_finite_positive(series, f"close history for {symbol}")
            if len(series) <= horizon:
                raise Expl017Error(f"PROCESS_DEFECT: insufficient momentum history for {symbol}")
            values[symbol] = series[-1] / series[-1 - horizon] - 1.0
        raw[horizon] = normalized_ranks(values)
    return {symbol: sum(raw[horizon][symbol] for horizon in horizons) / len(horizons)
            for symbol in closes}


def annualized_sample_volatility(close_series: list[float]) -> float:
    """Annualized sample volatility of completed close-to-close returns."""
    _require_finite_positive(close_series, "volatility close history")
    if len(close_series) < 3:
        raise Expl017Error("PROCESS_DEFECT: volatility needs at least two returns")
    returns = [close_series[index] / close_series[index - 1] - 1.0
               for index in range(1, len(close_series))]
    return statistics.stdev(returns) * math.sqrt(ANN)


def broad_volatility(
    closes: Mapping[str, list[float]], volatility_window: int | None = None
) -> float:
    """Median of the per-name annualized sample volatilities."""
    if not closes:
        raise Expl017Error("PROCESS_DEFECT: empty broad volatility universe")
    if volatility_window is not None:
        if volatility_window < 2 or any(len(series) != volatility_window + 1 for series in closes.values()):
            raise Expl017Error("PROCESS_DEFECT: stated volatility window does not match input bars")
    return statistics.median(annualized_sample_volatility(series) for series in closes.values())


def regime(statistic: float, threshold: float) -> str:
    if not (math.isfinite(statistic) and math.isfinite(threshold)):
        raise Expl017Error("PROCESS_DEFECT: non-finite volatility state")
    return "calm" if statistic <= threshold else "high"


def target_positions(scores: Mapping[str, float], leg_fraction: float, state: str) -> dict[str, float]:
    """Equal-weight ±0.5 legs; high volatility reverses the rank mapping."""
    if not 0 < leg_fraction <= 0.5 or state not in {"calm", "high"}:
        raise Expl017Error("PROCESS_DEFECT: invalid target-position input")
    ordered = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
    leg_count = max(1, math.floor(len(ordered) * leg_fraction))
    bottom, top = ordered[:leg_count], ordered[-leg_count:]
    longs, shorts = (top, bottom) if state == "calm" else (bottom, top)
    positions = {symbol: 0.5 / len(longs) for symbol in longs}
    positions.update({symbol: -0.5 / len(shorts) for symbol in shorts})
    return positions


def turnover(incumbent: Mapping[str, float], target: Mapping[str, float]) -> float:
    return sum(abs(target.get(symbol, 0.0) - incumbent.get(symbol, 0.0))
               for symbol in set(incumbent) | set(target))


def weekly_schedule() -> list[int]:
    """Frozen weekly schedule, anchored at 2021-01-01 UTC only."""
    start = int(dt.datetime.combine(FORMAL_START, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)
    end = int(dt.datetime.combine(FORMAL_END, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)
    return list(range(start, end + 1, REBALANCE_DAYS * DAY_MS))


def _date_for_ms(timestamp: int) -> dt.date:
    return dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC).date()


def _segment_for_ms(execution_ms: int) -> str:
    return segment_for_execution(_date_for_ms(execution_ms))


def _eligible_top_n(dataset: PriceDataset, execution_ms: int, count: int) -> tuple[str, ...]:
    """PIT-effective, 90-completed-bar quote-volume selection with no fallback."""
    decision_ms = execution_ms - DAY_MS
    rows: list[tuple[str, float]] = []
    for symbol in dataset.universe(execution_ms):
        # A member absent before the decision is observable inactive.  Any
        # internal missing bar is a hard data error through PriceDataset.bar.
        if decision_ms not in dataset.bars[symbol]:
            continue
        try:
            volumes = [dataset.bar(symbol, decision_ms - offset * DAY_MS).quote_volume
                       for offset in range(VOLUME_WINDOW)]
            dataset.bar(symbol, execution_ms)  # t+1 entry open must exist.
        except PriceAlphaError:
            raise
        if not all(math.isfinite(value) and value >= 0 for value in volumes):
            raise Expl017Error("DATA_UNAVAILABLE: invalid completed quote volume")
        rows.append((symbol, statistics.median(volumes)))
    if len(rows) < count:
        raise Expl017Error(f"DATA_UNAVAILABLE: only {len(rows)} eligible PIT members for top-{count}")
    return tuple(symbol for symbol, _ in sorted(rows, key=lambda row: (-row[1], row[0]))[:count])


def _decision_inputs(
    dataset: PriceDataset, execution_ms: int, config: EngineConfig
) -> tuple[tuple[str, ...], dict[str, float], float]:
    """All signal inputs stop at close t; no t+1 close is read."""
    config.validate()
    decision_ms = execution_ms - DAY_MS
    selected = _eligible_top_n(dataset, execution_ms, config.top_n)
    ranks: dict[int, dict[str, float]] = {}
    for horizon in MOMENTUM_HORIZONS:
        values = {
            symbol: dataset.bar(symbol, decision_ms).close
            / dataset.bar(symbol, decision_ms - horizon * DAY_MS).close - 1.0
            for symbol in selected
        }
        ranks[horizon] = normalized_ranks(values)
    scores = {symbol: sum(ranks[horizon][symbol] for horizon in MOMENTUM_HORIZONS) / len(MOMENTUM_HORIZONS)
              for symbol in selected}
    per_name_volatility = []
    for symbol in selected:
        closes = [dataset.bar(symbol, decision_ms - offset * DAY_MS).close
                  for offset in reversed(range(config.volatility_window + 1))]
        per_name_volatility.append(annualized_sample_volatility(closes))
    return selected, scores, statistics.median(per_name_volatility)


def _marked_exposure(weight: float, entry: Bar, exit_bar: Bar, *, terminal: bool) -> float:
    """Mark one exposure only; intentionally never aggregates portfolio PnL."""
    end_price = exit_bar.close if terminal else exit_bar.open
    if not (math.isfinite(entry.open) and math.isfinite(end_price) and entry.open > 0 and end_price > 0):
        raise Expl017Error("DATA_UNAVAILABLE: invalid interval mark")
    return weight * end_price / entry.open


def build_correctness_plan(
    dataset: PriceDataset,
    *,
    config: EngineConfig = PRIMARY_CONFIG,
    executions: list[int] | None = None,
    final_liquidation_ms: int | None = None,
) -> list[Decision]:
    """Build signals and continuous turnover/lifecycle records, never returns/PnL.

    Injected data makes timing, PIT, lifecycle, state thresholds, and split
    containment independently reviewable without opening a formal result path.
    """
    config.validate()
    dates = weekly_schedule() if executions is None else list(executions)
    if not dates or dates != sorted(set(dates)):
        raise Expl017Error("PROCESS_DEFECT: executions must be ordered and unique")
    if executions is None:
        final_liquidation_ms = int(dt.datetime.combine(FORMAL_END, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)
    preliminary = []
    for execution_ms in dates:
        segment = _segment_for_ms(execution_ms)
        selected, scores, volatility_statistic = _decision_inputs(dataset, execution_ms, config)
        preliminary.append((execution_ms, segment, selected, scores, volatility_statistic))

    train_statistics = [stat for _, segment, _, _, stat in preliminary if segment == "train"]
    if len(train_statistics) < WARMUP_DECISIONS:
        raise Expl017Error("DATA_UNAVAILABLE: fewer than eight valid train decisions")
    fixed_train_threshold = statistics.median(train_statistics)
    incumbent: dict[str, float] = {}
    prior_train: list[float] = []
    decisions: list[Decision] = []
    for index, (execution_ms, segment, selected, scores, statistic) in enumerate(preliminary):
        if segment == "train" and len(prior_train) < WARMUP_DECISIONS:
            threshold, state, target = None, "warmup", {}
        else:
            threshold = (statistics.median(prior_train) if segment == "train" else fixed_train_threshold)
            state = regime(statistic, threshold)
            target = target_positions(scores, 0.2, state)
        next_ms = dates[index + 1] if index + 1 < len(dates) else final_liquidation_ms
        terminal_exits: list[str] = []
        terminal_exit_turnover = 0.0

        # A contract whose terminal bar is this execution is first marked
        # open-to-close and exited. It is removed from the new target, so it
        # cannot be exited, re-entered, then exited a second time.
        terminal_now = {symbol for symbol in target if dataset.last_timestamp[symbol] == execution_ms}
        for symbol, weight in list(incumbent.items()):
            terminal_ms = dataset.last_timestamp[symbol]
            if terminal_ms < execution_ms:
                raise Expl017Error(f"DATA_UNAVAILABLE: held symbol was not exited {symbol}")
            if terminal_ms == execution_ms:
                terminal_weight = _marked_exposure(
                    weight, dataset.bar(symbol, execution_ms), dataset.bar(symbol, execution_ms), terminal=True
                )
                terminal_exit_turnover += abs(terminal_weight)
                terminal_exits.append(symbol)
                incumbent.pop(symbol)
        target = {symbol: weight for symbol, weight in target.items() if symbol not in terminal_now}
        trade_turnover = turnover(incumbent, target)

        next_incumbent: dict[str, float] = {}
        if next_ms is not None:
            for symbol, weight in target.items():
                terminal_ms = dataset.last_timestamp[symbol]
                if terminal_ms < execution_ms:
                    raise Expl017Error(f"DATA_UNAVAILABLE: selected symbol ended before entry {symbol}")
                entry = dataset.bar(symbol, execution_ms)
                # Strictly before next execution exits in this interval. An
                # equality is carried at next-open and settled at that event.
                if terminal_ms < next_ms:
                    terminal_weight = _marked_exposure(
                        weight, entry, dataset.bar(symbol, terminal_ms), terminal=True
                    )
                    terminal_exits.append(symbol)
                    terminal_exit_turnover += abs(terminal_weight)
                else:
                    next_incumbent[symbol] = _marked_exposure(
                        weight, entry, dataset.bar(symbol, next_ms), terminal=False
                    )
        else:
            next_incumbent = dict(target)
        if final_liquidation_ms is not None and index == len(preliminary) - 1:
            # The final bar is marked open-to-close and liquidated once.
            remaining = {}
            for symbol, weight in next_incumbent.items():
                final_bar = dataset.bar(symbol, final_liquidation_ms)
                remaining[symbol] = _marked_exposure(weight, final_bar, final_bar, terminal=True)
            terminal_exit_turnover += sum(abs(weight) for weight in remaining.values())
            terminal_exits.extend(sorted(remaining))
            next_incumbent = {}
        total_turnover = trade_turnover + terminal_exit_turnover
        decisions.append(Decision(
            execution_ms=execution_ms,
            decision_ms=execution_ms - DAY_MS,
            segment=segment,
            selected=selected,
            scores=scores,
            volatility_statistic=statistic,
            threshold=threshold,
            state=state,
            target=target,
            trade_turnover=trade_turnover,
            terminal_exit_turnover=terminal_exit_turnover,
            turnover=total_turnover,
            cost=total_turnover * BASE_COST,
            terminal_exits=tuple(sorted(terminal_exits)),
        ))
        incumbent = next_incumbent
        if segment == "train":
            prior_train.append(statistic)
    return decisions


def gold_case_result(case: Mapping[str, object]) -> dict[str, object]:
    """Derive one committed gold case from time-t inputs, not expected outputs."""
    parameters = case["parameters"]
    horizons = tuple(parameters["momentum_horizons"])
    leg_fraction = parameters["leg_fraction"]
    volatility_window = parameters["volatility_window"]
    bars = case["input_bars"]
    closes = {symbol: values["closes_t_minus_2_to_t"] for symbol, values in bars.items()}
    scores = momentum_scores(closes, horizons)
    statistic = broad_volatility(closes, volatility_window)
    state = regime(statistic, case["volatility_regime_value"]["frozen_threshold"])
    target = target_positions(scores, leg_fraction, state)
    incumbent = case.get("incumbent_position", {})
    base_turnover = turnover(incumbent, target)

    try:
        decision_text = str(case["decision_timestamp"])
        decision_dt = dt.datetime.strptime(decision_text, "%Y-%m-%dT%H:%M:%SZ-close").replace(tzinfo=dt.UTC)
    except (KeyError, ValueError) as error:
        raise Expl017Error("PROCESS_DEFECT: malformed gold decision timestamp") from error
    decision_timestamp = decision_dt.strftime("%Y-%m-%dT%H:%M:%SZ-close")
    execution_timestamp = (decision_dt + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ-open")
    ordered_longs = ", ".join(sorted(symbol for symbol, weight in target.items() if weight > 0))
    ordered_shorts = ", ".join(sorted(symbol for symbol, weight in target.items() if weight < 0))
    signal = f"{'continuation' if state == 'calm' else 'reversal'}: long {ordered_longs}, short {ordered_shorts}"
    if any("execution_close_not_available_at_decision" in row for row in bars.values()):
        signal += "; execution-day closes cannot alter the decision"

    returns: dict[str, float] = {}
    terminal_exits: dict[str, float] = {}
    for symbol, weight in target.items():
        row = bars[symbol]
        entry = float(row["execution_open"])
        if row.get("next_execution_open") is None:
            terminal = float(row["terminal_close"])
            returns[symbol] = terminal / entry - 1.0
            terminal_exits[symbol] = abs(weight)
        else:
            returns[symbol] = float(row["next_execution_open"]) / entry - 1.0
    total_turnover = base_turnover + sum(terminal_exits.values())
    total_cost = total_turnover * BASE_COST
    gross = sum(target[symbol] * returns[symbol] for symbol in target)
    return {
        "decision_timestamp": decision_timestamp,
        "momentum_value": scores,
        "volatility_regime_value": {
            "state": state, "broad_statistic": statistic, "volatility_window": volatility_window,
        },
        "signal": signal,
        "target_position": target,
        "execution_timestamp": execution_timestamp,
        "turnover": total_turnover,
        "cost": total_cost,
        "next_period_return": returns,
        "portfolio_pnl": {"gross": gross, "net": gross - total_cost},
    }


def _assert_close(actual: float, expected: float, field: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
        raise Expl017Error(f"PROCESS_DEFECT: gold mismatch {field}: {actual} != {expected}")


def replay_gold_sample(path: pathlib.Path = GOLD_PATH) -> list[dict[str, object]]:
    """Validate every stated gold field, including terminal exit turnover/cost."""
    results = []
    for case in _gold_payload(path)["cases"]:
        actual = gold_case_result(case)
        for field in ("decision_timestamp", "signal", "execution_timestamp"):
            if actual[field] != case[field]:
                raise Expl017Error(f"PROCESS_DEFECT: gold mismatch {case['case']}:{field}")
        for field in ("momentum_value", "target_position", "next_period_return"):
            for symbol, expected in case[field].items():
                # The terminal gold label is deliberately explanatory rather
                # than a traded symbol; bind it to the same held E return.
                actual_symbol = symbol.removesuffix("_terminal_open_to_close")
                _assert_close(actual[field][actual_symbol], expected, f"{case['case']}:{field}:{symbol}")
        expected_vol = case["volatility_regime_value"]
        if actual["volatility_regime_value"]["state"] != expected_vol["state"]:
            raise Expl017Error(f"PROCESS_DEFECT: gold mismatch {case['case']}:regime")
        _assert_close(actual["volatility_regime_value"]["broad_statistic"], expected_vol["broad_statistic"], f"{case['case']}:broad_vol")
        if actual["volatility_regime_value"]["volatility_window"] != case["parameters"]["volatility_window"]:
            raise Expl017Error(f"PROCESS_DEFECT: gold mismatch {case['case']}:volatility_window")
        for field in ("turnover", "cost"):
            _assert_close(actual[field], case[field], f"{case['case']}:{field}")
        for field in ("gross", "net"):
            _assert_close(actual["portfolio_pnl"][field], case["portfolio_pnl"][field], f"{case['case']}:pnl:{field}")
        results.append(actual)
    return results


def validate_dataset(data_root: pathlib.Path = DATA_ROOT) -> dict[str, object]:
    """Replay identity, fields, PIT, lifecycle, and time-containment admission only."""
    try:
        dataset = load_dataset(data_root)
    except PriceAlphaError as error:
        raise Expl017Error(f"DATA_UNAVAILABLE: {error}") from error
    admission = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    required = admission["dataset"]
    if (dataset.manifest_sha256 != MANIFEST_SHA256 or dataset.pit_sha256 != PIT_SHA256
            or dataset.manifest_sha256 != required["manifest_sha256"]
            or dataset.pit_sha256 != required["pit_sha256"]):
        raise Expl017Error("DATA_UNAVAILABLE: frozen Price V1 identity differs")
    if set(dataset.labels) != set(required["labels"]) or len(dataset.bars) != required["symbols"]:
        raise Expl017Error("DATA_UNAVAILABLE: frozen Price V1 summary differs")
    start = int(dt.datetime(2020, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
    end = int(dt.datetime(2023, 12, 31, tzinfo=dt.UTC).timestamp() * 1000)
    # Terminal contracts may have a shorter observable life (and some were
    # listed after 2020); the admission requires aggregate coverage from 2020
    # through 2023 and prohibits any later bar, not a fictional full history
    # for every symbol.
    if (min(min(points) for points in dataset.bars.values()) != start
            or max(max(points) for points in dataset.bars.values()) != end
            or any(max(points) > end for points in dataset.bars.values())):
        raise Expl017Error("DATA_UNAVAILABLE: price coverage violates EXPL-017 containment")
    if len(dataset.pit) != admission["time_range"]["pit_records"]:
        raise Expl017Error("DATA_UNAVAILABLE: PIT record count differs")
    return {"snapshot_id": SNAPSHOT_ID, "manifest_sha256": dataset.manifest_sha256,
            "pit_sha256": dataset.pit_sha256, "symbols": len(dataset.bars),
            "classification_on_failure": "DATA_UNAVAILABLE"}


def segment_for_execution(execution: dt.date) -> str:
    """The frozen split map; this does not inspect or evaluate any returns."""
    if not FORMAL_START <= execution <= FORMAL_END:
        raise Expl017Error("PROCESS_DEFECT: execution outside formal containment")
    if execution <= TRAIN_END:
        return "train"
    if execution <= OOS_END:
        return "oos"
    return "final_holdout"


def formal_run(*_args: object, **_kwargs: object) -> None:
    """Fail closed until a separately committed formal freeze binds this runner."""
    raise FormalRunLocked("FORMAL_RUN_LOCKED: EXPL-017 needs a separate committed freeze")


def correctness_check(data_root: pathlib.Path = DATA_ROOT) -> dict[str, object]:
    return {"experiment_id": EXPERIMENT_ID, "dataset": validate_dataset(data_root),
            "gold_cases": len(replay_gold_sample()), "formal_run": "LOCKED"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["correctness"]):
        raise SystemExit("usage: expl_017.py [correctness]")
    correctness_check()
    print("EXPL-017 correctness checks: PASS; formal run remains LOCKED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
