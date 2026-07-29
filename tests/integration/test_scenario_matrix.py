from __future__ import annotations

from global_quant.gate1a.scenarios import REQUIRED_SCENARIOS
from global_quant.gate1a.scenarios import run_all_scenarios


def test_all_frozen_scenarios_pass_with_complete_evidence(tmp_path) -> None:
    results = run_all_scenarios(tmp_path)

    assert [result.name for result in results] == list(REQUIRED_SCENARIOS)
    assert len(results) == 12
    assert all(result.status == "PASS" for result in results)
    assert all(result.exit_code == 0 for result in results)
    for result in results:
        assert result.initial_state
        assert result.input_events
        assert result.expected_orders is not None
        assert result.expected_fills is not None
        assert result.final_positions is not None
        assert result.final_wallet
        assert result.protection_state is not None
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

