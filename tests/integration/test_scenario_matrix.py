from __future__ import annotations

import pytest

from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS
from global_quant.gate1a.scenarios import SCENARIO_RUNNERS
from global_quant.gate1a.scenarios import run_all_scenarios


def test_raw_scenario_observation_cannot_self_report_pass(tmp_path) -> None:
    result = SCENARIO_RUNNERS["new_order_rejected"](tmp_path)

    assert result.status == "UNVALIDATED"
    assert result.expected_orders == []
    assert result.expected_fills == []


def test_all_frozen_scenarios_pass_with_complete_evidence(tmp_path) -> None:
    results = run_all_scenarios(tmp_path)

    assert [result.name for result in results] == list(REQUIRED_SCENARIOS)
    assert len(results) == 12
    assert all(result.status == "PASS" for result in results)
    assert all(result.exit_code == 0 for result in results)
    for result in results:
        assert result.initial_state
        assert result.input_events
        assert result.observed_orders == result.expected_orders
        assert result.observed_fills == result.expected_fills
        assert result.final_positions == result.expected_final_positions
        assert result.final_wallet == result.expected_final_wallet
        assert result.protection_state == result.expected_protection_state
        assert result.exit_code == result.expected_exit_code
        assert result.business_hash == result.expected_business_hash
        assert result.oracle_version == "NT-GATE-1A-SCENARIO-ORACLE-1"
        assert result.validation_errors == []
        assert result.ledger_hash
        assert result.business_hash


def test_unknown_external_event_scenario_passes_only_by_failing_closed(tmp_path) -> None:
    result = {
        item.name: item
        for item in run_all_scenarios(tmp_path)
    }["unknown_external_event"]

    assert result.status == "PASS"
    assert result.fail_closed is True
    assert "ANOMALY" in result.observed_events


def test_snapshot_mismatch_scenario_detects_integrity_failure(tmp_path) -> None:
    result = {
        item.name: item
        for item in run_all_scenarios(tmp_path)
    }["snapshot_replay_mismatch"]

    assert result.status == "PASS"
    assert result.expected_failure == "CheckpointIntegrityError"


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("observed_orders", ["wrong-order"]),
        ("observed_fills", ["wrong-fill"]),
        ("final_positions", {}),
        ("final_wallet", "0"),
        ("protection_state", {}),
        ("exit_code", 99),
        ("business_hash", "0" * 64),
    ],
)
def test_each_mutated_actual_field_is_stopped_by_frozen_oracle(
    tmp_path,
    monkeypatch,
    field,
    mutation,
) -> None:
    scenario = "main_close_cancels_protection"
    original = SCENARIO_RUNNERS[scenario]

    def mutated_runner(root):
        result = original(root)
        setattr(result, field, mutation)
        return result

    monkeypatch.setitem(
        SCENARIO_RUNNERS,
        scenario,
        mutated_runner,
    )

    result = {
        item.name: item
        for item in run_all_scenarios(tmp_path)
    }[scenario]

    assert result.status == "STOP"
    assert f"{field} mismatch" in result.validation_errors
