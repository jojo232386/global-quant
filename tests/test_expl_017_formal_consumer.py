from __future__ import annotations

import ast
import datetime as dt
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "exploration"))
sys.path.insert(0, str(ROOT / "research" / "data"))
import expl_017 as core  # noqa: E402
import expl_017_formal_consumer as formal  # noqa: E402
import expl_017_lifecycle_v1 as lifecycle  # noqa: E402
import expl_017_horizon_preflight as horizon  # noqa: E402


def day(year: int, month: int, value: int) -> int:
    return int(dt.datetime(year, month, value, tzinfo=dt.UTC).timestamp() * 1000)


class SyntheticFixture:
    """Independent deterministic inputs; it never calls the consumer for data."""

    is_synthetic = True

    def __init__(self, base: int):
        self.symbols = ("A", "B", "C", "D", "E")
        self.base = base
        self.bars = {symbol: {} for symbol in self.symbols}
        rates = {"A": 0.001, "B": 0.002, "C": 0.003, "D": 0.004, "E": 0.005}
        for offset in range(-200, 120):
            timestamp = base + offset * core.DAY_MS
            for index, symbol in enumerate(self.symbols):
                # Independent input formula; momentum and later open returns are
                # strictly ordered A < ... < E by the stated daily rates.
                open_value = (100.0 + index) * (1.0 + rates[symbol]) ** (offset + 200)
                self.bars[symbol][timestamp] = core.CompletedBar(
                    open_value, open_value * (1.0 + rates[symbol] / 2.0)
                )
        self.missing: set[tuple[str, int]] = set()

    def universe(self, effective_timestamp):
        return self.symbols

    def decision_bar(self, symbol, timestamp):
        bar = self.completed_bar(symbol, timestamp)
        return core.DecisionBar(bar.close, 10_000.0 - self.symbols.index(symbol))

    def execution_open(self, symbol, timestamp):
        if (symbol, timestamp) in self.missing:
            raise formal.FormalDataUnavailable("DATA_UNAVAILABLE: independent synthetic gap")
        return self.completed_bar(symbol, timestamp).open

    def completed_bar(self, symbol, timestamp):
        if (symbol, timestamp) in self.missing:
            raise formal.FormalDataUnavailable("DATA_UNAVAILABLE: independent synthetic gap")
        return self.bars[symbol][timestamp]

    def lifecycle_as_of(self, symbol, timestamp):
        return core.LifecycleStatus(True)

    def forward_return(self, symbol, execution_ms, endpoint_ms):
        if endpoint_ms != execution_ms + 7 * core.DAY_MS:
            raise core.PreFormalError("partial or truncated IC horizon")
        return self.execution_open(symbol, endpoint_ms) / self.execution_open(symbol, execution_ms) - 1.0


GOLD_CONFIG = core.Config(n=5, horizons=(1, 2), volatility_window=2)


def contracts(base: int) -> list[formal.HorizonContract]:
    return [
        formal.HorizonContract(
            decision_ms=base + 7 * index * core.DAY_MS,
            execution_ms=base + (7 * index + 1) * core.DAY_MS,
            endpoint_ms=base + (7 * index + 8) * core.DAY_MS,
            split="train",
            ic_included=index >= 8,
        )
        for index in range(9)
    ]


def continuous_contracts(base: int) -> list[formal.HorizonContract]:
    """Independent bounded schedule with one train-to-OOS label transition."""
    output = contracts(base)
    output.append(
        formal.HorizonContract(
            decision_ms=base + 63 * core.DAY_MS,
            execution_ms=base + 64 * core.DAY_MS,
            endpoint_ms=base + 71 * core.DAY_MS,
            split="oos",
            ic_included=True,
        )
    )
    return output


def test_normal_active_complete_ic_and_three_cost_paths_have_independent_expected_value():
    base = day(2021, 4, 1)
    fixture = SyntheticFixture(base)
    consumer = formal.FormalConsumer(fixture, GOLD_CONFIG)
    outcomes = [consumer.execute(contract) for contract in contracts(base)]
    actual = outcomes[-1]
    # The fixture has strictly ordered factor inputs and forward returns; the
    # hand-derived Spearman rank correlation is exactly one.
    assert actual.ic == pytest.approx(1.0)
    assert set(actual.states) == {0.0, 0.0015, 0.003}
    assert actual.states[0.0].cost == 0.0
    assert actual.states[0.003].cost >= actual.states[0.0015].cost


def test_ic_exclusion_does_not_skip_any_portfolio_path_or_read_endpoint():
    base = day(2021, 4, 1)
    fixture = SyntheticFixture(base)
    consumer = formal.FormalConsumer(fixture, GOLD_CONFIG)
    warmup = contracts(base)[:8]
    for contract in warmup:
        consumer.execute(contract)
    excluded = formal.HorizonContract(
        decision_ms=base + 56 * core.DAY_MS,
        execution_ms=base + 57 * core.DAY_MS,
        endpoint_ms=base + 64 * core.DAY_MS,
        split="train",
        ic_included=False,
    )
    fixture.missing.add(("A", excluded.endpoint_ms))
    result = consumer.execute(excluded)
    assert result.ic is None
    assert all(state.selected for state in result.states.values())


def test_each_cross_split_or_dataset_horizon_is_runtime_ic_excluded_before_prices_are_read():
    boundaries = [
        row
        for row in horizon.build_rows()
        if row["reason"]
        in {
            horizon.HORIZON_CROSSES_SPLIT,
            horizon.HORIZON_CROSSES_SPLIT_AND_DATASET,
        }
    ]
    assert [row["split"] for row in boundaries] == ["train", "oos", "holdout"]
    for row in boundaries:
        contract = formal.HorizonContract(
            decision_ms=day(*map(int, str(row["decision"]).split("-"))),
            execution_ms=day(*map(int, str(row["execution"]).split("-"))),
            endpoint_ms=day(*map(int, str(row["endpoint"]).split("-"))),
            split=str(row["split"]),
            ic_included=bool(row["ic_included"]),
        )
        contract.validate()
        assert contract.ic_included is False


class ReadCountingVerifiedFormalFixture:
    is_verified_formal = True
    is_synthetic = False

    def __init__(self):
        self.reads = 0

    def _read(self):
        self.reads += 1
        raise AssertionError("contract mismatch must reject before a fixture read")

    universe = lambda self, timestamp: self._read()
    decision_bar = lambda self, symbol, timestamp: self._read()
    execution_open = lambda self, symbol, timestamp: self._read()
    completed_bar = lambda self, symbol, timestamp: self._read()
    lifecycle_as_of = lambda self, symbol, timestamp: self._read()
    forward_return = lambda self, symbol, start, stop: self._read()


def _preflight_contract(row):
    return formal.HorizonContract(
        decision_ms=day(*map(int, str(row["decision"]).split("-"))),
        execution_ms=day(*map(int, str(row["execution"]).split("-"))),
        endpoint_ms=day(*map(int, str(row["endpoint"]).split("-"))),
        split=str(row["split"]),
        ic_included=bool(row["ic_included"]),
    )


def test_verified_formal_binds_every_contract_to_static_preflight_before_reads():
    fixture = ReadCountingVerifiedFormalFixture()
    consumer = formal.FormalConsumer(fixture, GOLD_CONFIG)
    boundaries = [
        row for row in horizon.build_rows()
        if row["reason"] in {horizon.HORIZON_CROSSES_SPLIT, horizon.HORIZON_CROSSES_SPLIT_AND_DATASET}
    ]
    assert [row["split"] for row in boundaries] == ["train", "oos", "holdout"]
    for row in boundaries:
        consumer.validate_contract(_preflight_contract(row))
        expected = _preflight_contract(row)
        tampered = formal.HorizonContract(
            expected.decision_ms, expected.execution_ms, expected.endpoint_ms,
            expected.split, True,
        )
        with pytest.raises(core.PreFormalError, match="differs from horizon preflight"):
            consumer.execute(tampered)
    assert fixture.reads == 0


def test_verified_formal_schedule_must_be_complete_ordered_and_finalized_at_frozen_day_before_reads():
    fixture = ReadCountingVerifiedFormalFixture()
    consumer = formal.FormalConsumer(fixture, GOLD_CONFIG)
    schedule = tuple(_preflight_contract(row) for row in horizon.build_rows())
    assert len(schedule) == 157
    assert consumer.validate_schedule(schedule, formal.FormalConsumer._frozen_final_timestamp()) == schedule
    for invalid in (schedule[:-1], tuple(reversed(schedule))):
        with pytest.raises(core.PreFormalError, match="schedule"):
            consumer.execute_schedule(invalid, formal.FormalConsumer._frozen_final_timestamp())
    with pytest.raises(core.PreFormalError, match="schedule"):
        consumer.execute_schedule(schedule, formal.FormalConsumer._frozen_final_timestamp() - core.DAY_MS)
    assert fixture.reads == 0


def test_complete_horizon_missing_endpoint_is_data_unavailable_not_truncated():
    base = day(2021, 4, 1)
    fixture = SyntheticFixture(base)
    consumer = formal.FormalConsumer(fixture, GOLD_CONFIG)
    all_contracts = contracts(base)
    for contract in all_contracts[:8]:
        consumer.execute(contract)
    fixture.missing.add(("A", all_contracts[-1].endpoint_ms))
    with pytest.raises(formal.FormalDataUnavailable, match="DATA_UNAVAILABLE"):
        consumer.execute(all_contracts[-1])


class FakeBar:
    def __init__(self, open_value, close_value, volume=100.0):
        self.open, self.close, self.quote_volume = open_value, close_value, volume


class FakePriceDataset:
    def __init__(self, bars):
        self.bars = bars

    def universe(self, timestamp):
        if timestamp == -1:
            raise KeyError("PIT absent")
        return ("OLD",)

    def bar(self, symbol, timestamp):
        return self.bars[symbol][timestamp]


def test_confirmed_terminal_uses_only_its_final_close_and_unconfirmed_fails_closed():
    execution, terminal, endpoint = day(2022, 5, 1), day(2022, 5, 3), day(2022, 5, 8)
    event = lifecycle.LifecycleEvent(
        "OLD", lifecycle.TERMINATED_CONFIRMED, terminal + 4 * 60 * 60 * 1000,
        terminal - 2 * 60 * 60 * 1000, terminal
    )
    adapter = formal.PriceV1FormalAdapter(
        FakePriceDataset({"OLD": {execution: FakeBar(100, 100), terminal: FakeBar(90, 110)}}),
        lifecycle.LifecycleResolver({"OLD": event}),
    )
    with pytest.raises(formal.FormalDataUnavailable, match="DATA_UNAVAILABLE"):
        adapter.forward_return("OLD", execution, endpoint)

    unresolved = lifecycle.LifecycleEvent(
        "OLD", lifecycle.TERMINATED_UNCONFIRMED, terminal, terminal - core.DAY_MS, terminal
    )
    unresolved_adapter = formal.PriceV1FormalAdapter(
        FakePriceDataset({"OLD": {execution: FakeBar(100, 100), terminal: FakeBar(90, 110)}}),
        lifecycle.LifecycleResolver({"OLD": unresolved}),
    )
    with pytest.raises(formal.FormalDataUnavailable, match="DATA_UNAVAILABLE"):
        unresolved_adapter.forward_return("OLD", execution, endpoint)


def test_pit_mismatch_and_internal_required_gap_fail_closed():
    adapter = formal.PriceV1FormalAdapter(
        FakePriceDataset({"OLD": {}}), lifecycle.LifecycleResolver({})
    )
    with pytest.raises(formal.FormalDataUnavailable, match="PIT universe"):
        adapter.universe(-1)
    with pytest.raises(formal.FormalDataUnavailable, match="DATA_UNAVAILABLE"):
        adapter.completed_bar("OLD", day(2022, 1, 2))


class PreFormalUnavailableFixture(SyntheticFixture):
    def __init__(self, base, failure):
        super().__init__(base)
        self.failure = failure
        self.root = KeyError("independent missing input")

    def universe(self, effective_timestamp):
        if self.failure == "universe":
            raise formal.FormalDataUnavailable("DATA_UNAVAILABLE: preserved PIT") from self.root
        return super().universe(effective_timestamp)

    def lifecycle_as_of(self, symbol, timestamp):
        if self.failure == "lifecycle":
            raise formal.FormalDataUnavailable("DATA_UNAVAILABLE: preserved lifecycle") from self.root
        return super().lifecycle_as_of(symbol, timestamp)


@pytest.mark.parametrize("failure, expected", [("universe", "PIT"), ("lifecycle", "lifecycle")])
def test_core_preserves_adapter_data_unavailable_class_and_cause(failure, expected):
    fixture = PreFormalUnavailableFixture(day(2021, 4, 1), failure)
    with pytest.raises(formal.FormalDataUnavailable, match=expected) as raised:
        core.Engine(fixture, GOLD_CONFIG).execute(core.Decision(fixture.base, "train"))
    assert isinstance(raised.value.__cause__, KeyError)


def test_consumer_schedule_has_continuous_three_cost_accounting_across_split_and_finalizes():
    base = day(2021, 4, 1)
    final_timestamp = base + 67 * core.DAY_MS
    consumer = formal.FormalConsumer(SyntheticFixture(base), GOLD_CONFIG)
    outcome = consumer.execute_schedule(continuous_contracts(base), final_timestamp)
    assert len(outcome.decisions) == 10
    assert all(item.ic is None for item in outcome.decisions[:8])
    # Independent fixture construction makes momentum and forward returns
    # strictly ordered A < ... < E: calm IC is +1, while the later high-vol
    # reversal orientation is -1.
    assert [item.ic for item in outcome.decisions[8:]] == pytest.approx([1.0, -1.0])
    assert outcome.decisions[-2].states[core.COST].regime in {"calm", "high"}
    assert outcome.decisions[-1].states[core.COST].regime in {"calm", "high"}
    assert set(outcome.accounting) == {0.0, core.COST, 0.003}
    for cost, ledger in outcome.accounting.items():
        open_entries = [entry for entry in ledger if entry.phase == "OPEN"]
        final_entries = [entry for entry in ledger if entry.phase == "FINAL_CLOSE"]
        assert [entry.timestamp for entry in open_entries] == list(
            range(base, final_timestamp + core.DAY_MS, core.DAY_MS)
        )
        assert len(final_entries) == 1
        assert final_entries[0].timestamp == final_timestamp
        assert final_entries[0].finalization is True
        assert final_entries[0].notionals == ()
        assert final_entries[0].turnover > 0
        assert final_entries[0].cost >= 0
        assert outcome.lifecycle_exits[cost] == ()
        assert outcome.final_liquidation_exits[cost]
    assert outcome.accounting[0.0][-1].cost == 0.0
    assert outcome.accounting[core.COST][-1].cost > 0.0
    assert outcome.accounting[0.003][-1].cost > outcome.accounting[core.COST][-1].cost
    assert not hasattr(outcome, "total_return")
    assert not hasattr(outcome, "sharpe")
    assert not hasattr(outcome, "max_drawdown")


def test_finalization_independently_marks_open_to_close_then_charges_each_exit():
    final_timestamp = day(2021, 6, 7)
    fixture = SyntheticFixture(day(2021, 4, 1))
    penultimate_timestamp = final_timestamp - core.DAY_MS
    fixture.bars["A"][penultimate_timestamp] = core.CompletedBar(100.0, 100.0)
    fixture.bars["E"][penultimate_timestamp] = core.CompletedBar(100.0, 100.0)
    fixture.bars["A"][final_timestamp] = core.CompletedBar(120.0, 144.0)
    fixture.bars["E"][final_timestamp] = core.CompletedBar(80.0, 64.0)
    engine = core.Engine(fixture, GOLD_CONFIG)
    engine.portfolio.cash = 1.0
    engine.portfolio.notionals = {"A": 0.5, "E": -0.5}
    engine.last_mark_ms = penultimate_timestamp
    engine.last_execution_ms = penultimate_timestamp - core.DAY_MS
    exits = engine.finalize(final_timestamp)
    # Frozen interval starts: penultimate open -> final open gives A=.6/E=-.4
    # and NAV=1.2; final open -> final close gives A=.72/E=-.32 and NAV=1.4.
    final_open_nav = 1.2
    nav_before_a = 1.4
    a_cost = 0.72 * core.COST
    e_cost = 0.32 * core.COST
    expected_turnover = (0.72 + 0.32) / nav_before_a
    assert [(item.symbol, item.timestamp) for item in exits] == [
        ("A", final_timestamp), ("E", final_timestamp)
    ]
    assert exits[0].nav_before == pytest.approx(nav_before_a)
    assert exits[0].turnover == pytest.approx(0.72 / nav_before_a)
    assert exits[0].cost == pytest.approx(a_cost)
    assert exits[1].nav_before == pytest.approx(nav_before_a)
    assert exits[1].turnover == pytest.approx(0.32 / nav_before_a)
    assert exits[1].cost == pytest.approx(e_cost)
    open_entry, final_entry = engine.accounting[-2:]
    assert (open_entry.timestamp, open_entry.phase) == (final_timestamp, "OPEN")
    assert open_entry.nav == pytest.approx(final_open_nav)
    assert (final_entry.timestamp, final_entry.phase) == (final_timestamp, "FINAL_CLOSE")
    assert final_entry.turnover == pytest.approx(expected_turnover)
    assert final_entry.cost == pytest.approx(a_cost + e_cost)
    assert final_entry.nav == pytest.approx(nav_before_a - a_cost - e_cost)


class HeldTerminalScheduleFixture(SyntheticFixture):
    def __init__(self, base, terminal):
        super().__init__(base)
        self.symbols = (*self.symbols, "F")
        self.bars["F"] = dict(self.bars["D"])
        self.terminal = terminal

    def lifecycle_as_of(self, symbol, timestamp):
        if symbol == "E" and timestamp >= self.terminal:
            return core.LifecycleStatus(False, self.terminal)
        return core.LifecycleStatus(True)


def test_schedule_separates_held_confirmed_terminal_from_final_target_to_cash_exits():
    base = day(2021, 4, 1)
    terminal = base + 59 * core.DAY_MS  # after the first live target, before next decision
    final_timestamp = base + 67 * core.DAY_MS
    outcome = formal.FormalConsumer(
        HeldTerminalScheduleFixture(base, terminal), GOLD_CONFIG
    ).execute_schedule(continuous_contracts(base), final_timestamp)
    for cost in (0.0, core.COST, 0.003):
        assert [(item.symbol, item.timestamp) for item in outcome.lifecycle_exits[cost]] == [
            ("E", terminal)
        ]
        assert all(item.symbol != "E" for item in outcome.final_liquidation_exits[cost])
        assert outcome.final_liquidation_exits[cost]


def test_adapter_resolver_terminal_exit_enters_consumer_accounting_without_forward_fill():
    terminal = day(2022, 5, 3)
    event = lifecycle.LifecycleEvent(
        "OLD", lifecycle.TERMINATED_CONFIRMED,
        terminal + 4 * 60 * 60 * 1000, terminal - 2 * 60 * 60 * 1000, terminal,
    )
    adapter = formal.PriceV1FormalAdapter(
        FakePriceDataset({"OLD": {terminal: FakeBar(100, 110)}}),
        lifecycle.LifecycleResolver({"OLD": event}),
    )
    engine = core.Engine(adapter, GOLD_CONFIG)
    engine.portfolio.cash = 1.0
    engine.portfolio.notionals = {"OLD": 0.5}
    engine.last_mark_ms = terminal
    engine.last_execution_ms = terminal - core.DAY_MS
    exits = engine.advance_to(terminal + core.DAY_MS)
    assert [(item.symbol, item.timestamp) for item in exits] == [("OLD", terminal)]
    assert engine.portfolio.notionals == {}
    assert engine.accounting[-1].exits == exits
    assert engine.accounting[-1].nav == pytest.approx(1.0 + 0.55 - 0.55 * core.COST)


def test_formal_consumer_matches_reviewed_core_semantics_on_same_synthetic_fixture():
    base = day(2021, 4, 1)
    reference_fixture = SyntheticFixture(base)
    reference = core.Engine(reference_fixture, GOLD_CONFIG)
    actual_fixture = SyntheticFixture(base)
    consumer = formal.FormalConsumer(actual_fixture, GOLD_CONFIG)
    for contract in contracts(base):
        expected = reference.execute(core.Decision(contract.decision_ms, contract.split))
        received = consumer.execute(contract).states[core.COST]
        assert formal.FormalConsumer._semantic_tuple(received) == formal.FormalConsumer._semantic_tuple(expected)


def test_actual_adapter_is_consumer_ready_and_legacy_formal_entry_remains_locked():
    adapter = formal.PriceV1FormalAdapter(FakePriceDataset({"OLD": {}}), lifecycle.LifecycleResolver({}))
    consumer = formal.FormalConsumer(adapter, GOLD_CONFIG)
    assert consumer.fixture is adapter
    assert core.FORMAL_RUN_ID is None
    with pytest.raises(core.FormalRunLockedError, match="FORMAL_RUN_LOCKED"):
        core.formal_run()


def test_real_snapshot_can_bind_to_sidecar_without_reading_performance():
    data_root = ROOT.parent / "gmaq-data"
    if not (data_root / "registry.sqlite").is_file():
        pytest.skip("local immutable Price V1 snapshot is not installed")
    adapter = formal.load_verified_price_lifecycle_adapter(data_root)
    assert adapter.is_verified_formal is True
    assert adapter.is_synthetic is False
    assert len(adapter.universe(day(2022, 6, 1))) > 0


def test_no_actual_performance_output_or_serialization_surface():
    tree = ast.parse((ROOT / "research" / "exploration" / "expl_017_formal_consumer.py").read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "print" not in called
    assert "open" not in called
    assert formal.FORMAL_METRICS_EXPOSED is False
    assert formal.FORMAL_RUN_ID is None
