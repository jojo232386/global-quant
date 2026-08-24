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


def _path_metrics(ledger: Sequence[core.AccountingEntry], split: str) -> Mapping[str, float | int | None]:
    opens = [entry for entry in ledger if entry.phase == "OPEN"]
    if len(opens) < 2:
        raise FormalRunnerError("insufficient continuous accounting")
    returns: list[float] = []
    for previous, current in zip(opens, opens[1:]):
        # The ledger checkpoint at current open closes the interval that began
        # at previous UTC open, which is the frozen reporting convention.
        if _split(previous.timestamp) == split:
            returns.append(current.nav / previous.nav - 1.0)
    final = next((entry for entry in ledger if entry.phase == "FINAL_CLOSE"), None)
    if final is not None and split == "holdout":
        last_open = opens[-1]
        returns.append(final.nav / last_open.nav - 1.0)
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


def summarize(outcome: consumer.ConsumerSchedule, schedule: Sequence[consumer.HorizonContract]) -> Mapping[str, Any]:
    """Aggregate only the already-executed consumer ledger; never reread data."""
    if len(outcome.decisions) != len(schedule):
        raise FormalRunnerError("consumer schedule/result mismatch")
    by_cost = {
        str(cost): {split: _path_metrics(ledger, split) for split in ("train", "oos", "holdout")}
        for cost, ledger in outcome.accounting.items()
    }
    ic: dict[str, list[float]] = {"train": [], "oos": [], "holdout": []}
    regimes: dict[str, list[float]] = {"calm": [], "high": []}
    max_weight = 0.0
    for item, contract in zip(outcome.decisions, schedule):
        state = item.states[core.COST]
        max_weight = max(max_weight, *(abs(value) for value in (*state.target.values(), *state.incumbent.values())))
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
        "ic": {split: _summary(values) for split, values in ic.items()},
        "regimes": {regime: _summary(values) for regime, values in regimes.items()},
        "concentration": {"max_abs_incumbent_or_target_weight": max_weight},
        "lifecycle": {str(cost): len(exits) for cost, exits in outcome.lifecycle_exits.items()},
        "turnover": {str(cost): sum(entry.turnover for entry in ledger) for cost, ledger in outcome.accounting.items()}
    }


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
    payload = {"formal_run_id": FORMAL_RUN_ID, "freeze_contract_sha256": _sha256(FREEZE_PATH), "results": cells}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload
