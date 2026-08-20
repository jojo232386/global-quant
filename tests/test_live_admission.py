from copy import deepcopy
from datetime import datetime, timezone
import json
import pathlib
import subprocess

import pytest

from gmaq_live import committed_candidate_sha
from gmaq_live.admission import evaluate_live_candidate, reconcile_binance_usdm_truth, validate_live_config


CANDIDATE = "a" * 40
CONFIG_SHA = "b" * 64
NOW = 1_800_000_000
CAPTURED = datetime.fromtimestamp(NOW - 30, timezone.utc).isoformat()


def truth_inputs() -> dict:
    client_id = "gmaq-live-eth-00000001"
    event_ms = (NOW - 31) * 1000
    return {
        "intent": {
            "exchange": "binance_usdm",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "quantity": "0.01",
            "position_side": "BOTH",
            "reduce_only": False,
            "client_order_id": client_id,
            "adapter_id": "gmaq-binance-usdm-v1",
            "pre_submit_recorded": True,
            "intent_audit_sha256": "c" * 64,
        },
        "rest_orders": [
            {
                "symbol": "ETHUSDT",
                "clientOrderId": client_id,
                "orderId": 12345,
                "status": "FILLED",
                "origQty": "0.010",
                "executedQty": "0.010",
                "side": "BUY",
                "positionSide": "BOTH",
                "reduceOnly": False,
            }
        ],
        "user_stream_events": [
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": event_ms - 1000,
                "o": {
                    "s": "ETHUSDT",
                    "c": client_id,
                    "S": "BUY",
                    "ps": "BOTH",
                    "R": False,
                    "X": "NEW",
                    "i": 12345,
                    "q": "0.010",
                    "z": "0",
                },
            },
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": event_ms - 500,
                "o": {
                    "s": "ETHUSDT",
                    "c": client_id,
                    "S": "BUY",
                    "ps": "BOTH",
                    "R": False,
                    "X": "FILLED",
                    "i": 12345,
                    "q": "0.010",
                    "z": "0.010",
                },
            },
            {
                "e": "ACCOUNT_UPDATE",
                "E": event_ms,
                "a": {"P": [{"s": "ETHUSDT", "pa": "0.010", "ps": "BOTH"}]},
            },
        ],
        "rest_positions": [{"symbol": "ETHUSDT", "positionAmt": "0.010", "positionSide": "BOTH"}],
        "stream_state": {
            "connected": True,
            "gap_detected": False,
            "session_id": "stream-0001",
            "last_event_epoch_ms": event_ms,
        },
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "captured_at_utc": CAPTURED,
    }


def matched_broker_evidence() -> dict:
    return reconcile_binance_usdm_truth(**truth_inputs())


def account_evidence() -> dict:
    return {
        "schema_version": 2,
        "scope": "authorized read-only session; GET requests only",
        "fetched_at_utc": CAPTURED,
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "verdict": "PASS_READONLY",
        "facts": {
            "accountType": "usd_m_futures",
            "canTrade": True,
            "canWithdraw": False,
            "dualSidePosition": False,
            "multiAssetsMarginMode": False,
            "apiRestrictions": {
                "enableFutures": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
            },
            "commission": {"taker": "0.0005", "status": "VERIFIED_ON_ACCOUNT"},
            "leverageBracketTier1": {"maintMarginRatio": "0.004"},
        },
    }


def readiness() -> dict:
    return {
        "schema_version": 1,
        "captured_at_utc": CAPTURED,
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "dedicated_account": True,
        "sole_operator": True,
        "risk_limits_approved": True,
        "alert_route_verified": True,
        "secret_storage_approved": True,
        "strategy_verdict": "PASS",
        "soak_verdict": "PASS",
        "strategy_artifact_sha256": "d" * 64,
        "soak_evidence_sha256": "e" * 64,
        "alert_evidence_sha256": "f" * 64,
        "risk_approval_id": "risk-approval-0001",
        "operator_attestation_id": "operator-attestation-0001",
        "secret_storage_approval_id": "secret-storage-0001",
    }


def evaluate(**overrides) -> dict:
    inputs = {
        "account_evidence": account_evidence(),
        "broker_evidence": matched_broker_evidence(),
        "readiness": readiness(),
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "now_epoch": NOW,
    }
    inputs.update(overrides)
    return evaluate_live_candidate(**inputs)


def test_matching_rest_stream_and_position_truth() -> None:
    result = matched_broker_evidence()
    assert result["verdict"] == "MATCH"
    assert result["errors"] == []
    assert result["client_order_id"] == "gmaq-live-eth-00000001"
    assert result["exchange_order_id"] == "12345"
    assert len(result["capture_sha256"]) == 64
    assert result["does_not_authorize_live_trading"] is True


def test_unrelated_later_account_update_does_not_hide_relevant_position() -> None:
    inputs = truth_inputs()
    later = (NOW - 30) * 1000
    inputs["user_stream_events"].append(
        {
            "e": "ACCOUNT_UPDATE",
            "E": later,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ps": "BOTH"}]},
        }
    )
    inputs["stream_state"]["last_event_epoch_ms"] = later
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "MATCH"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["stream_state"].update(connected=False), "user_stream_not_connected"),
        (lambda value: value["stream_state"].update(gap_detected=True), "user_stream_gap_or_unknown"),
        (lambda value: value["stream_state"].update(last_event_epoch_ms=0), "user_stream_freshness_invalid"),
        (lambda value: value["rest_orders"].append(deepcopy(value["rest_orders"][0])), "rest_order_identity_not_unique"),
        (lambda value: value["rest_orders"][0].update(status="PARTIALLY_FILLED"), "order_status_mismatch"),
        (lambda value: value["rest_orders"][0].update(executedQty="0.005"), "order_filled_mismatch"),
        (lambda value: value["intent"].update(symbol="BTCUSDT"), "intent_symbol_mismatch"),
        (lambda value: value["intent"].update(side="SELL"), "intent_side_mismatch"),
        (lambda value: value["intent"].update(quantity="0.02"), "intent_quantity_mismatch"),
        (lambda value: value["intent"].update(reduce_only=True), "intent_reduce_only_mismatch"),
        (lambda value: value["rest_positions"][0].update(positionAmt="0"), "position_amount_mismatch"),
        (lambda value: value["user_stream_events"].pop(), "stream_position_identity_not_unique"),
        (lambda value: value["intent"].update(adapter_id=""), "exchange_bound_adapter_missing"),
    ],
)
def test_truth_mismatch_is_quarantined(mutation, expected) -> None:
    inputs = truth_inputs()
    mutation(inputs)
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert expected in result["errors"]


def test_malformed_capture_is_quarantined_without_exception() -> None:
    inputs = truth_inputs()
    inputs["user_stream_events"] = ["not-an-object"]
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert result["errors"] == ["user_stream_events_invalid"]


def test_nonfinite_numbers_and_naive_capture_time_fail_closed() -> None:
    inputs = truth_inputs()
    inputs["user_stream_events"][1]["o"]["z"] = "NaN"
    inputs["captured_at_utc"] = "2027-01-15T08:00:00"
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_filled_progression_invalid" in result["errors"]
    assert "user_stream_freshness_invalid" in result["errors"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda event: event.update(s="BTCUSDT"), "order_event_symbol_mismatch"),
        (lambda event: event.update(X="UNKNOWN"), "order_event_status_unknown"),
        (lambda event: event.update(z="0.02"), "order_event_filled_progression_invalid"),
        (lambda event: event.update(X="FILLED", z="0"), "order_event_status_fill_inconsistent"),
    ],
)
def test_earlier_contradictory_order_event_is_quarantined(mutation, expected) -> None:
    inputs = truth_inputs()
    mutation(inputs["user_stream_events"][0]["o"])
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert expected in result["errors"]


@pytest.mark.parametrize("terminal_status", ["CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"])
def test_nonfilled_terminal_state_cannot_claim_full_fill(terminal_status) -> None:
    inputs = truth_inputs()
    inputs["rest_orders"][0]["status"] = terminal_status
    inputs["user_stream_events"][1]["o"]["X"] = terminal_status
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_status_fill_inconsistent" in result["errors"]


def test_zero_quantity_order_is_quarantined() -> None:
    inputs = truth_inputs()
    inputs["intent"]["quantity"] = "0"
    inputs["rest_orders"][0].update(origQty="0", executedQty="0")
    for event in inputs["user_stream_events"][:2]:
        event["o"].update(q="0", z="0")
    inputs["rest_positions"][0]["positionAmt"] = "0"
    inputs["user_stream_events"][2]["a"]["P"][0]["pa"] = "0"
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_filled_progression_invalid" in result["errors"]


def test_complete_fixture_contract_remains_structurally_blocked() -> None:
    result = evaluate()
    assert result["verdict"] == "BLOCKED"
    assert result["blockers"] == [
        "implementation_authenticated_capture_adapter_missing",
        "implementation_exchange_bound_submission_adapter_missing",
        "implementation_live_credential_account_binding_missing",
    ]
    assert result["entry_authorized"] is False
    assert result["order_submission_enabled"] is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"now_epoch": NOW + 3600}, "account_evidence_stale_or_invalid"),
        ({"candidate_sha": "c" * 40}, "account_candidate_sha_mismatch"),
    ],
)
def test_missing_or_stale_evidence_blocks(overrides, expected) -> None:
    result = evaluate(**overrides)
    assert result["verdict"] == "BLOCKED"
    assert expected in result["blockers"]
    assert result["entry_authorized"] is False


def test_failed_account_and_operational_predicates_block() -> None:
    account = account_evidence()
    account["facts"]["dualSidePosition"] = True
    account["facts"]["canWithdraw"] = True
    account["facts"]["apiRestrictions"]["enableWithdrawals"] = True
    operations = readiness()
    operations["strategy_verdict"] = "REJECT"
    operations["soak_verdict"] = "RUNNING"
    result = evaluate(account_evidence=account, readiness=operations)
    assert result["verdict"] == "BLOCKED"
    assert "account_dualSidePosition_invalid" in result["blockers"]
    assert "account_canWithdraw_invalid" in result["blockers"]
    assert "account_enableWithdrawals_invalid" in result["blockers"]
    assert "readiness_strategy_verdict_invalid" in result["blockers"]
    assert "readiness_soak_verdict_invalid" in result["blockers"]


def test_cli_is_non_ordering_and_does_not_print_credentials() -> None:
    script = (pathlib.Path(__file__).parents[1] / "scripts" / "gmaq-live-admission").read_text()
    assert "urllib" not in script
    assert "requests" not in script
    assert "create_order" not in script
    assert "GMAQ_LIVE_KEY" not in script
    assert "GMAQ_LIVE_SECRET" not in script
    assert '"entry_authorized": True' not in script
    assert '"order_submission_enabled": True' not in script
    assert "--config-sha256" not in script
    assert "validated_config_sha256(args.config)" in script
    assert "committed_candidate_sha(ROOT)" in script
    assert "--max-age-seconds" not in script


def test_dry_run_authorization_cannot_satisfy_live_candidate() -> None:
    operations = readiness()
    operations["authorization_scope"] = "DEMO_DRY_RUN_ENTRY"
    operations["strategy_verdict"] = "NOT_PROVEN_ALPHA"
    result = evaluate(readiness=operations)
    assert result["verdict"] == "BLOCKED"
    assert "readiness_strategy_verdict_invalid" in result["blockers"]


def test_proposed_live_config_is_non_secret_and_minimal() -> None:
    config = {
        "dry_run": False,
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "stake_currency": "USDT",
        "max_open_trades": 1,
        "exchange": {
            "name": "binance",
            "pair_whitelist": ["ETH/USDT:USDT"],
        },
    }
    assert validate_live_config(config) == []
    config["exchange"]["secret"] = "must-not-be-in-config"
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    assert "config_contains_secret_material" in validate_live_config(config)
    config["exchange"].pop("secret")
    config["dry_run"] = True
    assert "config_dry_run_invalid" in validate_live_config(config)
    config["dry_run"] = False
    config["exchange"]["password"] = "must-not-be-stored"
    assert "config_contains_secret_material" in validate_live_config(config)
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    config["exchange"].pop("password")
    config["opaque"] = "secret-under-an-arbitrary-name"
    assert "config_unknown_fields" in validate_live_config(config)
    config.pop("opaque")
    config["exchange"]["key"] = False
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    config["exchange"].pop("key")
    config["max_open_trades"] = True
    assert "config_max_open_trades_invalid" in validate_live_config(config)


def test_numeric_permission_values_cannot_impersonate_booleans() -> None:
    account = account_evidence()
    account["facts"]["canWithdraw"] = 0
    account["facts"]["apiRestrictions"]["enableFutures"] = 1
    operations = readiness()
    operations["dedicated_account"] = 1
    result = evaluate(account_evidence=account, readiness=operations)
    assert "account_canWithdraw_invalid" in result["blockers"]
    assert "account_enableFutures_invalid" in result["blockers"]
    assert "readiness_dedicated_account_invalid" in result["blockers"]


def test_oversized_evidence_age_cannot_relax_freshness() -> None:
    result = evaluate(now_epoch=NOW + 3600, max_evidence_age_seconds=86_400)
    assert "evidence_age_limit_invalid" in result["blockers"]
    assert "account_evidence_stale_or_invalid" in result["blockers"]


def test_candidate_sha_rejects_hidden_index_flags(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GMAQ Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("committed\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-index", "--assume-unchanged", "tracked.txt"], cwd=tmp_path, check=True)
    tracked.write_text("hidden change\n")
    with pytest.raises(RuntimeError, match="hidden-index flags"):
        committed_candidate_sha(tmp_path)
