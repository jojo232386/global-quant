"""EXPL-017-IMPL-016 formal-consumer correctness path.

This is deliberately not a formal-run entry point.  It adapts immutable Price
V1 plus the exception-only Lifecycle V1 sidecar to the reviewed IMPL-014
engine. ``formal_run`` in ``expl_017`` remains locked and no actual
Train/OOS/Holdout metric is read in IMPL-016.
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence


HERE = pathlib.Path(__file__).resolve()
for _directory in (HERE.parent, HERE.parents[1] / "data"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import expl_017 as core  # noqa: E402
import expl_017_lifecycle_v1 as lifecycle  # noqa: E402


FORWARD_HORIZON_DAYS = 7
FORMAL_METRICS_EXPOSED = False
FORMAL_RUN_ID = None


class FormalDataUnavailable(core.PreFormalError):
    """A required formal price has no PIT-safe lifecycle interpretation."""


class FormalMetricsExposureError(core.PreFormalError):
    """Actual formal performance is deliberately prohibited in IMPL-016."""


@dataclass(frozen=True)
class HorizonContract:
    decision_ms: int
    execution_ms: int
    endpoint_ms: int
    split: str
    ic_included: bool

    def validate(self) -> None:
        if self.execution_ms != self.decision_ms + core.DAY_MS:
            raise core.PreFormalError("horizon decision/execution mismatch")
        if self.endpoint_ms != self.execution_ms + FORWARD_HORIZON_DAYS * core.DAY_MS:
            raise core.PreFormalError("horizon endpoint mismatch")
        if self.split not in {"train", "oos", "holdout"}:
            raise core.PreFormalError("horizon split invalid")
        if type(self.ic_included) is not bool:
            raise core.PreFormalError("horizon IC inclusion must be boolean")


class PriceV1FormalAdapter:
    """Thin Price V1 + Lifecycle V1 fixture for the reviewed core Engine."""

    is_verified_formal = True
    is_synthetic = False

    def __init__(self, dataset, resolver: lifecycle.LifecycleResolver):
        self.dataset = dataset
        self.resolver = resolver

    def universe(self, effective_timestamp: int) -> tuple[str, ...]:
        try:
            return tuple(self.dataset.universe(effective_timestamp))
        except Exception as error:
            raise FormalDataUnavailable("DATA_UNAVAILABLE: PIT universe") from error

    def _price_bar(self, symbol: str, timestamp: int):
        try:
            return self.dataset.bar(symbol, timestamp)
        except Exception as error:
            try:
                self.resolver.require_missing_bar_semantics(symbol, timestamp)
            except lifecycle.LifecycleDataUnavailable as unavailable:
                raise FormalDataUnavailable(str(unavailable)) from error
            raise FormalDataUnavailable(
                f"DATA_UNAVAILABLE: confirmed terminal {symbol} lacks its final daily bar"
            ) from error

    def decision_bar(self, symbol: str, timestamp: int) -> core.DecisionBar:
        bar = self._price_bar(symbol, timestamp)
        return core.DecisionBar(bar.close, bar.quote_volume)

    def execution_open(self, symbol: str, timestamp: int) -> float:
        return self._price_bar(symbol, timestamp).open

    def completed_bar(self, symbol: str, timestamp: int) -> core.CompletedBar:
        bar = self._price_bar(symbol, timestamp)
        return core.CompletedBar(bar.open, bar.close)

    def lifecycle_as_of(self, symbol: str, timestamp: int) -> core.LifecycleStatus:
        view = self.resolver.as_of(symbol, timestamp)
        return core.LifecycleStatus(view.active, view.terminal_timestamp)

    def forward_return(self, symbol: str, execution_ms: int, endpoint_ms: int) -> float:
        """Use exact endpoint open, or a PIT-confirmed terminal final close only."""
        if endpoint_ms != execution_ms + FORWARD_HORIZON_DAYS * core.DAY_MS:
            raise core.PreFormalError("partial or truncated IC horizon")
        start = self.execution_open(symbol, execution_ms)
        try:
            end = self.execution_open(symbol, endpoint_ms)
        except FormalDataUnavailable as original:
            event = self.resolver.terminal_event_as_of(symbol, endpoint_ms)
            if event is None or not (execution_ms <= event.last_valid_bar_ms < endpoint_ms):
                raise original
            terminal = self.completed_bar(symbol, event.last_valid_bar_ms)
            return terminal.close / start - 1.0
        return end / start - 1.0


def load_verified_price_lifecycle_adapter(data_root: pathlib.Path):
    """Bind the immutable Price V1 snapshot and sidecar without running metrics."""
    import price_alpha_v1 as price  # local, price-only existing loader

    dataset = price.load_dataset(data_root)
    if (
        dataset.manifest_sha256 != lifecycle.PRICE_MANIFEST_SHA
        or dataset.pit_sha256 != lifecycle.PRICE_PIT_SHA
        or len(dataset.bars) != 208
    ):
        raise FormalDataUnavailable("DATA_UNAVAILABLE: verified Price V1 identity differs")
    events, sidecar_sha = lifecycle.load_sidecar()
    lifecycle.verify_composite_identity(sidecar_sha)
    _verify_price_v1_scope(dataset, events)
    return PriceV1FormalAdapter(dataset, lifecycle.LifecycleResolver(events))


def _verify_price_v1_scope(dataset, events: Mapping[str, lifecycle.LifecycleEvent]) -> None:
    """Recompute the exception-only audit from immutable loaded Price V1 bytes."""
    cutoff = int(dt.datetime(2023, 12, 31, tzinfo=dt.UTC).timestamp() * 1000)
    if len(dataset.bars) != 208 or set(dataset.bars) != set(dataset.last_timestamp):
        raise FormalDataUnavailable("DATA_UNAVAILABLE: Price V1 symbol identity differs")
    early: set[str] = set()
    for symbol, series in dataset.bars.items():
        timestamps = sorted(series)
        if not timestamps or dataset.last_timestamp[symbol] != timestamps[-1]:
            raise FormalDataUnavailable("DATA_UNAVAILABLE: Price V1 terminal index differs")
        if any(right != left + core.DAY_MS for left, right in zip(timestamps, timestamps[1:])):
            raise FormalDataUnavailable("DATA_UNAVAILABLE: Price V1 internal gap")
        if timestamps[-1] == cutoff:
            continue
        if timestamps[-1] > cutoff:
            raise FormalDataUnavailable("DATA_UNAVAILABLE: Price V1 exceeds cutoff")
        early.add(symbol)
    if len(early) != 12 or len(dataset.bars) - len(early) != 196 or early != set(events):
        raise FormalDataUnavailable("DATA_UNAVAILABLE: lifecycle exception scope differs")


def _average_ranks(values: Mapping[str, float]) -> dict[str, float]:
    if len(values) < 2 or not all(math.isfinite(value) for value in values.values()):
        raise core.PreFormalError("IC requires two finite observations")
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    output: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        stop = index + 1
        while stop < len(ordered) and ordered[stop][1] == ordered[index][1]:
            stop += 1
        rank = (index + stop - 1) / 2
        for current in range(index, stop):
            output[ordered[current][0]] = rank
        index = stop
    return output


def spearman(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if set(left) != set(right):
        raise core.PreFormalError("IC symbol mismatch")
    left_ranks, right_ranks = _average_ranks(left), _average_ranks(right)
    left_values = list(left_ranks.values())
    right_values = [right_ranks[symbol] for symbol in left_ranks]
    left_mean, right_mean = statistics.mean(left_values), statistics.mean(right_values)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values)
    )
    left_scale = math.sqrt(sum((a - left_mean) ** 2 for a in left_values))
    right_scale = math.sqrt(sum((b - right_mean) ** 2 for b in right_values))
    if not left_scale or not right_scale:
        raise core.PreFormalError("IC rank variance is zero")
    return numerator / (left_scale * right_scale)


@dataclass(frozen=True)
class ConsumerDecision:
    states: Mapping[float, core.State]
    ic: float | None


class FormalConsumer:
    """Three cost paths sharing one reviewed-engine semantics contract.

    Verified Price V1 adapters are accepted so the consumer can be completely
    bound before a later freeze.  Their contracts must exactly match the
    already-reviewed static containment schedule.  This class does not
    schedule a formal run, print, or serialize a metric.
    """

    def __init__(self, fixture, config: core.Config):
        self._synthetic = getattr(fixture, "is_synthetic", False) is True
        self._verified_formal = getattr(fixture, "is_verified_formal", False) is True
        if not (self._synthetic or self._verified_formal):
            raise FormalMetricsExposureError("synthetic or verified formal fixture required")
        self.fixture = fixture
        self.config = config
        self.engines = {
            cost: core.Engine(
                fixture,
                core.Config(
                    n=config.n,
                    horizons=config.horizons,
                    volatility_window=config.volatility_window,
                    cost=cost,
                ),
            )
            for cost in (0.0, core.COST, 0.003)
        }

    @staticmethod
    def _preflight_contracts() -> dict[int, HorizonContract]:
        import expl_017_horizon_preflight as horizon

        def timestamp(value: object) -> int:
            parsed = dt.date.fromisoformat(str(value))
            return int(
                dt.datetime.combine(parsed, dt.time(), tzinfo=dt.UTC).timestamp()
                * 1000
            )

        return {
            timestamp(row["decision"]): HorizonContract(
                decision_ms=timestamp(row["decision"]),
                execution_ms=timestamp(row["execution"]),
                endpoint_ms=timestamp(row["endpoint"]),
                split=str(row["split"]),
                ic_included=bool(row["ic_included"]),
            )
            for row in horizon.build_rows()
        }

    def validate_contract(self, contract: HorizonContract) -> None:
        """Validate before any fixture accessor can read a price."""
        contract.validate()
        if not self._verified_formal:
            return
        expected = self._preflight_contracts().get(contract.decision_ms)
        if expected != contract:
            raise core.PreFormalError("verified formal contract differs from horizon preflight")

    @staticmethod
    def _semantic_tuple(state: core.State) -> tuple[object, ...]:
        return (
            state.selected,
            tuple(sorted(state.momentum.items())),
            state.volatility,
            state.threshold,
            state.regime,
            tuple(sorted(state.target.items())),
            tuple((exit.symbol, exit.timestamp) for exit in state.exits),
        )

    def execute(self, contract: HorizonContract) -> ConsumerDecision:
        self.validate_contract(contract)
        states = {
            cost: engine.execute(core.Decision(contract.decision_ms, contract.split))
            for cost, engine in self.engines.items()
        }
        semantics = [self._semantic_tuple(item) for item in states.values()]
        if any(item != semantics[0] for item in semantics[1:]):
            raise core.PreFormalError("cost path signal semantic parity failed")

        ic: float | None = None
        if contract.ic_included:
            primary = states[core.COST]
            if primary.regime not in {"calm", "high"}:
                raise core.PreFormalError("IC included before regime is defined")
            raw_returns = {
                symbol: self.fixture.forward_return(
                    symbol, contract.execution_ms, contract.endpoint_ms
                )
                for symbol in primary.selected
            }
            oriented = {
                symbol: primary.momentum[symbol]
                if primary.regime == "calm"
                else -primary.momentum[symbol]
                for symbol in primary.selected
            }
            ic = spearman(oriented, raw_returns)
        return ConsumerDecision(states=states, ic=ic)
