from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b.mutation_protocol import (
    NORMAL_TOTAL_HTTP_REQUESTS,
    PROTOCOL_VERSION,
    SYMBOL,
    AccountState,
    LimitOrderFilters,
    MutationProtocolError,
    SymbolState,
    validate_lifecycle_pass,
)
from global_quant.gate1b.mutation_runner import (
    PROJECT_ROOT,
    PROTOCOL_TAG,
    FakeLifecycleTransport,
    MutationRunner,
    MutationRunnerError,
    run_mutation_lifecycle,
)

_RUNTIME_COMMIT = "a" * 40
_SESSION_NONCE = "0123456789abcdef"
_AUTHORIZATION_ID = "g1b16-0123456789abcdef"
_PROTOCOL_COMMIT = "d" * 40
_PROTOCOL_TAG_OBJECT = "e" * 40
_PROTOCOL_SHA256 = "f" * 64


def _filters() -> LimitOrderFilters:
    return LimitOrderFilters(
        min_price=Decimal("1000.00"),
        max_price=Decimal("5000.00"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        percent_price_multiplier_down=Decimal("0.85"),
        percent_price_multiplier_up=Decimal("1.05"),
    )


def _account_state(**overrides: object) -> AccountState:
    values: dict[str, object] = {
        "can_trade": True,
        "dual_side_position": False,
        "multi_assets_margin": False,
        "margin_type": "ISOLATED",
        "leverage": 1,
        "auto_add_margin": False,
        "server_time_skew_ms": Decimal("100"),
        "wallet_balance": Decimal("100"),
        "available_balance": Decimal("100"),
        "nonzero_positions": (),
        "open_regular_order_ids": (),
        "open_algo_order_ids": (),
    }
    values.update(overrides)
    return AccountState(**values)  # type: ignore[arg-type]


def _symbol_state(**overrides: object) -> SymbolState:
    values: dict[str, object] = {
        "symbol": SYMBOL,
        "status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "order_types": frozenset({"LIMIT", "MARKET"}),
        "time_in_force": frozenset({"GTX"}),
        "filter_type_counts": (
            ("PRICE_FILTER", 1),
            ("LOT_SIZE", 1),
            ("MARKET_LOT_SIZE", 1),
            ("MIN_NOTIONAL", 1),
            ("PERCENT_PRICE", 1),
        ),
        "uninterpreted_applicable_filter_types": (),
    }
    values.update(overrides)
    return SymbolState(**values)  # type: ignore[arg-type]


def _final_state() -> dict[str, object]:
    return {
        "nonzero_positions": (),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }


def _happy_transport() -> FakeLifecycleTransport:
    return FakeLifecycleTransport(
        account_state=_account_state(),
        symbol_state=_symbol_state(),
        filters=_filters(),
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        create_ack={
            "orderId": "1",
            "status": "NEW",
            "clientOrderId": "g1b16-xxxxxxxxxx-0123456789abcdef-01",
        },
        query_status="NEW",
        query_executed_quantity=Decimal("0"),
        query_accepted_elapsed_seconds=Decimal("1"),
        cancel_status="CANCELED",
        final_state=_final_state(),
        production_contacted=False,
    )


def _runner(transport: FakeLifecycleTransport) -> MutationRunner:
    return MutationRunner(
        transport,
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    )


def test_runner_happy_path_passes_lifecycle_validation() -> None:
    runner = _runner(_happy_transport())
    evidence = runner.execute_lifecycle()

    assert evidence.create_requests == 1
    assert evidence.cancel_requests == 1
    assert evidence.emergency_close_requests == 0
    assert evidence.modify_requests == 0
    assert evidence.accepted_orders == 1
    assert evidence.total_http_requests == NORMAL_TOTAL_HTTP_REQUESTS
    assert evidence.executed_quantity == Decimal("0")
    assert evidence.unexpected_mutations == 0
    assert evidence.production_contacted is False
    assert evidence.final_open_regular_orders == 0
    assert evidence.final_open_algo_orders == 0
    # The happy path must clear the frozen offline arbiter.
    validate_lifecycle_pass(evidence)


def test_runner_production_contacted_cannot_pass() -> None:
    transport = _happy_transport()
    transport.production_contacted = True
    evidence = _runner(transport).execute_lifecycle()

    with pytest.raises(MutationProtocolError, match="PRODUCTION_CONTACTED"):
        validate_lifecycle_pass(evidence)


def test_runner_unexpected_fill_stops_before_cancel() -> None:
    transport = _happy_transport()
    transport.query_status = "PARTIALLY_FILLED"
    transport.query_executed_quantity = Decimal("0.001")

    with pytest.raises(MutationRunnerError, match="UNEXPECTED_ORDER_STATE_AT_QUERY"):
        _runner(transport).execute_lifecycle()


def test_runner_filled_order_stops_before_cancel() -> None:
    transport = _happy_transport()
    transport.query_status = "FILLED"
    transport.query_executed_quantity = Decimal("0.003")

    with pytest.raises(MutationRunnerError, match="UNEXPECTED_ORDER_STATE_AT_QUERY"):
        _runner(transport).execute_lifecycle()


def test_runner_account_margin_mismatch_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(margin_type="CROSS")

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_account_leverage_mismatch_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(leverage=2)

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_auto_add_margin_on_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(auto_add_margin=True)

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_hedge_mode_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(dual_side_position=True)

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_existing_position_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(nonzero_positions=((SYMBOL, Decimal("0.001")),))

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_existing_regular_order_stops_before_create() -> None:
    transport = _happy_transport()
    transport.account_state = _account_state(open_regular_order_ids=("999",))

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_minimum_quantity_above_cap_stops_before_create() -> None:
    # min_quantity 0.01 with price ~2475 gives notional 24.75 > 10 USDT cap.
    transport = _happy_transport()
    transport.filters = replace(_filters(), min_quantity=Decimal("0.010"))

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_symbol_not_trading_stops_before_create() -> None:
    transport = _happy_transport()
    transport.symbol_state = _symbol_state(status="HALT")

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_runner_gtx_not_supported_stops_before_create() -> None:
    transport = _happy_transport()
    transport.symbol_state = _symbol_state(time_in_force=frozenset({"GTC"}))

    with pytest.raises(MutationProtocolError):
        _runner(transport).execute_lifecycle()


def test_run_mutation_lifecycle_rejects_credential_environment(tmp_path: Path) -> None:
    transport = _happy_transport()
    code, evidence_path = run_mutation_lifecycle(
        transport,
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={"BINANCE_DEMO_API_KEY": "present-name-only"},
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    )

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY" in payload["reason_codes"]
    assert payload["credentials_read"] is False
    assert payload["network_accessed"] is False


def test_run_mutation_lifecycle_passes_protocol_tag_binding(tmp_path: Path) -> None:
    transport = _happy_transport()
    code, evidence_path = run_mutation_lifecycle(
        transport,
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    )

    assert code == 0
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "PASS"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["protocol_tag"] == PROTOCOL_TAG
    assert "protocol_commit" in payload
    assert "protocol_sha256" in payload
    assert payload["lifecycle"]["create_requests"] == 1
    assert payload["lifecycle"]["cancel_requests"] == 1
    assert payload["lifecycle"]["executed_quantity"] == "0"
    assert payload["lifecycle"]["production_contacted"] is False
