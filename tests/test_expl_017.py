from __future__ import annotations

import ast
import datetime as dt
import pathlib
import statistics
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "exploration"))
sys.path.insert(0, str(pathlib.Path(__file__).with_name("oracles")))
import expl_017 as r  # noqa: E402
import expl_017_impl_002_oracle as oracle  # noqa: E402


def ms(year: int, month: int, day: int = 1) -> int:
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


class FX:
    is_synthetic = True

    def __init__(self, base: int, symbols=("A", "B", "C", "D", "E")):
        self.ss = symbols
        self.dec = {symbol: {} for symbol in symbols}
        self.op = {symbol: {} for symbol in symbols}
        self.bar = {symbol: {} for symbol in symbols}
        self.events = {symbol: None for symbol in symbols}
        self.lifecycle_calls = []
        for index, timestamp in enumerate(
            range(base - 200 * r.DAY_MS, base + 50 * r.DAY_MS, r.DAY_MS)
        ):
            for offset, symbol in enumerate(symbols):
                price = (100 if index % 2 == 0 else 120) + offset
                self.dec[symbol][timestamp] = r.DecisionBar(price, 1000 + offset)
                self.op[symbol][timestamp] = price
                self.bar[symbol][timestamp] = r.CompletedBar(price, price)

    def universe(self, effective_timestamp):
        return tuple(sorted(self.ss))

    def decision_bar(self, symbol, timestamp):
        return self.dec[symbol][timestamp]

    def execution_open(self, symbol, timestamp):
        return self.op[symbol][timestamp]

    def completed_bar(self, symbol, timestamp):
        return self.bar[symbol][timestamp]

    def lifecycle_as_of(self, symbol, timestamp):
        self.lifecycle_calls.append((symbol, timestamp))
        event = self.events[symbol]
        if event is None or event > timestamp:
            return r.LifecycleStatus(True)
        return r.LifecycleStatus(False, event)

    def setd(self, symbol, timestamp, close=None, volume=None):
        current = self.dec[symbol][timestamp]
        self.dec[symbol][timestamp] = r.DecisionBar(
            current.close if close is None else close,
            current.quote_volume if volume is None else volume,
        )

    def seto(self, symbol, timestamp, value):
        self.op[symbol][timestamp] = value

    def setb(self, symbol, timestamp, open_value=None, close_value=None):
        current = self.bar[symbol][timestamp]
        self.bar[symbol][timestamp] = r.CompletedBar(
            current.open if open_value is None else open_value,
            current.close if close_value is None else close_value,
        )


GOLD_CONFIG = r.Config(n=5, horizons=(1, 2), volatility_window=2)


def fx(base, event=None, six=False):
    fixture = FX(
        base, ("A", "B", "C", "D", "E", "F") if six else ("A", "B", "C", "D", "E")
    )
    closes = {
        "A": (100, 102, 104),
        "B": (100, 101, 102),
        "C": (100, 100, 100.5),
        "D": (100, 99, 98),
        "E": (100, 98, 96),
    }
    for symbol, values in closes.items():
        for day_offset, value in zip((-2, -1, 0), values):
            fixture.setd(symbol, base + day_offset * r.DAY_MS, close=value)
        for day_offset, value in zip((5, 6, 7), values):
            fixture.setd(symbol, base + day_offset * r.DAY_MS, close=value)
    execution = base + r.DAY_MS
    fixture.seto("A", execution, 100)
    fixture.seto("E", execution, 100)
    fixture.setb("A", execution, 100, 100)
    fixture.setb("E", execution, 100, 80)
    fixture.seto("A", execution + r.DAY_MS, 200)
    fixture.seto("E", execution + r.DAY_MS, 100)
    fixture.setb("A", execution + r.DAY_MS, 200, 200)
    fixture.setb("E", execution + r.DAY_MS, 100, 100)
    for timestamp in range(
        execution + 2 * r.DAY_MS, base + 10 * r.DAY_MS, r.DAY_MS
    ):
        fixture.seto("A", timestamp, 200)
        fixture.seto("E", timestamp, 100)
        fixture.setb("A", timestamp, 200, 200)
        fixture.setb("E", timestamp, 100, 100)
    if event is not None:
        fixture.events["E"] = event
    if six:
        for timestamp in range(
            base - 90 * r.DAY_MS, base + 10 * r.DAY_MS, r.DAY_MS
        ):
            fixture.setd("F", timestamp, volume=1)
    return fixture


def nine(engine, base):
    return [
        engine.execute(r.Decision(base - (8 - index) * 7 * r.DAY_MS, "train"))
        for index in range(9)
    ]


def snapshot(engine):
    return (
        engine.last_mark_ms,
        engine.last_execution_ms,
        tuple(sorted(engine._terminal_events.items())),
        frozenset(engine.terminated_symbols),
        engine.portfolio.cash,
        tuple(sorted(engine.portfolio.notionals.items())),
        tuple(engine.train),
        engine.fixed,
        engine.left_train,
    )


def test_oracle_nav_turnover_and_switch():
    base = ms(2021, 4, 1)
    expected = oracle.oracle_values()
    engine = r.Engine(fx(base), GOLD_CONFIG)
    first = nine(engine, base)[-1]
    assert first.momentum == pytest.approx(expected["momentum"])
    assert first.volatility == pytest.approx(expected["broad_volatility"])
    assert first.target == expected["calm_target"]

    state = engine.execute(r.Decision(base + 7 * r.DAY_MS, "oos"))
    assert state.nav_before_trade == pytest.approx(expected["drift_nav"])
    assert state.incumbent == pytest.approx(expected["drift_weights"])
    assert state.turnover == pytest.approx(expected["drift_turnover"])
    assert state.cost == pytest.approx(expected["drift_cost_dollars"])

    fixture = fx(base)
    for symbol in "ABC":
        fixture.setd(symbol, base + 5 * r.DAY_MS, close=100)
        fixture.setd(
            symbol,
            base + 6 * r.DAY_MS,
            close=10000 if symbol != "A" else 102,
        )
        fixture.setd(
            symbol,
            base + 7 * r.DAY_MS,
            close=100 if symbol != "A" else 10000,
        )
    switched = r.Engine(fixture, GOLD_CONFIG)
    nine(switched, base)
    state = switched.execute(r.Decision(base + 7 * r.DAY_MS, "oos"))
    assert state.target == {"A": -0.5, "E": 0.5}
    assert state.turnover == pytest.approx(2.001001001001001)
    assert state.cost == pytest.approx(0.00449775)


def test_prior_terminal_unheld_missing_current_bar_and_open_is_excluded():
    base = ms(2021, 4, 1)
    fixture = fx(base, base - r.DAY_MS, True)
    del fixture.bar["E"][base]
    del fixture.op["E"][base + r.DAY_MS]
    engine = r.Engine(fixture, GOLD_CONFIG)
    state = nine(engine, base)[-1]
    assert "E" in engine.terminated_symbols
    assert "E" not in state.selected
    assert "F" in state.selected


def test_terminal_at_decision_close_held_exits_before_rank():
    base = ms(2021, 4, 1)
    fixture = fx(base, base + 7 * r.DAY_MS, True)
    engine = r.Engine(fixture, GOLD_CONFIG)
    assert "E" in nine(engine, base)[-1].target
    state = engine.execute(r.Decision(base + 7 * r.DAY_MS, "oos"))
    assert [item.symbol for item in state.exits] == ["E"]
    assert "E" not in state.selected
    assert "E" not in engine.portfolio.notionals


def test_tplusone_event_not_queried_at_t_enters_then_exits_once():
    base = ms(2021, 4, 1)
    expected = oracle.oracle_values()
    fixture = fx(base, base + r.DAY_MS, True)
    fixture.seto("A", base + 2 * r.DAY_MS, 110)
    fixture.setb("A", base + 2 * r.DAY_MS, 110, 110)
    engine = r.Engine(fixture, GOLD_CONFIG)
    entered = nine(engine, base)[-1]
    assert "E" in entered.selected
    assert ("E", base + r.DAY_MS) not in fixture.lifecycle_calls
    exits = engine.advance_to(base + 2 * r.DAY_MS)
    assert len(exits) == 1 and exits[0].symbol == "E"
    assert exits[0].nav_before == pytest.approx(expected["terminal_nav_before_exit"])
    assert exits[0].turnover == pytest.approx(expected["terminal_exit_turnover"])
    assert exits[0].cost == pytest.approx(expected["terminal_exit_cost_dollars"])
    assert engine.portfolio.nav() == pytest.approx(expected["terminal_final_nav"])


def test_active_missing_data_hardstops_and_clocks_and_threshold():
    base = ms(2021, 4, 1)
    engine = r.Engine(fx(base), GOLD_CONFIG)
    train = nine(engine, base)
    assert all(not state.target for state in train[:8])
    assert train[-1].threshold == pytest.approx(
        statistics.median(state.volatility for state in train[:8])
    )
    engine.advance_to(base + 3 * r.DAY_MS)
    assert engine.execute(r.Decision(base + 7 * r.DAY_MS, "oos")).selected

    bad = fx(base, None, True)
    for index in range(90):
        bad.setd("A", base - index * r.DAY_MS, volume=1e9)
    del bad.op["A"][base + r.DAY_MS]
    with pytest.raises(r.PreFormalError, match="execution open absent"):
        r.Engine(bad, GOLD_CONFIG).execute(r.Decision(base, "train"))
    with pytest.raises(r.FormalRunLockedError):
        r.formal_run()

    source = (ROOT / "research" / "exploration" / "expl_017.py").read_text()
    assert "last_timestamp" not in source
    tree = ast.parse(source)
    assert not any(
        "oracle" in alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )


def test_future_or_contradictory_asof_status_hardstops():
    base = ms(2021, 4, 1)
    fixture = fx(base)
    fixture.lifecycle_as_of = lambda symbol, timestamp: r.LifecycleStatus(
        False, timestamp + r.DAY_MS
    )
    with pytest.raises(r.PreFormalError, match="future/malformed terminal event"):
        r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))

    fixture = fx(base)
    fixture.lifecycle_as_of = lambda symbol, timestamp: r.LifecycleStatus(
        True, timestamp
    )
    with pytest.raises(r.PreFormalError, match="contradictory lifecycle status"):
        r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))


def test_oos_statistic_cannot_contaminate_frozen_train_threshold():
    base = ms(2021, 4, 1)
    baseline = r.Engine(fx(base), GOLD_CONFIG)
    nine(baseline, base)
    mutated_fixture = fx(base)
    for symbol in "ABC":
        mutated_fixture.setd(symbol, base + 7 * r.DAY_MS, close=10000)
    mutated = r.Engine(mutated_fixture, GOLD_CONFIG)
    nine(mutated, base)
    left = baseline.execute(r.Decision(base + 7 * r.DAY_MS, "oos"))
    right = mutated.execute(r.Decision(base + 7 * r.DAY_MS, "oos"))
    assert left.threshold == pytest.approx(right.threshold)
    assert left.volatility != pytest.approx(right.volatility)


def test_strict_lifecycle_types_and_persisted_monotonicity():
    base = ms(2021, 4, 1)
    for active in ("true", 1):
        fixture = fx(base)
        fixture.lifecycle_as_of = (
            lambda symbol, timestamp, value=active: r.LifecycleStatus(value)
        )
        with pytest.raises(r.PreFormalError, match="malformed lifecycle active"):
            r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))
    for terminal in (True, 1.0, base + 1):
        fixture = fx(base)
        fixture.lifecycle_as_of = (
            lambda symbol, timestamp, value=terminal: r.LifecycleStatus(False, value)
        )
        with pytest.raises(r.PreFormalError, match="future/malformed terminal event"):
            r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))

    engine = r.Engine(fx(base), GOLD_CONFIG)
    engine._terminal_events["A"] = base
    with pytest.raises(r.PreFormalError, match="terminal reactivation"):
        engine._read_status("A", base)

    fixture = fx(base)
    engine = r.Engine(fixture, GOLD_CONFIG)
    engine._terminal_events["A"] = base
    fixture.lifecycle_as_of = lambda symbol, timestamp: r.LifecycleStatus(
        False, base - r.DAY_MS
    )
    with pytest.raises(r.PreFormalError, match="terminal timestamp changed"):
        engine._read_status("A", base)


def test_universe_boundary_and_member_errors_are_normalized():
    base = ms(2021, 4, 1)
    for failure in (
        KeyError("x"),
        AttributeError("x"),
        TypeError("x"),
        ValueError("x"),
    ):
        fixture = fx(base)
        fixture.universe = lambda effective, error=failure: (_ for _ in ()).throw(
            error
        )
        with pytest.raises(r.PreFormalError, match="PIT universe unavailable"):
            r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))

    invalid = (
        None,
        "ABCDE",
        b"ABCDE",
        (symbol for symbol in "ABCDE"),
        ("A", 1),
        ("A", []),
        ("",),
        (" A",),
        ("a",),
        ("A-",),
        ("Ａ",),
        ("A", "A"),
        ("B", "A", "C", "D", "E"),
    )
    for raw in invalid:
        fixture = fx(base)
        fixture.universe = lambda effective, value=raw: value
        with pytest.raises(r.PreFormalError):
            r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))

    for raw in [("A", "B", "C", "D", "E"), ["A", "B", "C", "D", "E"]]:
        fixture = fx(base)
        fixture.universe = lambda effective, value=raw: value
        assert r.Engine(fixture, GOLD_CONFIG).execute(r.Decision(base, "train"))


def test_mixed_valid_terminal_then_malformed_status_restores_all_state():
    base = ms(2021, 4, 1)
    fixture = fx(base)

    def lifecycle(symbol, timestamp):
        if symbol == "A":
            return r.LifecycleStatus(False, timestamp)
        if symbol == "B":
            return "malformed"
        return r.LifecycleStatus(True)

    fixture.lifecycle_as_of = lifecycle
    engine = r.Engine(fixture, GOLD_CONFIG)
    before = snapshot(engine)
    with pytest.raises(r.PreFormalError, match="malformed lifecycle status"):
        engine.advance_to(base, ("A", "B"))
    assert snapshot(engine) == before


def test_accessor_typeerror_after_earlier_member_restores_all_state():
    base = ms(2021, 4, 1)
    fixture = fx(base)

    def lifecycle(symbol, timestamp):
        if symbol == "A":
            return r.LifecycleStatus(False, timestamp)
        if symbol == "B":
            raise TypeError("broken accessor")
        return r.LifecycleStatus(True)

    fixture.lifecycle_as_of = lifecycle
    engine = r.Engine(fixture, GOLD_CONFIG)
    before = snapshot(engine)
    with pytest.raises(r.PreFormalError, match="lifecycle status unavailable"):
        engine.advance_to(base, ("A", "B"))
    assert snapshot(engine) == before


def test_missing_held_terminal_bar_or_next_open_restores_all_state():
    base = ms(2021, 4, 1)
    execution = base + r.DAY_MS

    terminal_fixture = fx(base, execution)
    terminal_engine = r.Engine(terminal_fixture, GOLD_CONFIG)
    nine(terminal_engine, base)
    del terminal_fixture.bar["E"][execution]
    before = snapshot(terminal_engine)
    with pytest.raises(r.PreFormalError, match="completed bar absent"):
        terminal_engine.advance_to(execution + r.DAY_MS)
    assert snapshot(terminal_engine) == before

    open_fixture = fx(base)
    open_engine = r.Engine(open_fixture, GOLD_CONFIG)
    nine(open_engine, base)
    del open_fixture.op["A"][execution + r.DAY_MS]
    before = snapshot(open_engine)
    with pytest.raises(r.PreFormalError, match="execution open absent"):
        open_engine.advance_to(execution + r.DAY_MS)
    assert snapshot(open_engine) == before


def test_multiday_second_day_failure_restores_all_state():
    base = ms(2021, 4, 1)
    execution = base + r.DAY_MS
    fixture = fx(base)
    engine = r.Engine(fixture, GOLD_CONFIG)
    nine(engine, base)
    del fixture.op["A"][execution + 2 * r.DAY_MS]
    before = snapshot(engine)
    with pytest.raises(r.PreFormalError, match="execution open absent"):
        engine.advance_to(execution + 2 * r.DAY_MS)
    assert snapshot(engine) == before


def test_execute_lifecycle_mutation_then_missing_selected_open_restores_all_state():
    base = ms(2021, 4, 1)
    fixture = fx(base, None, True)
    engine = r.Engine(fixture, GOLD_CONFIG)
    nine(engine, base)
    decision = base + 7 * r.DAY_MS
    fixture.events["F"] = decision
    del fixture.op["A"][decision + r.DAY_MS]
    before = snapshot(engine)
    with pytest.raises(r.PreFormalError, match="execution open absent"):
        engine.execute(r.Decision(decision, "oos"))
    assert snapshot(engine) == before


def test_execute_post_regime_failure_restores_threshold_and_split_state():
    base = ms(2021, 4, 1)
    fixture = fx(base, None, True)
    engine = r.Engine(fixture, GOLD_CONFIG)
    nine(engine, base)
    decision = base + 7 * r.DAY_MS
    for index in range(90):
        fixture.setd("F", decision - index * r.DAY_MS, volume=1e9)
    # F displaces incumbent A from selection, so A's absent execution open is
    # reached only after the OOS threshold and split state have been mutated.
    del fixture.op["A"][decision + r.DAY_MS]
    before = snapshot(engine)
    with pytest.raises(r.PreFormalError, match="execution open absent"):
        engine.execute(r.Decision(decision, "oos"))
    assert snapshot(engine) == before


def test_same_asof_terminal_observation_is_idempotent():
    base = ms(2021, 4, 1)
    fixture = fx(base, base)
    engine = r.Engine(fixture, GOLD_CONFIG)
    engine.advance_to(base, ("E",))
    before = snapshot(engine)
    engine.advance_to(base, ("E",))
    assert snapshot(engine) == before
