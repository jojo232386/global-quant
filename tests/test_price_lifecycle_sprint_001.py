import json
import math
import pathlib

import pytest

from research.exploration import price_lifecycle_sprint_001 as sprint
from research.exploration.price_alpha_v1 import Bar, PriceDataset


DAY = sprint.DAY_MS


def _dataset(symbols, days=50):
    bars = {}
    for rank, symbol in enumerate(symbols):
        points = {}
        for day in range(days):
            # Distinct, strictly positive deterministic histories; tests never
            # load Price V1 or another real dataset.
            close = 100.0 + rank + day * (1.0 + rank / 100.0)
            points[day * DAY] = Bar(close, close, 1000.0 + rank * 100.0 + day)
        bars[symbol] = points
    return PriceDataset(bars=bars, last_timestamp={symbol: (days - 1) * DAY for symbol in symbols}, pit={}, artifact_path=pathlib.Path("synthetic"), manifest_sha256="synthetic", pit_sha256="synthetic", labels=("synthetic",))


def _master(symbols, terminal=None):
    return {"records": [{"symbol": symbol, "terminal_timestamp_utc": terminal.get(symbol) if terminal else None} for symbol in symbols]}


def test_gold_sample_is_explicit_and_non_market_data():
    payload = json.loads((sprint.ROOT / "research/exploration/price-lifecycle-sprint-001-gold-sample.json").read_text())
    assert payload["program_id"] == sprint.PROGRAM_ID
    assert payload["examples"]["shock_signal"]["expected_signal"] == -5.0
    assert payload["examples"]["volume_share_signal"]["expected_signal"] == pytest.approx(math.log(0.7) - math.log(0.5))
    terminal = payload["examples"]["terminal_cost_and_drift"]
    assert terminal["terminal_liquidation_turnover_after_marking"] == pytest.approx(
        0.5 * 1.1
    )
    assert terminal["stress_net_return"] == pytest.approx(
        0.05 - sprint.STRESS_COST * 1.55
    )
    assert terminal["post_interval_B_weight"] == pytest.approx(-0.5 / 1.04535)
    final_exit = payload["examples"]["final_exit"]
    assert final_exit["turnover"] == pytest.approx(abs(terminal["post_interval_B_weight"]))
    assert final_exit["stress_net_return"] == pytest.approx(
        -sprint.STRESS_COST * final_exit["turnover"]
    )
    assert "not Price V1" in payload["purpose"]


def test_target_uses_equal_dollar_neutral_legs_and_canonical_ties():
    target = sprint._target({"S00": 0.0, "S01": 0.0, "S02": 1.0, "S03": 2.0, "S04": 3.0, "S05": 4.0, "S06": 5.0, "S07": 6.0, "S08": 7.0, "S09": 8.0}, minimum=10)
    assert target == {"S00": -0.25, "S01": -0.25, "S08": 0.25, "S09": 0.25}


def test_simulation_books_terminal_then_cost_and_final_exit_weight_drift():
    symbols = ("A", "B")
    data = _dataset(symbols, days=3)
    data.bars["A"][0] = Bar(100.0, 110.0, 1.0)
    data.bars["B"][0] = Bar(100.0, 100.0, 1.0)
    data.bars["B"][DAY] = Bar(100.0, 100.0, 1.0)
    data.bars["B"][2 * DAY] = Bar(100.0, 100.0, 1.0)
    master = _master(symbols, {"A": "1970-01-01T12:00:00Z"})
    config = sprint.Config("C", "H", "shock_reversal", None, None, 20, 2, 0, 0, 2 * DAY)
    event = sprint.SignalEvent(0, -DAY, {"A": 0.5, "B": -0.5}, {"A": 1.0, "B": 0.0}, {"A": 0.0, "B": 0.0})
    result = sprint.simulate(data, master, config, [event])
    assert result.gross == pytest.approx([0.05, 0.0, 0.0])
    assert result.turnover == pytest.approx([1.55, 0.0, 0.5 / 1.04535])
    assert result.rebalance_turnover == pytest.approx([1.0])
    assert result.terminal_liquidations == [{"symbol": "A", "date": "1970-01-01T00:00:00.000Z", "method": "canonical_terminal_open_to_final_close_then_exit"}]
    assert sprint._net(result, sprint.STRESS_COST)[0] == pytest.approx(0.04535)
    assert sprint._net(result, sprint.STRESS_COST)[-1] == pytest.approx(
        -sprint.STRESS_COST * (0.5 / 1.04535)
    )

    base = sprint.simulate(data, master, config, [event], cost_rate=sprint.BASE_COST)
    assert base.turnover[-1] == pytest.approx(0.5 / 1.047675)
    assert base.turnover[-1] != pytest.approx(result.turnover[-1])


def test_metrics_geometric_drawdown_and_hac_are_fixed():
    metrics = sprint._metrics([0.01, -0.02], [1.0, 0.0])
    assert metrics["total_return"] == pytest.approx(-0.0102)
    assert metrics["maximum_drawdown"] == pytest.approx(-0.02)
    assert metrics["annualized_sharpe"] == pytest.approx((-0.005 / math.sqrt(0.00045)) * math.sqrt(365.0))
    hac = sprint.hac_mean_test([0.1, 0.2, 0.3, 0.4], lag=3)
    assert hac["n"] == 4
    assert hac["p_one_sided_normal"] is not None


def test_shock_events_use_master_membership_not_price_v1_pit(monkeypatch):
    symbols = tuple(f"S{index:02d}" for index in range(10))
    data = _dataset(symbols, days=40)
    master = _master(symbols)
    monkeypatch.setattr(sprint, "universe_at", lambda payload, timestamp: symbols)
    config = sprint.Config("C", "H", "shock_reversal", None, None, 20, 3, 25 * DAY, 25 * DAY, 28 * DAY)
    events = sprint.build_events(data, master, config)
    assert len(events) == 1
    assert events[0].decision == 24 * DAY
    assert len(events[0].target) == 4


def test_master_queries_completed_decision_close_and_exact_execution_open(monkeypatch):
    symbols = tuple(f"S{index:02d}" for index in range(10))
    data = _dataset(symbols, days=40)
    queried = []

    def record_query(payload, timestamp):
        queried.append(timestamp)
        return symbols

    monkeypatch.setattr(sprint, "universe_at", record_query)
    config = sprint.Config(
        "C", "H", "shock_reversal", None, None, 20, 3, 25 * DAY, 25 * DAY, 28 * DAY
    )
    sprint.build_events(data, _master(symbols), config)
    assert queried == [
        "1970-01-25T23:59:59.999Z",
        "1970-01-26T00:00:00.000Z",
    ]


def test_event_selection_does_not_validate_unavailable_execution_day_fields(
    monkeypatch,
):
    symbols = tuple(f"S{index:02d}" for index in range(10))
    data = _dataset(symbols, days=40)
    for symbol in symbols:
        execution = data.bars[symbol][25 * DAY]
        data.bars[symbol][25 * DAY] = Bar(execution.open, math.nan, math.nan)
    monkeypatch.setattr(sprint, "universe_at", lambda payload, timestamp: symbols)
    config = sprint.Config(
        "C", "H", "shock_reversal", None, None, 20, 3, 25 * DAY, 25 * DAY, 28 * DAY
    )
    events = sprint.build_events(data, _master(symbols), config)
    assert len(events) == 1


def test_volume_share_uses_same_eligible_cohort_denominators(monkeypatch):
    symbols = tuple(f"S{index:02d}" for index in range(20))
    data = _dataset(symbols, days=50)
    # Last seven volumes for S00 are high, but its long-window share is held
    # fixed relative to the same 20-symbol cohort by the deterministic setup.
    for offset in range(7):
        data.bars["S00"][34 * DAY - offset * DAY] = Bar(100.0, 100.0, 5000.0)
    master = _master(symbols)
    monkeypatch.setattr(sprint, "universe_at", lambda payload, timestamp: symbols)
    config = sprint.Config("C", "V", "volume_share", 7, 28, None, 7, 35 * DAY, 35 * DAY, 42 * DAY)
    events = sprint.build_events(data, master, config)
    assert events[0].signal["S00"] > events[0].signal["S01"]
    assert "S00" in events[0].target


def test_invalid_quote_volume_excludes_symbol_before_minimum_check(monkeypatch):
    symbols = tuple(f"S{index:02d}" for index in range(21))
    data = _dataset(symbols, days=50)
    invalid = data.bars["S00"][34 * DAY]
    data.bars["S00"][34 * DAY] = Bar(invalid.open, invalid.close, math.nan)
    monkeypatch.setattr(sprint, "universe_at", lambda payload, timestamp: symbols)
    config = sprint.Config(
        "C", "V", "volume_share", 7, 28, None, 7, 35 * DAY, 35 * DAY, 42 * DAY
    )
    event = sprint.build_events(data, _master(symbols), config)[0]
    assert "S00" not in event.signal
    assert len(event.signal) == 20


def test_partial_horizon_and_unknown_candidate_fail_closed():
    data = _dataset(("A",), days=5)
    master = _master(("A",))
    bad = sprint.Config("C", "H", "shock_reversal", None, None, 20, 3, 0, 0, 2 * DAY)
    with pytest.raises(sprint.SprintDataError, match="partial holding horizon"):
        sprint.build_events(data, master, bad)
    with pytest.raises(sprint.SprintDataError, match="bypass frozen sequential order"):
        sprint.run_program(data, master, {"candidates": [], "common_execution_and_accounting": {"frozen_schedules": []}}, candidate="UNKNOWN")


def test_pinned_identity_validation_fails_closed_for_missing_file(monkeypatch):
    monkeypatch.setattr(sprint, "_pinned_paths", lambda preregistration: {sprint.ROOT / "missing-pinned-evidence.json": "0" * 64})
    with pytest.raises(sprint.SprintDataError, match="pinned identity differs"):
        sprint.verify_pinned_files({})


def test_price_identity_is_mechanically_bound_to_loader_constants():
    preregistration = sprint.load_preregistration()
    preregistration["data_contract"]["price_identity"]["snapshot_id"] = "wrong"
    with pytest.raises(sprint.SprintDataError, match="snapshot identity differs"):
        sprint._pinned_paths(preregistration)


def test_program_enforces_first_pass_order_and_marks_order_two_late(monkeypatch):
    candidates = [
        {"candidate_id": "C1", "order": 1},
        {"candidate_id": "C2", "order": 2},
    ]
    preregistration = {
        "candidates": candidates,
        "common_execution_and_accounting": {"frozen_schedules": []},
        "data_contract": {
            "pit_lifecycle_identity": {},
            "cohort_id": "COHORT",
            "support_start_inclusive_utc": "START",
            "support_end_exclusive_utc": "END",
        },
    }
    seen = []

    def candidate_result(dataset, master, configs, candidate_id):
        seen.append(candidate_id)
        return {
            "candidate_id": candidate_id,
            "tier1_status": (
                "TIER1_FAIL" if candidate_id == "C1" else "MECHANISM_WORTH_CONFIRMING"
            ),
        }

    monkeypatch.setattr(sprint, "_configurations", lambda payload: {})
    monkeypatch.setattr(sprint, "_candidate_result", candidate_result)
    output = sprint.run_program(_dataset(("A",)), {}, preregistration)
    assert seen == ["C1", "C2"]
    assert output["first_pass_candidate"] == "C2"
    assert output["program_accounting"] == {
        "candidates_preregistered": 2,
        "candidates_tested": 2,
        "pass_count": 1,
        "fail_count": 1,
        "data_windows_viewed": 8,
        "variants_viewed": 4,
        "configurations_viewed": 6,
        "late_program_pass": True,
    }
