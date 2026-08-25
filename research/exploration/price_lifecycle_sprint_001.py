#!/usr/bin/env python3
"""Frozen Tier-1 runner for Price/Lifecycle Sprint 001.

This module deliberately contains no current-exchange access and does not use
Funding or open interest.  ``run_program`` is deterministic once supplied a
Price V1 dataset, the pinned PIT master, and the committed preregistration.
The command-line entrypoint is intentionally explicit about both candidate and
output path so a research run is an auditable action rather than an import
side effect.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.data.pit_instrument_master_v1 import (  # noqa: E402
    InstrumentMasterError,
    load_master,
    universe_at,
)
from research.exploration.price_alpha_v1 import (  # noqa: E402
    DAY_MS,
    MANIFEST_SHA256 as PRICE_MANIFEST_SHA256,
    PIT_SHA256 as PRICE_PIT_SHA256,
    SNAPSHOT_ID as PRICE_SNAPSHOT_ID,
    Bar,
    PriceAlphaError,
    PriceDataset,
    hac_mean_test,
    load_dataset,
    normalized_ranks,
    pearson,
    sha256_file,
)


PROGRAM_ID = "PRICE_LIFECYCLE_SPRINT_001"
PREREGISTRATION_PATH = ROOT / "research/exploration/price-lifecycle-sprint-001-preregistration.json"
BASE_COST = 0.0015
STRESS_COST = 0.0030
SUPPORT_END = "2023-11-14T00:00:00Z"


class SprintDataError(RuntimeError):
    """A frozen data, timing, or lifecycle contract was not satisfied."""


@dataclass(frozen=True)
class Config:
    candidate_id: str
    configuration_id: str
    family: str
    short_window: int | None
    long_window: int | None
    sigma_window: int | None
    cadence_days: int
    first_execution: int
    final_execution: int
    final_exit: int


@dataclass(frozen=True)
class SignalEvent:
    execution: int
    decision: int
    target: dict[str, float]
    signal: dict[str, float]
    forward: dict[str, float]


@dataclass
class Simulation:
    dates: list[int]
    gross: list[float]
    turnover: list[float]
    rebalance_turnover: list[float]
    gross_contribution: list[dict[str, float]]
    terminal_liquidations: list[dict[str, str]]


def _timestamp(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise SprintDataError(f"DATA_ERROR_STOP: non-UTC frozen timestamp {value}")
    return int(parsed.timestamp() * 1000)


def _iso(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def load_preregistration(path: pathlib.Path = PREREGISTRATION_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SprintDataError("DATA_ERROR_STOP: preregistration unreadable") from error
    if payload.get("program_id") != PROGRAM_ID:
        raise SprintDataError("DATA_ERROR_STOP: preregistration identity differs")
    return payload


def _pinned_paths(preregistration: Mapping[str, Any]) -> dict[pathlib.Path, str]:
    try:
        identity = preregistration["data_contract"]["pit_lifecycle_identity"]
        price = preregistration["data_contract"]["price_identity"]
    except (KeyError, TypeError) as error:
        raise SprintDataError("DATA_ERROR_STOP: preregistration identity malformed") from error
    pairs = {
        identity["instrument_master_path"]: identity["instrument_master_sha256"],
        identity["price_activity_path"]: identity["price_activity_sha256"],
        identity["lifecycle_sidecar_path"]: identity["lifecycle_sidecar_sha256"],
        identity["supplemental_terminal_evidence_path"]: identity["supplemental_terminal_evidence_sha256"],
        identity["price_lifecycle_composite_path"]: identity["price_lifecycle_composite_sha256"],
    }
    if price.get("snapshot_id") != PRICE_SNAPSHOT_ID:
        raise SprintDataError("DATA_ERROR_STOP: frozen Price V1 snapshot identity differs")
    if price.get("manifest_sha256") != PRICE_MANIFEST_SHA256:
        raise SprintDataError("DATA_ERROR_STOP: frozen Price V1 manifest identity differs")
    if price.get("pit_sha256") != PRICE_PIT_SHA256:
        raise SprintDataError("DATA_ERROR_STOP: frozen Price V1 PIT identity differs")
    if price.get("price_v1_pit_usage") != (
        "identity and loader validation only; sprint membership comes exclusively from the bounded PIT instrument master universe_at interface"
    ):
        raise SprintDataError("DATA_ERROR_STOP: Price V1 PIT usage is not identity-only")
    output: dict[pathlib.Path, str] = {}
    for relative, expected in pairs.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise SprintDataError("DATA_ERROR_STOP: pinned evidence path escapes repository")
        output[path] = expected
    return output


def verify_pinned_files(preregistration: Mapping[str, Any]) -> None:
    for path, expected in _pinned_paths(preregistration).items():
        if not path.is_file() or sha256_file(path) != expected:
            raise SprintDataError(f"DATA_ERROR_STOP: pinned identity differs for {path}")


def load_frozen_inputs(
    preregistration: Mapping[str, Any], data_root: pathlib.Path | None = None
) -> tuple[PriceDataset, dict[str, Any]]:
    """Load only frozen Price V1 bars and the deterministic instrument master."""
    verify_pinned_files(preregistration)
    try:
        dataset = load_dataset() if data_root is None else load_dataset(data_root)
        master = load_master()
    except (PriceAlphaError, InstrumentMasterError, OSError, ValueError) as error:
        raise SprintDataError(f"DATA_ERROR_STOP: frozen input validation failed: {error}") from error
    return dataset, master


def _configurations(preregistration: Mapping[str, Any]) -> dict[str, Config]:
    schedules = {
        item["configuration_id"]: item
        for item in preregistration["common_execution_and_accounting"]["frozen_schedules"]
    }
    output: dict[str, Config] = {}
    for candidate in preregistration["candidates"]:
        candidate_id = candidate["candidate_id"]
        primary = candidate["hypothesis_id"]
        ids = [primary] + [item["variant_id"] for item in candidate["sanity_variants"]]
        for configuration_id in ids:
            schedule = schedules.get(configuration_id)
            if schedule is None:
                raise SprintDataError(f"DATA_ERROR_STOP: missing frozen schedule {configuration_id}")
            if primary == "HYP-PLS001-001":
                sigma = 15 if configuration_id.endswith("-V1") else 20
                family, short, long = "shock_reversal", None, None
            elif primary == "HYP-PLS001-002":
                short = 5 if configuration_id.endswith("-V1") else 7
                long = 21 if configuration_id.endswith("-V2") else 28
                family, sigma = "volume_share", None
            else:
                raise SprintDataError(f"DATA_ERROR_STOP: unsupported candidate {primary}")
            config = Config(
                candidate_id=candidate_id,
                configuration_id=configuration_id,
                family=family,
                short_window=short,
                long_window=long,
                sigma_window=sigma,
                cadence_days=int(schedule["cadence_calendar_days"]),
                first_execution=_timestamp(schedule["first_execution_utc"]),
                final_execution=_timestamp(schedule["final_execution_utc"]),
                final_exit=_timestamp(schedule["final_exit_utc"]),
            )
            if config.final_execution + config.cadence_days * DAY_MS != config.final_exit:
                raise SprintDataError(f"DATA_ERROR_STOP: final horizon differs for {configuration_id}")
            if config.final_exit >= _timestamp(SUPPORT_END):
                raise SprintDataError(f"DATA_ERROR_STOP: final exit outside support for {configuration_id}")
            output[configuration_id] = config
    return output


def _bar(dataset: PriceDataset, symbol: str, timestamp: int) -> Bar:
    try:
        value = dataset.bars[symbol][timestamp]
    except KeyError as error:
        raise SprintDataError(f"DATA_ERROR_STOP: required bar absent {symbol}:{_iso(timestamp)}") from error
    if not all(math.isfinite(item) for item in (value.open, value.close, value.quote_volume)):
        raise SprintDataError(f"DATA_ERROR_STOP: non-finite bar {symbol}:{_iso(timestamp)}")
    if value.open <= 0 or value.close <= 0 or value.quote_volume < 0:
        raise SprintDataError(f"DATA_ERROR_STOP: invalid bar {symbol}:{_iso(timestamp)}")
    return value


def _execution_open(dataset: PriceDataset, symbol: str, timestamp: int) -> float:
    """Validate only information observable at the execution boundary."""
    try:
        value = dataset.bars[symbol].get(timestamp)
    except KeyError as error:
        raise SprintDataError(
            f"DATA_ERROR_STOP: execution series absent {symbol}:{_iso(timestamp)}"
        ) from error
    if value is None or not math.isfinite(value.open) or value.open <= 0:
        raise SprintDataError(
            f"DATA_ERROR_STOP: required execution open invalid {symbol}:{_iso(timestamp)}"
        )
    return value.open


def _universe(master: Mapping[str, Any], timestamp: int) -> tuple[str, ...]:
    try:
        members = universe_at(master, _iso(timestamp))
    except InstrumentMasterError as error:
        raise SprintDataError(f"DATA_ERROR_STOP: master membership unavailable: {error}") from error
    if members != tuple(sorted(set(members))):
        raise SprintDataError("DATA_ERROR_STOP: master returned malformed universe")
    return members


def _target(signal: Mapping[str, float], *, minimum: int) -> dict[str, float]:
    if len(signal) < minimum:
        raise SprintDataError(f"DATA_ERROR_STOP: fewer than {minimum} eligible symbols")
    ordered = sorted(signal, key=lambda symbol: (signal[symbol], symbol))
    leg = max(1, math.floor(len(ordered) * 0.20))
    result = {symbol: 0.5 / leg for symbol in ordered[-leg:]}
    result.update({symbol: -0.5 / leg for symbol in ordered[:leg]})
    return result


def _terminal_timestamp(master: Mapping[str, Any], symbol: str) -> int | None:
    for record in master.get("records", []):
        if record.get("symbol") == symbol:
            value = record.get("terminal_timestamp_utc")
            return _timestamp(value) if isinstance(value, str) else None
    raise SprintDataError(f"DATA_ERROR_STOP: symbol absent from master {symbol}")


def _forward_return(
    dataset: PriceDataset, master: Mapping[str, Any], symbol: str, execution: int, exit_at: int
) -> float:
    entry = _execution_open(dataset, symbol, execution)
    terminal = _terminal_timestamp(master, symbol)
    if terminal is not None and execution <= terminal < exit_at:
        final_day = terminal // DAY_MS * DAY_MS
        return _bar(dataset, symbol, final_day).close / entry - 1.0
    return _bar(dataset, symbol, exit_at).open / entry - 1.0


def build_events(
    dataset: PriceDataset,
    master: Mapping[str, Any],
    config: Config,
    *,
    excluded_symbol: str | None = None,
) -> list[SignalEvent]:
    events: list[SignalEvent] = []
    for execution in range(config.first_execution, config.final_execution + 1, config.cadence_days * DAY_MS):
        decision = execution - DAY_MS
        if execution + config.cadence_days * DAY_MS > config.final_exit:
            raise SprintDataError("DATA_ERROR_STOP: partial holding horizon")
        members = _universe(master, execution - 1)
        execution_members = set(_universe(master, execution))
        signal: dict[str, float] = {}
        volume_sums: dict[str, tuple[float, float]] = {}
        for symbol in members:
            if symbol == excluded_symbol:
                continue
            # A lifecycle change between completed decision close and the next
            # UTC open is observable at the execution boundary; do not enter.
            if symbol not in execution_members:
                continue
            # Price inputs are required for every master member at a decision;
            # volume uses zero as the only explicitly permitted eligibility filter.
            _bar(dataset, symbol, decision)
            _execution_open(dataset, symbol, execution)
            if config.family == "shock_reversal":
                assert config.sigma_window is not None
                closes = [_bar(dataset, symbol, decision - offset * DAY_MS).close for offset in range(config.sigma_window + 1)]
                returns = [closes[index] / closes[index + 1] - 1.0 for index in range(config.sigma_window)]
                sigma = statistics.stdev(returns)
                if not math.isfinite(sigma) or sigma == 0:
                    continue
                signal[symbol] = -(closes[0] / closes[1] - 1.0) / sigma
            else:
                assert config.short_window is not None and config.long_window is not None
                values = [_bar(dataset, symbol, decision - offset * DAY_MS).quote_volume for offset in range(config.long_window)]
                if any(value <= 0 or not math.isfinite(value) for value in values):
                    continue
                # Stored order is t, t-1, ...; summation is order independent.
                short_sum = sum(values[: config.short_window])
                long_sum = sum(values)
                volume_sums[symbol] = (short_sum, long_sum)
        if config.family == "volume_share":
            raw = volume_sums
            signal = {
                symbol: math.log(float(pair[0]) / sum(float(value[0]) for value in raw.values()))
                - math.log(float(pair[1]) / sum(float(value[1]) for value in raw.values()))
                for symbol, pair in raw.items()
            }
        target = _target(signal, minimum=10 if config.family == "shock_reversal" else 20)
        forward = {symbol: _forward_return(dataset, master, symbol, execution, execution + config.cadence_days * DAY_MS) for symbol in signal}
        events.append(SignalEvent(execution, decision, target, signal, forward))
    return events


def _is_terminal_today(master: Mapping[str, Any], symbol: str, current: int) -> bool:
    terminal = _terminal_timestamp(master, symbol)
    return terminal is not None and current <= terminal < current + DAY_MS


def simulate(
    dataset: PriceDataset,
    master: Mapping[str, Any],
    config: Config,
    events: Sequence[SignalEvent],
    *,
    cost_rate: float = STRESS_COST,
) -> Simulation:
    by_execution = {event.execution: event for event in events}
    expected = list(range(config.first_execution, config.final_execution + DAY_MS, config.cadence_days * DAY_MS))
    if sorted(by_execution) != expected:
        raise SprintDataError("DATA_ERROR_STOP: event schedule differs from freeze")
    held: dict[str, float] = {}
    dates: list[int] = []
    gross_values: list[float] = []
    turnover_values: list[float] = []
    rebalance_turnover: list[float] = []
    contributions: list[dict[str, float]] = []
    terminal_liquidations: list[dict[str, str]] = []
    for current in range(config.first_execution, config.final_exit, DAY_MS):
        turnover = 0.0
        scheduled_turnover: float | None = None
        if current in by_execution:
            target = by_execution[current].target
            turnover = sum(abs(target.get(symbol, 0.0) - held.get(symbol, 0.0)) for symbol in sorted(set(held) | set(target)))
            scheduled_turnover = turnover
            held = dict(target)
        daily: dict[str, float] = {}
        terminal_symbols: list[str] = []
        for symbol, weight in sorted(held.items()):
            now = _bar(dataset, symbol, current)
            if _is_terminal_today(master, symbol, current):
                asset_return = now.close / now.open - 1.0
                terminal_symbols.append(symbol)
            else:
                asset_return = _bar(dataset, symbol, current + DAY_MS).open / now.open - 1.0
            daily[symbol] = weight * asset_return
        for symbol in terminal_symbols:
            asset_return = daily[symbol] / held[symbol]
            turnover += abs(held[symbol] * (1.0 + asset_return))
            terminal_liquidations.append({"symbol": symbol, "date": _iso(current), "method": "canonical_terminal_open_to_final_close_then_exit"})
            del held[symbol]
        gross = sum(daily.values())
        net = gross - cost_rate * turnover
        if net <= -1.0:
            raise SprintDataError("DATA_ERROR_STOP: net return reached or crossed -100%")
        denominator = 1.0 + net
        held = {symbol: weight * (1.0 + daily.get(symbol, 0.0) / weight) / denominator for symbol, weight in held.items()}
        dates.append(current)
        gross_values.append(gross)
        turnover_values.append(turnover)
        if scheduled_turnover is not None:
            # Terminal and final-exit liquidation costs are deliberately not a
            # scheduled-rebalance observation for the median turnover rule.
            rebalance_turnover.append(scheduled_turnover)
        contributions.append(daily)
    # Frozen final exit is a cost-only observation, booked against drifted weights.
    final_turnover = sum(abs(weight) for weight in held.values())
    dates.append(config.final_exit)
    gross_values.append(0.0)
    turnover_values.append(final_turnover)
    contributions.append({})
    return Simulation(dates, gross_values, turnover_values, rebalance_turnover, contributions, terminal_liquidations)


def _metrics(returns: Sequence[float], turnover: Sequence[float]) -> dict[str, float | int]:
    if len(returns) < 2:
        raise SprintDataError("DATA_ERROR_STOP: metric window has fewer than two returns")
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    mean = statistics.mean(returns)
    deviation = statistics.stdev(returns)
    return {
        "days": len(returns),
        "total_return": equity - 1.0,
        "mean_daily_return": mean,
        "annualized_sharpe": mean / deviation * math.sqrt(365.0) if deviation else 0.0,
        "maximum_drawdown": drawdown,
        "turnover_total": sum(turnover),
    }


def _rank_ic(events: Iterable[SignalEvent]) -> dict[str, object]:
    ics: list[float] = []
    for event in events:
        symbols = sorted(set(event.signal) & set(event.forward))
        left = normalized_ranks({symbol: event.signal[symbol] for symbol in symbols})
        right = normalized_ranks({symbol: event.forward[symbol] for symbol in symbols})
        value = pearson([left[symbol] for symbol in symbols], [right[symbol] for symbol in symbols])
        if value is None:
            raise SprintDataError("DATA_ERROR_STOP: unavailable rank IC")
        ics.append(value)
    if not ics:
        raise SprintDataError("DATA_ERROR_STOP: no rank IC events")
    return {"event_ics": ics, "mean_rank_ic": statistics.mean(ics), "hac_lag_3": hac_mean_test(ics, lag=3)}


def _contribution(simulation: Simulation) -> tuple[dict[str, float], dict[str, object]]:
    totals: dict[str, float] = {}
    for daily in simulation.gross_contribution:
        for symbol, value in daily.items():
            totals[symbol] = totals.get(symbol, 0.0) + value
    totals = dict(sorted(totals.items()))
    denominator = sum(abs(value) for value in totals.values())
    largest = min(
        (symbol for symbol, value in totals.items() if value > 0),
        key=lambda symbol: (-totals[symbol], symbol),
        default=None,
    )
    absolute_largest = min(totals, key=lambda symbol: (-abs(totals[symbol]), symbol), default=None)
    return totals, {
        "largest_strictly_positive_symbol": largest,
        "largest_absolute_symbol": absolute_largest,
        "largest_absolute_share": abs(totals[absolute_largest]) / denominator if absolute_largest and denominator else 0.0,
    }


def _net(simulation: Simulation, cost: float) -> list[float]:
    return [gross - cost * turnover for gross, turnover in zip(simulation.gross, simulation.turnover)]


def evaluate_configuration(
    dataset: PriceDataset,
    master: Mapping[str, Any],
    config: Config,
    *,
    excluded_symbol: str | None = None,
) -> dict[str, object]:
    events = build_events(dataset, master, config, excluded_symbol=excluded_symbol)
    stress_simulation = simulate(dataset, master, config, events, cost_rate=STRESS_COST)
    base_simulation = simulate(dataset, master, config, events, cost_rate=BASE_COST)
    stress = _net(stress_simulation, STRESS_COST)
    base = _net(base_simulation, BASE_COST)
    contribution, concentration = _contribution(stress_simulation)
    subperiods = []
    for definition in (
        ("2021-02-01T00:00:00Z", "2022-01-01T00:00:00Z"),
        ("2022-01-01T00:00:00Z", "2023-01-01T00:00:00Z"),
        ("2023-01-01T00:00:00Z", "2023-11-14T00:00:00Z"),
    ):
        left, right = (_timestamp(value) for value in definition)
        indices = [
            index
            for index, date in enumerate(stress_simulation.dates)
            if left <= date < right
        ]
        if len(indices) < 2:
            raise SprintDataError("DATA_ERROR_STOP: insufficient frozen subperiod")
        subperiods.append(
            {
                "range": f"{definition[0]} to {definition[1]}",
                "stress": _metrics(
                    [stress[index] for index in indices],
                    [stress_simulation.turnover[index] for index in indices],
                ),
            }
        )
    return {
        "configuration_id": config.configuration_id,
        "candidate_id": config.candidate_id,
        "data_semantics": "Price V1 bars only; membership exclusively master universe_at; net excludes funding and includes frozen trading-cost proxy only",
        "metric_window": {"start": _iso(config.first_execution), "end": _iso(config.final_exit)},
        "base_15bps": _metrics(base, base_simulation.turnover),
        "stress_30bps": _metrics(stress, stress_simulation.turnover),
        "median_one_way_turnover_per_rebalance": statistics.median(
            stress_simulation.rebalance_turnover
        ),
        "rank_ic": _rank_ic(events),
        "gross_symbol_contribution": contribution,
        "concentration": concentration,
        "subperiods": subperiods,
        "terminal_liquidations": stress_simulation.terminal_liquidations,
    }


def _candidate_result(dataset: PriceDataset, master: Mapping[str, Any], configs: Mapping[str, Config], candidate_id: str) -> dict[str, object]:
    selected = sorted((config for config in configs.values() if config.candidate_id == candidate_id), key=lambda item: item.configuration_id)
    primary = next(config for config in selected if config.configuration_id in {"HYP-PLS001-001", "HYP-PLS001-002"})
    primary_result = evaluate_configuration(dataset, master, primary)
    variants = [evaluate_configuration(dataset, master, config) for config in selected if config != primary]
    concentration = primary_result["concentration"]
    assert isinstance(concentration, Mapping)
    removed = concentration["largest_strictly_positive_symbol"]
    removal: dict[str, object]
    if not isinstance(removed, str):
        removal = {"status": "FAIL_NO_STRICTLY_POSITIVE_CONTRIBUTOR"}
    else:
        rerun = evaluate_configuration(dataset, master, primary, excluded_symbol=removed)
        stress = rerun["stress_30bps"]
        assert isinstance(stress, Mapping)
        removal = {"removed_symbol": removed, "stress_mean_daily_return": stress["mean_daily_return"], "passes": stress["mean_daily_return"] >= 0.0}
    stress = primary_result["stress_30bps"]
    rank_ic = primary_result["rank_ic"]
    assert isinstance(stress, Mapping) and isinstance(rank_ic, Mapping)
    hac = rank_ic["hac_lag_3"]
    assert isinstance(hac, Mapping)
    subperiods = primary_result["subperiods"]
    nonnegative = sum(item["stress"]["total_return"] >= 0.0 for item in subperiods)  # type: ignore[index]
    variants_ok = all(
        item["stress_30bps"]["mean_daily_return"] > 0 and item["rank_ic"]["mean_rank_ic"] > 0  # type: ignore[index]
        for item in variants
    )
    checks = {
        "positive_stress_mean": stress["mean_daily_return"] > 0,
        "sharpe_at_least_0_50": stress["annualized_sharpe"] >= 0.50,
        "positive_rank_ic": rank_ic["mean_rank_ic"] > 0,
        "hac_p_at_most_0_05": hac["p_one_sided_normal"] is not None and hac["p_one_sided_normal"] <= 0.05,
        "two_nonnegative_subperiods": nonnegative >= 2,
        "drawdown_no_worse_than_minus_0_35": stress["maximum_drawdown"] >= -0.35,
        "concentration_at_most_0_20": primary_result["concentration"]["largest_absolute_share"] <= 0.20,  # type: ignore[index]
        "single_positive_removal": removal.get("passes") is True,
        "median_turnover_at_most_1_25": primary_result["median_one_way_turnover_per_rebalance"] <= 1.25,
        "both_variants_positive": variants_ok,
        "lifecycle_universe_missingness_checks": True,
    }
    return {
        "candidate_id": candidate_id,
        "primary": primary_result,
        "variants": variants,
        "single_symbol_removal": removal,
        "pass_checks": checks,
        "tier1_status": (
            "MECHANISM_WORTH_CONFIRMING" if all(checks.values()) else "TIER1_FAIL"
        ),
    }


def run_program(
    dataset: PriceDataset,
    master: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    *,
    candidate: str = "all",
) -> dict[str, object]:
    configs = _configurations(preregistration)
    candidates = sorted(preregistration["candidates"], key=lambda item: item["order"])
    if candidate != "all":
        raise SprintDataError(
            "DATA_ERROR_STOP: candidate selection would bypass frozen sequential order"
        )
    results = []
    for item in candidates:
        candidate_id = item["candidate_id"]
        result = _candidate_result(dataset, master, configs, candidate_id)
        results.append(result)
        if result["tier1_status"] == "MECHANISM_WORTH_CONFIRMING":
            break
    first_pass_candidate = next(
        (
            item["candidate_id"]
            for item in results
            if item["tier1_status"] == "MECHANISM_WORTH_CONFIRMING"
        ),
        None,
    )
    order_by_candidate = {item["candidate_id"]: item["order"] for item in candidates}
    return {
        "program_id": PROGRAM_ID,
        "candidate_selection": candidate,
        "result_semantics": "Tier 1 exploration only; no formal strategy, runtime, live, Funding, or open interest claim",
        "results": results,
        "first_pass_candidate": first_pass_candidate,
        "data_identity": {
            "price_snapshot_id": PRICE_SNAPSHOT_ID,
            "price_snapshot_artifact_path": str(dataset.artifact_path),
            "price_manifest_sha256": dataset.manifest_sha256,
            "price_pit_sha256_identity_only": dataset.pit_sha256,
            "pit_lifecycle_identity": preregistration["data_contract"][
                "pit_lifecycle_identity"
            ],
            "cohort_id": preregistration["data_contract"]["cohort_id"],
            "support_start_inclusive_utc": preregistration["data_contract"][
                "support_start_inclusive_utc"
            ],
            "support_end_exclusive_utc": preregistration["data_contract"][
                "support_end_exclusive_utc"
            ],
        },
        "program_accounting": {
            "candidates_preregistered": len(candidates),
            "candidates_tested": len(results),
            "pass_count": sum(
                item["tier1_status"] == "MECHANISM_WORTH_CONFIRMING"
                for item in results
            ),
            "fail_count": sum(
                item["tier1_status"] == "TIER1_FAIL" for item in results
            ),
            "data_windows_viewed": 4 * len(results),
            "variants_viewed": 2 * len(results),
            "configurations_viewed": 3 * len(results),
            "late_program_pass": (
                first_pass_candidate is not None
                and order_by_candidate[first_pass_candidate] > 1
            ),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="all", help="all or one preregistered candidate id")
    parser.add_argument("--output", type=pathlib.Path, required=True, help="new JSON artifact path")
    parser.add_argument("--data-root", type=pathlib.Path, default=None)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("refusing to overwrite an existing output artifact")
    preregistration = load_preregistration()
    dataset, master = load_frozen_inputs(preregistration, args.data_root)
    result = run_program(dataset, master, preregistration, candidate=args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(_canonical_json(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
