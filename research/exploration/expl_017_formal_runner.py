"""The one-shot EXPL-017-FORMAL-003 entry point.

It intentionally has no import-time side effects.  Calling ``run`` is the
formal evaluation: it reads the frozen Price V1 snapshot, computes metrics and
writes the one immutable result artifact.  Tests exercise only ``summarize``
with synthetic, already-produced consumer output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
import statistics
from typing import Any, Mapping, Sequence

import expl_017 as core
import expl_017_formal_consumer as consumer


FORMAL_RUN_ID = "EXPL-017-FORMAL-003"
FREEZE_PATH = pathlib.Path(__file__).with_name("expl-017-formal-003-freeze.json")
REVIEW_PATH = pathlib.Path(__file__).with_name("expl-017-formal-003-contract-review.json")


class FormalRunnerError(core.PreFormalError):
    """The frozen one-shot entry cannot safely proceed."""


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_freeze(path: pathlib.Path = FREEZE_PATH) -> Mapping[str, Any]:
    """Load and verify every code artifact the formal entry will consume."""
    try:
        freeze = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormalRunnerError("formal freeze unreadable") from error
    if freeze.get("formal_run_id") != FORMAL_RUN_ID:
        raise FormalRunnerError("wrong formal run identity")
    if freeze.get("status") != "FROZEN_AWAITING_INDEPENDENT_CONTRACT_REVIEW":
        raise FormalRunnerError("formal freeze is not eligible")
    identity = freeze.get("identity")
    if not isinstance(identity, Mapping):
        raise FormalRunnerError("formal identity missing")
    root = pathlib.Path(__file__).resolve().parents[2]
    for name in ("lifecycle_v1", "composite", "gold_oracle", "reviewed_core", "formal_consumer", "horizon_preflight", "formal_runner"):
        item = identity.get(name)
        if not isinstance(item, Mapping):
            raise FormalRunnerError(f"formal {name} identity missing")
        key = (
            "generator_sha256" if name == "horizon_preflight"
            else "dataset_sha256" if name == "lifecycle_v1"
            else "sha256"
        )
        file_key = "generator_path" if name == "horizon_preflight" else "path"
        if not isinstance(item.get(file_key), str) or item.get(key) != _sha256(root / item[file_key]):
            raise FormalRunnerError(f"formal {name} identity differs")
    if identity["formal_consumer"]["sha256"] != _sha256(pathlib.Path(consumer.__file__)):
        raise FormalRunnerError("loaded formal consumer differs")
    return freeze


def require_independent_approval(freeze_path: pathlib.Path = FREEZE_PATH, review_path: pathlib.Path = REVIEW_PATH) -> None:
    """Require a separate immutable reviewer artifact, not a caller-supplied flag."""
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormalRunnerError("independent formal contract approval absent") from error
    if not (
        review.get("formal_run_id") == FORMAL_RUN_ID
        and review.get("approve") is True
        and review.get("reviewed_freeze_contract_sha256") == _sha256(freeze_path)
    ):
        raise FormalRunnerError("independent formal contract approval invalid")


def _timestamp(value: object) -> int:
    date = dt.date.fromisoformat(str(value))
    return int(dt.datetime.combine(date, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)


def frozen_schedule() -> tuple[consumer.HorizonContract, ...]:
    import expl_017_horizon_preflight as horizon

    return tuple(
        consumer.HorizonContract(
            _timestamp(row["decision"]), _timestamp(row["execution"]),
            _timestamp(row["endpoint"]), str(row["split"]), bool(row["ic_included"])
        )
        for row in horizon.build_rows()
    )


def _split(timestamp: int) -> str | None:
    year = dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC).year
    return {2021: "train", 2022: "oos", 2023: "holdout"}.get(year)


def _summary(values: Sequence[float]) -> Mapping[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "t_stat": None}
    mean = statistics.mean(values)
    if len(values) < 2:
        t_stat = None
    else:
        deviation = statistics.stdev(values)
        t_stat = None if deviation == 0 else mean / (deviation / math.sqrt(len(values)))
    return {"count": len(values), "mean": mean, "t_stat": t_stat}


def _period(timestamp: int) -> str | None:
    stamp = dt.datetime.fromtimestamp(timestamp / 1000, tz=dt.UTC)
    if stamp.year == 2021:
        return "train"
    if stamp.year == 2022:
        return "oos_h1" if stamp.month <= 6 else "oos_h2"
    if stamp.year == 2023:
        return "holdout_h1" if stamp.month <= 6 else "holdout_h2"
    return None


def _returns(ledger: Sequence[core.AccountingEntry]) -> tuple[tuple[int, float], ...]:
    opens = [entry for entry in ledger if entry.phase == "OPEN"]
    if len(opens) < 2:
        raise FormalRunnerError("insufficient continuous accounting")
    output = [(previous.timestamp, current.nav / previous.nav - 1.0) for previous, current in zip(opens, opens[1:])]
    final = next((entry for entry in ledger if entry.phase == "FINAL_CLOSE"), None)
    if final is not None:
        output.append((opens[-1].timestamp, final.nav / opens[-1].nav - 1.0))
    return tuple(output)


def _path_metrics(ledger: Sequence[core.AccountingEntry], periods: set[str]) -> Mapping[str, float | int | None]:
    returns: list[float] = []
    for timestamp, value in _returns(ledger):
        current = _period(timestamp)
        split = current.split("_")[0] if current else None
        if current in periods or split in periods:
            returns.append(value)
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    sharpe = None if stdev == 0 else statistics.mean(returns) / stdev * math.sqrt(365)
    return {"return": equity - 1.0, "sharpe": sharpe, "max_drawdown": drawdown, "daily_count": len(returns)}


def _contributions(outcome: consumer.ConsumerSchedule, schedule: Sequence[consumer.HorizonContract]) -> Mapping[str, Any]:
    """Attribute realized baseline ledger changes without rereading prices.

    Rebalance entries use the reviewed pre-trade incumbent state, so target
    notional transfers cannot masquerade as symbol profit and loss.  Terminal
    and final exits use the reviewed Exit amount/cost pair.
    """
    ledger = outcome.accounting[core.COST]
    opens = [entry for entry in ledger if entry.phase == "OPEN"]
    states = {contract.execution_ms: item.states[core.COST] for item, contract in zip(outcome.decisions, schedule)}
    contribution: dict[str, float] = {}
    lifecycle_events: list[float] = []
    daily_absolute: list[float] = []

    def add(symbol: str, value: float, daily: dict[str, float]) -> None:
        if not math.isfinite(value):
            raise FormalRunnerError("non-finite contribution")
        contribution[symbol] = contribution.get(symbol, 0.0) + value
        daily[symbol] = daily.get(symbol, 0.0) + value

    def interval(previous: core.AccountingEntry, current: core.AccountingEntry) -> None:
        if _split(previous.timestamp) not in {"oos", "holdout"}:
            return
        before = dict(previous.notionals)
        daily: dict[str, float] = {}
        state = states.get(current.timestamp)
        pretrade = (
            {symbol: weight * state.nav_before_trade for symbol, weight in state.incumbent.items()}
            if state is not None else dict(current.notionals)
        )
        exits = {item.symbol: item for item in current.exits}
        for symbol in set(before) | set(pretrade) | set(exits):
            if symbol in exits:
                exit = exits[symbol]
                sign = 1.0 if before.get(symbol, 0.0) >= 0 else -1.0
                value = sign * exit.nav_before * exit.turnover - before.get(symbol, 0.0) - exit.cost
                add(symbol, value, daily)
                lifecycle_events.append(value)
            else:
                add(symbol, pretrade.get(symbol, 0.0) - before.get(symbol, 0.0), daily)
        if state is not None and state.cost:
            changes = {symbol: abs(state.target.get(symbol, 0.0) - state.incumbent.get(symbol, 0.0)) for symbol in set(state.target) | set(state.incumbent)}
            total = sum(changes.values())
            if total <= 0:
                raise FormalRunnerError("unallocatable reviewed trade cost")
            for symbol, amount in changes.items():
                add(symbol, -state.cost * amount / total, daily)
        daily_absolute.append(sum(abs(value) for value in daily.values()))

    for previous, current in zip(opens, opens[1:]):
        interval(previous, current)
    final = next((entry for entry in ledger if entry.phase == "FINAL_CLOSE"), None)
    if final is not None:
        interval(opens[-1], final)
    absolute = {symbol: abs(value) for symbol, value in contribution.items()}
    total = sum(absolute.values())
    ordered = sorted(absolute.values(), reverse=True)
    return {
        "per_symbol_net_contribution": dict(sorted(contribution.items())),
        "largest_symbol_absolute_net_contribution_share": (ordered[0] / total if total else 0.0),
        "top_five_absolute_net_contribution_share": (sum(ordered[:5]) / total if total else 0.0),
        "lifecycle_event_count": len(lifecycle_events),
        "largest_single_lifecycle_event_absolute_net_pnl": max((abs(value) for value in lifecycle_events), default=0.0),
        "total_absolute_daily_net_pnl": sum(daily_absolute),
        "largest_lifecycle_event_impact": (max((abs(value) for value in lifecycle_events), default=0.0) / sum(daily_absolute) if sum(daily_absolute) else 0.0),
    }


def summarize(outcome: consumer.ConsumerSchedule, schedule: Sequence[consumer.HorizonContract]) -> Mapping[str, Any]:
    """Aggregate only the already-executed consumer ledger; never reread data."""
    if len(outcome.decisions) != len(schedule):
        raise FormalRunnerError("consumer schedule/result mismatch")
    period_sets = {
        "train": {"train"}, "oos": {"oos"}, "holdout": {"holdout"}, "oos_holdout": {"oos", "holdout"},
        "oos_h1": {"oos_h1"}, "oos_h2": {"oos_h2"}, "holdout_h1": {"holdout_h1"}, "holdout_h2": {"holdout_h2"},
    }
    by_cost = {
        str(cost): {label: _path_metrics(ledger, periods) for label, periods in period_sets.items()}
        for cost, ledger in outcome.accounting.items()
    }
    ic: dict[str, list[float]] = {"train": [], "oos": [], "holdout": []}
    regimes: dict[str, list[float]] = {"calm": [], "high": []}
    max_weight = 0.0
    regime_returns: dict[str, list[float]] = {"calm": [], "high": []}
    regimes_by_execution = {contract.execution_ms: item.states[core.COST].regime for item, contract in zip(outcome.decisions, schedule)}
    baseline_opens = [entry for entry in outcome.accounting[core.COST] if entry.phase == "OPEN"]
    for previous, current in zip(baseline_opens, baseline_opens[1:]):
        if _split(previous.timestamp) in {"oos", "holdout"}:
            candidates = [timestamp for timestamp in regimes_by_execution if timestamp <= previous.timestamp]
            regime = regimes_by_execution[max(candidates)] if candidates else None
            if regime in regime_returns:
                regime_returns[regime].append(current.nav / previous.nav - 1.0)
    for item, contract in zip(outcome.decisions, schedule):
        state = item.states[core.COST]
        weights = [abs(value) for value in (*state.target.values(), *state.incumbent.values())]
        if weights:
            max_weight = max(max_weight, *weights)
        if item.ic is not None:
            ic[contract.split].append(item.ic)
            if state.regime in regimes:
                regimes[state.regime].append(item.ic)
    return {
        "portfolio": by_cost,
        "cost_impact": {
            split: by_cost["0.0"][split]["return"] - by_cost[str(core.COST)][split]["return"]
            for split in ("train", "oos", "holdout")
        },
        "ic": {
            **{split: _summary(values) for split, values in ic.items()},
            "oos_holdout": _summary([*ic["oos"], *ic["holdout"]]),
        },
        "regimes": {regime: {**_summary(values), "baseline_return": _path_metrics_from_returns(regime_returns[regime])} for regime, values in regimes.items()},
        "concentration": {"max_abs_incumbent_or_target_weight": max_weight, **_contributions(outcome, schedule)},
        "lifecycle": {str(cost): len(exits) for cost, exits in outcome.lifecycle_exits.items()},
        "turnover": {str(cost): sum(entry.turnover for entry in ledger) for cost, ledger in outcome.accounting.items()}
    }


def _path_metrics_from_returns(values: Sequence[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1.0 + value
    return result - 1.0


def evaluate(cells: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """Evaluate every frozen market gate; no post-result discretion remains."""
    primary = cells["n30_v30"]
    baseline = primary["portfolio"][str(core.COST)]
    stress = primary["portfolio"]["0.003"]
    concentration = primary["concentration"]
    regime = primary["regimes"]
    gates = {
        "primary": all(baseline[key]["return"] > 0 and baseline[key]["sharpe"] is not None and baseline[key]["sharpe"] >= 0.50 and baseline[key]["max_drawdown"] >= -0.25 for key in ("oos", "holdout")) and baseline["oos_holdout"]["return"] > 0 and baseline["oos_holdout"]["sharpe"] is not None and baseline["oos_holdout"]["sharpe"] >= 0.75,
        "stress": all(stress[key]["return"] > 0 for key in ("oos", "holdout")) and stress["oos_holdout"]["sharpe"] is not None and stress["oos_holdout"]["sharpe"] >= 0.50,
        "predictive": all(primary["ic"][key]["mean"] is not None and primary["ic"][key]["mean"] > 0 for key in ("oos", "holdout")) and primary["ic"]["oos_holdout"]["t_stat"] is not None and primary["ic"]["oos_holdout"]["t_stat"] >= 1.50,
        "regimes": all(regime[key]["count"] >= 20 and regime[key]["mean"] is not None and regime[key]["mean"] > 0 and regime[key]["baseline_return"] > 0 for key in ("calm", "high")),
        "multi_period": sum(baseline[key]["return"] > 0 for key in ("oos_h1", "oos_h2", "holdout_h1", "holdout_h2")) >= 3 and all(baseline[key]["max_drawdown"] >= -0.25 for key in ("oos_h1", "oos_h2", "holdout_h1", "holdout_h2")),
        "neighborhood": sum(cell["portfolio"][str(core.COST)]["oos"]["return"] > 0 and cell["portfolio"][str(core.COST)]["holdout"]["return"] > 0 and cell["portfolio"][str(core.COST)]["oos_holdout"]["sharpe"] is not None and cell["portfolio"][str(core.COST)]["oos_holdout"]["sharpe"] >= 0.50 for cell in cells.values()) >= 3,
        "concentration": concentration["max_abs_incumbent_or_target_weight"] <= 0.15 and concentration["largest_symbol_absolute_net_contribution_share"] <= 0.20 and concentration["top_five_absolute_net_contribution_share"] <= 0.60,
        "lifecycle": concentration["largest_lifecycle_event_impact"] <= 0.20,
    }
    return {"gates": gates, "classification": "EXPLORATION_PASS" if all(gates.values()) else "HYPOTHESIS_FAIL"}


def run(data_root: pathlib.Path, result_path: pathlib.Path) -> Mapping[str, Any]:
    """Perform the single authorized formal run and atomically write its artifact."""
    freeze = load_freeze()
    require_independent_approval()
    if result_path.exists():
        raise FormalRunnerError("formal result artifact already exists")
    schedule = frozen_schedule()
    cells: dict[str, Any] = {}
    adapter = consumer.load_verified_price_lifecycle_adapter(data_root)
    for params in freeze["parameters"]["neighborhood"]:
        config = core.Config(n=params["top_n"], volatility_window=params["volatility_window_days"])
        outcome = consumer.FormalConsumer(adapter, config).execute_schedule(schedule, consumer.FormalConsumer._frozen_final_timestamp())
        cells[f"n{config.n}_v{config.volatility_window}"] = summarize(outcome, schedule)
    payload = {"formal_run_id": FORMAL_RUN_ID, "freeze_contract_sha256": _sha256(FREEZE_PATH), "results": cells, "verdict": evaluate(cells)}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload
