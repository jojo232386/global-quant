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
    assert adapter.forward_return("OLD", execution, endpoint) == pytest.approx(0.10)

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


def test_actual_adapter_and_legacy_formal_entry_remain_execution_locked():
    adapter = formal.PriceV1FormalAdapter(FakePriceDataset({"OLD": {}}), lifecycle.LifecycleResolver({}))
    with pytest.raises(formal.FormalMetricsExposureError, match="prohibited"):
        formal.FormalConsumer(adapter, GOLD_CONFIG)
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
