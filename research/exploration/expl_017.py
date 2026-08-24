"""Synthetic-only, transactional correctness engine for EXPL-017-IMPL-014."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


DAY_MS = 86_400_000
ANN = 365.0
COST = 0.0015
ANCHOR_MS = 1_609_459_200_000
HYPOTHESIS_ID = "EXPL-017"
IMPLEMENTATION_ATTEMPT_ID = "EXPL-017-IMPL-014"
FORMAL_RUN_ID = None


class PreFormalError(RuntimeError):
    pass


class FormalRunLockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionBar:
    close: float
    quote_volume: float


@dataclass(frozen=True)
class CompletedBar:
    open: float
    close: float


@dataclass(frozen=True)
class LifecycleStatus:
    active: bool
    terminal_timestamp: int | None = None


@dataclass(frozen=True)
class Config:
    n: int = 30
    horizons: tuple[int, ...] = (7, 14, 28)
    volatility_window: int = 30
    cost: float = COST

    def validate(self) -> None:
        production = (
            self.n in {20, 30}
            and self.horizons == (7, 14, 28)
            and self.volatility_window in {21, 30}
        )
        gold_fixture = (
            self.n == 5
            and self.horizons == (1, 2)
            and self.volatility_window == 2
        )
        if not (production or gold_fixture) or self.cost not in {0.0, COST, 0.003}:
            raise PreFormalError("frozen configuration violated")


@dataclass(frozen=True)
class Decision:
    close_ms: int
    split: str


@dataclass(frozen=True)
class Exit:
    symbol: str
    timestamp: int
    nav_before: float
    turnover: float
    cost: float


@dataclass(frozen=True)
class AccountingEntry:
    """One continuous portfolio accounting checkpoint, not a performance metric."""

    timestamp: int
    nav: float
    cash: float
    notionals: tuple[tuple[str, float], ...]
    turnover: float
    cost: float
    exits: tuple[Exit, ...]
    finalization: bool


@dataclass(frozen=True)
class State:
    selected: tuple[str, ...]
    momentum: dict[str, float]
    volatility: float
    threshold: float | None
    regime: str | None
    target: dict[str, float]
    nav_before_trade: float
    incumbent: dict[str, float]
    turnover: float
    cost: float
    exits: tuple[Exit, ...]


@dataclass(frozen=True)
class _Snapshot:
    last_mark_ms: int | None
    last_execution_ms: int | None
    terminal_events: dict[str, int]
    terminated_symbols: set[str]
    cash: float
    notionals: dict[str, float]
    train: list[float]
    fixed: float | None
    left_train: bool
    accounting: list[AccountingEntry]
    finalized_at: int | None


def pos(value: float) -> bool:
    return math.isfinite(value) and value > 0


def ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) < 2 or not all(math.isfinite(item[1]) for item in ordered):
        raise PreFormalError("bad ranks")
    result: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = ((index + end - 1) / 2) / (len(ordered) - 1)
        for tied_index in range(index, end):
            result[ordered[tied_index][0]] = rank
        index = end
    return result


@dataclass
class Portfolio:
    cash: float = 1.0
    notionals: dict[str, float] = field(default_factory=dict)

    def nav(self) -> float:
        value = self.cash + sum(self.notionals.values())
        if not math.isfinite(value) or value <= 0:
            raise PreFormalError("non-positive NAV")
        return value

    def weights(self) -> dict[str, float]:
        nav = self.nav()
        return {symbol: value / nav for symbol, value in self.notionals.items()}

    def mark(self, relatives: dict[str, float]) -> None:
        if set(relatives) != set(self.notionals) or not all(
            pos(value) for value in relatives.values()
        ):
            raise PreFormalError("bad mark")
        for symbol, relative in relatives.items():
            self.notionals[symbol] *= relative
        self.nav()

    def trade(self, target: dict[str, float], cost_rate: float) -> tuple[float, float]:
        nav = self.nav()
        incumbent = self.weights()
        symbols = set(target) | set(incumbent)
        turnover = sum(
            abs(target.get(symbol, 0.0) - incumbent.get(symbol, 0.0))
            for symbol in symbols
        )
        cost = turnover * cost_rate * nav
        new_notionals = {
            symbol: weight * nav for symbol, weight in target.items() if weight
        }
        self.cash += (
            sum(self.notionals.values()) - sum(new_notionals.values()) - cost
        )
        self.notionals = new_notionals
        self.nav()
        return turnover, cost

    def exit(self, symbol: str, cost_rate: float, timestamp: int) -> Exit:
        if symbol not in self.notionals:
            raise PreFormalError("duplicate exit")
        nav = self.nav()
        notional = self.notionals.pop(symbol)
        cost = abs(notional) * cost_rate
        self.cash += notional - cost
        self.nav()
        return Exit(symbol, timestamp, nav, abs(notional) / nav, cost)


class Engine:
    def __init__(self, fixture, config: Config = Config()):
        if not (
            getattr(fixture, "is_synthetic", False) is True
            or getattr(fixture, "is_verified_formal", False) is True
        ):
            raise PreFormalError("synthetic or verified formal fixture required")
        config.validate()
        self.fixture = fixture
        self.config = config
        self.portfolio = Portfolio()
        self.last_mark_ms: int | None = None
        self.last_execution_ms: int | None = None
        self.terminated_symbols: set[str] = set()
        self._terminal_events: dict[str, int] = {}
        self.train: list[float] = []
        self.fixed: float | None = None
        self.left_train = False
        self.accounting: list[AccountingEntry] = []
        self.finalized_at: int | None = None

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(
            last_mark_ms=self.last_mark_ms,
            last_execution_ms=self.last_execution_ms,
            terminal_events=dict(self._terminal_events),
            terminated_symbols=set(self.terminated_symbols),
            cash=self.portfolio.cash,
            notionals=dict(self.portfolio.notionals),
            train=list(self.train),
            fixed=self.fixed,
            left_train=self.left_train,
            accounting=list(self.accounting),
            finalized_at=self.finalized_at,
        )

    def _restore(self, snapshot: _Snapshot) -> None:
        self.last_mark_ms = snapshot.last_mark_ms
        self.last_execution_ms = snapshot.last_execution_ms
        self._terminal_events = dict(snapshot.terminal_events)
        self.terminated_symbols = set(snapshot.terminated_symbols)
        self.portfolio.cash = snapshot.cash
        self.portfolio.notionals = dict(snapshot.notionals)
        self.train = list(snapshot.train)
        self.fixed = snapshot.fixed
        self.left_train = snapshot.left_train
        self.accounting = list(snapshot.accounting)
        self.finalized_at = snapshot.finalized_at

    def _record(
        self,
        timestamp: int,
        turnover: float = 0.0,
        cost: float = 0.0,
        exits: tuple[Exit, ...] = (),
        finalization: bool = False,
    ) -> None:
        """Keep one mergeable NAV/accounting checkpoint per UTC date."""
        if self.accounting and timestamp < self.accounting[-1].timestamp:
            raise PreFormalError("accounting timeline")
        prior = self.accounting[-1] if self.accounting and self.accounting[-1].timestamp == timestamp else None
        if prior is not None:
            turnover += prior.turnover
            cost += prior.cost
            exits = prior.exits + exits
            finalization = finalization or prior.finalization
            self.accounting.pop()
        self.accounting.append(
            AccountingEntry(
                timestamp=timestamp,
                nav=self.portfolio.nav(),
                cash=self.portfolio.cash,
                notionals=tuple(sorted(self.portfolio.notionals.items())),
                turnover=turnover,
                cost=cost,
                exits=exits,
                finalization=finalization,
            )
        )

    def _decision(self, symbol: str, timestamp: int) -> DecisionBar:
        try:
            value = self.fixture.decision_bar(symbol, timestamp)
        except (KeyError, AttributeError) as error:
            raise PreFormalError("decision bar absent") from error
        if not isinstance(value, DecisionBar) or not pos(value.close):
            raise PreFormalError("bad decision bar")
        return value

    def _open(self, symbol: str, timestamp: int) -> float:
        try:
            value = self.fixture.execution_open(symbol, timestamp)
        except (KeyError, AttributeError) as error:
            raise PreFormalError("execution open absent") from error
        if not pos(value):
            raise PreFormalError("bad execution open")
        return value

    def _bar(self, symbol: str, timestamp: int) -> CompletedBar:
        try:
            value = self.fixture.completed_bar(symbol, timestamp)
        except (KeyError, AttributeError) as error:
            raise PreFormalError("completed bar absent") from error
        if (
            not isinstance(value, CompletedBar)
            or not pos(value.open)
            or not pos(value.close)
        ):
            raise PreFormalError("bad completed bar")
        return value

    def _read_status(self, symbol: str, asof: int) -> LifecycleStatus:
        try:
            value = self.fixture.lifecycle_as_of(symbol, asof)
        except PreFormalError:
            raise
        except Exception as error:
            raise PreFormalError("lifecycle status unavailable") from error
        if not isinstance(value, LifecycleStatus):
            raise PreFormalError("malformed lifecycle status")
        if type(value.active) is not bool:
            raise PreFormalError("malformed lifecycle active")
        prior = self._terminal_events.get(symbol)
        if value.active:
            if value.terminal_timestamp is not None:
                raise PreFormalError("contradictory lifecycle status")
            if prior is not None:
                raise PreFormalError("terminal reactivation")
        elif (
            type(value.terminal_timestamp) is not int
            or value.terminal_timestamp > asof
            or (value.terminal_timestamp - ANCHOR_MS) % DAY_MS
        ):
            raise PreFormalError("future/malformed terminal event")
        elif prior is not None and prior != value.terminal_timestamp:
            raise PreFormalError("terminal timestamp changed")
        return value

    def _universe(self, effective_timestamp: int) -> tuple[str, ...]:
        try:
            raw = self.fixture.universe(effective_timestamp)
        except PreFormalError:
            raise
        except Exception as error:
            raise PreFormalError("PIT universe unavailable") from error
        if type(raw) not in {tuple, list}:
            raise PreFormalError("malformed PIT universe container")
        members = tuple(raw)
        if any(
            type(symbol) is not str
            or not symbol
            or symbol != symbol.strip()
            or not symbol.isascii()
            or not symbol.isalnum()
            or symbol != symbol.upper()
            for symbol in members
        ):
            raise PreFormalError("malformed PIT universe")
        if members != tuple(sorted(set(members))):
            raise PreFormalError("malformed PIT universe")
        return members

    def _observe_and_exit(
        self, timestamp: int, members: tuple[str, ...], move: bool
    ) -> tuple[Exit, ...]:
        symbols = sorted(set(members) | set(self.portfolio.notionals))
        statuses = {
            symbol: self._read_status(symbol, timestamp) for symbol in symbols
        }
        inactive = {
            symbol: status.terminal_timestamp
            for symbol, status in statuses.items()
            if not status.active
        }
        held = [symbol for symbol in inactive if symbol in self.portfolio.notionals]
        if any(inactive[symbol] != timestamp for symbol in held):
            raise PreFormalError("held terminal was not exited on event")

        # The full lifecycle batch is valid before persistent lifecycle state changes.
        self._terminal_events.update(
            {
                symbol: terminal_timestamp
                for symbol, terminal_timestamp in inactive.items()
                if symbol not in self._terminal_events
            }
        )
        self.terminated_symbols.update(inactive)

        exits: list[Exit] = []
        if held:
            bars = {symbol: self._bar(symbol, timestamp) for symbol in held}
            self.portfolio.mark(
                {
                    symbol: (
                        bars[symbol].close / bars[symbol].open
                        if symbol in held
                        else 1.0
                    )
                    for symbol in self.portfolio.notionals
                }
            )
            exits = [
                self.portfolio.exit(symbol, self.config.cost, timestamp)
                for symbol in sorted(held)
            ]
        if move and self.portfolio.notionals:
            bars = {
                symbol: self._bar(symbol, timestamp)
                for symbol in self.portfolio.notionals
            }
            relatives = {
                symbol: self._open(symbol, timestamp + DAY_MS) / bars[symbol].open
                for symbol in self.portfolio.notionals
            }
            self.portfolio.mark(relatives)
        self._record(
            timestamp + DAY_MS if move else timestamp,
            turnover=sum(item.turnover for item in exits),
            cost=sum(item.cost for item in exits),
            exits=tuple(exits),
        )
        return tuple(exits)

    def _advance_to(
        self, timestamp: int, pit: tuple[str, ...] = ()
    ) -> tuple[Exit, ...]:
        if self.last_mark_ms is None:
            exits = self._observe_and_exit(timestamp, pit, False)
            self.last_mark_ms = timestamp
            return exits
        if (
            timestamp < self.last_mark_ms
            or (timestamp - self.last_mark_ms) % DAY_MS
        ):
            raise PreFormalError("timeline")
        exits: list[Exit] = []
        while self.last_mark_ms < timestamp:
            exits.extend(
                self._observe_and_exit(
                    self.last_mark_ms, tuple(self.portfolio.notionals), True
                )
            )
            self.last_mark_ms += DAY_MS
        exits.extend(self._observe_and_exit(timestamp, pit, False))
        return tuple(exits)

    def advance_to(
        self, timestamp: int, pit: tuple[str, ...] = ()
    ) -> tuple[Exit, ...]:
        snapshot = self._snapshot()
        try:
            return self._advance_to(timestamp, pit)
        except BaseException:
            self._restore(snapshot)
            raise

    def _to_execution(self, execution_timestamp: int) -> None:
        if (
            self.last_mark_ms is None
            or execution_timestamp != self.last_mark_ms + DAY_MS
        ):
            raise PreFormalError("execution timing")
        if self.portfolio.notionals:
            bars = {
                symbol: self._bar(symbol, self.last_mark_ms)
                for symbol in self.portfolio.notionals
            }
            relatives = {
                symbol: self._open(symbol, execution_timestamp) / bars[symbol].open
                for symbol in self.portfolio.notionals
            }
            self.portfolio.mark(relatives)
        self.last_mark_ms = execution_timestamp

    def _select(self, members: tuple[str, ...], timestamp: int) -> tuple[str, ...]:
        active = [
            symbol for symbol in members if symbol not in self.terminated_symbols
        ]
        volumes: dict[str, float] = {}
        for symbol in active:
            history = [
                self._decision(symbol, timestamp - index * DAY_MS).quote_volume
                for index in range(90)
            ]
            if not all(math.isfinite(value) and value >= 0 for value in history):
                raise PreFormalError("bad volume")
            volumes[symbol] = statistics.median(history)
        selected = tuple(
            symbol
            for symbol, _ in sorted(
                volumes.items(), key=lambda item: (-item[1], item[0])
            )[: self.config.n]
        )
        if len(selected) != self.config.n:
            raise PreFormalError("insufficient active PIT")
        for symbol in selected:
            for horizon in self.config.horizons:
                self._decision(symbol, timestamp - horizon * DAY_MS)
            for index in range(self.config.volatility_window + 1):
                self._decision(symbol, timestamp - index * DAY_MS)
            self._open(symbol, timestamp + DAY_MS)
        return selected

    def _features(
        self, selected: tuple[str, ...], timestamp: int
    ) -> tuple[dict[str, float], float]:
        horizon_ranks = []
        for horizon in self.config.horizons:
            horizon_ranks.append(
                ranks(
                    {
                        symbol: self._decision(symbol, timestamp).close
                        / self._decision(symbol, timestamp - horizon * DAY_MS).close
                        - 1
                        for symbol in selected
                    }
                )
            )
        momentum = {
            symbol: statistics.mean(
                horizon_rank[symbol] for horizon_rank in horizon_ranks
            )
            for symbol in selected
        }
        volatilities = []
        for symbol in selected:
            returns = [
                self._decision(symbol, timestamp - index * DAY_MS).close
                / self._decision(symbol, timestamp - (index + 1) * DAY_MS).close
                - 1
                for index in reversed(range(self.config.volatility_window))
            ]
            volatilities.append(statistics.stdev(returns) * math.sqrt(ANN))
        return momentum, statistics.median(volatilities)

    def _regime(self, split: str, value: float) -> tuple[float | None, str | None]:
        if split == "train":
            if self.left_train:
                raise PreFormalError("train after later split")
            threshold = statistics.median(self.train) if len(self.train) >= 8 else None
            self.train.append(value)
        elif split in {"oos", "holdout"}:
            self.left_train = True
            if len(self.train) < 8:
                raise PreFormalError("short train")
            if self.fixed is None:
                self.fixed = statistics.median(self.train)
            threshold = self.fixed
        else:
            raise PreFormalError("bad split")
        regime = None if threshold is None else ("calm" if value <= threshold else "high")
        return threshold, regime

    def _target(
        self, momentum: dict[str, float], regime: str | None
    ) -> dict[str, float]:
        if regime is None:
            return {}
        ordered = sorted(momentum, key=lambda symbol: (momentum[symbol], symbol))
        leg_size = max(1, math.floor(len(ordered) * 0.2))
        losers = ordered[:leg_size]
        winners = ordered[-leg_size:]
        longs, shorts = (winners, losers) if regime == "calm" else (losers, winners)
        return {
            **{symbol: 0.5 / len(longs) for symbol in longs},
            **{symbol: -0.5 / len(shorts) for symbol in shorts},
        }

    def _execute(self, decision: Decision) -> State:
        if self.finalized_at is not None:
            raise PreFormalError("execution after finalization")
        execution_timestamp = decision.close_ms + DAY_MS
        if (execution_timestamp - ANCHOR_MS) % (7 * DAY_MS):
            raise PreFormalError("not weekly")
        if (
            self.last_execution_ms is not None
            and execution_timestamp != self.last_execution_ms + 7 * DAY_MS
        ):
            raise PreFormalError("cadence")
        members = self._universe(execution_timestamp)
        exits = self.advance_to(decision.close_ms, members)
        selected = self._select(members, decision.close_ms)
        momentum, volatility = self._features(selected, decision.close_ms)
        threshold, regime = self._regime(decision.split, volatility)
        target = self._target(momentum, regime)
        self._to_execution(execution_timestamp)
        nav_before_trade = self.portfolio.nav()
        incumbent = self.portfolio.weights()
        turnover, cost = self.portfolio.trade(target, self.config.cost)
        self._record(execution_timestamp, turnover, cost)
        self.last_execution_ms = execution_timestamp
        return State(
            selected,
            momentum,
            volatility,
            threshold,
            regime,
            target,
            nav_before_trade,
            incumbent,
            turnover,
            cost,
            exits,
        )

    def execute(self, decision: Decision) -> State:
        snapshot = self._snapshot()
        try:
            return self._execute(decision)
        except BaseException:
            self._restore(snapshot)
            raise

    def _finalize(self, final_timestamp: int) -> tuple[Exit, ...]:
        if self.finalized_at is not None:
            raise PreFormalError("duplicate finalization")
        if self.last_execution_ms is None or self.last_mark_ms is None:
            raise PreFormalError("finalization before execution")
        if final_timestamp < self.last_mark_ms or (final_timestamp - ANCHOR_MS) % DAY_MS:
            raise PreFormalError("finalization timeline")
        prior_exits = list(self._advance_to(final_timestamp))
        final_exits: list[Exit] = []
        if self.portfolio.notionals:
            bars = {
                symbol: self._bar(symbol, final_timestamp)
                for symbol in self.portfolio.notionals
            }
            self.portfolio.mark(
                {
                    symbol: bars[symbol].close / bars[symbol].open
                    for symbol in self.portfolio.notionals
                }
            )
            nav_before_exit = self.portfolio.nav()
            incumbent = self.portfolio.weights()
            notionals = dict(self.portfolio.notionals)
            turnover, cost = self.portfolio.trade({}, self.config.cost)
            if not math.isclose(turnover, sum(abs(weight) for weight in incumbent.values())):
                raise PreFormalError("final liquidation turnover")
            final_exits.extend(
                Exit(
                    symbol=symbol,
                    timestamp=final_timestamp,
                    nav_before=nav_before_exit,
                    turnover=abs(incumbent[symbol]),
                    cost=abs(notionals[symbol]) * self.config.cost,
                )
                for symbol in sorted(notionals)
            )
            if not math.isclose(sum(item.cost for item in final_exits), cost):
                raise PreFormalError("final liquidation cost")
        self.finalized_at = final_timestamp
        self._record(
            final_timestamp,
            turnover=sum(item.turnover for item in final_exits),
            cost=sum(item.cost for item in final_exits),
            exits=tuple(final_exits),
            finalization=True,
        )
        return tuple(prior_exits + final_exits)

    def finalize(self, final_timestamp: int) -> tuple[Exit, ...]:
        """Mark the frozen final daily bar then force exits using Portfolio.exit."""
        snapshot = self._snapshot()
        try:
            return self._finalize(final_timestamp)
        except BaseException:
            self._restore(snapshot)
            raise


def formal_run(*args, **kwargs):
    raise FormalRunLockedError("FORMAL_RUN_LOCKED")
