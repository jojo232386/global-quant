from __future__ import annotations

import hashlib
import json
import subprocess
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
    MarketCloseFilters,
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
        runtime_binding_passed=True,
        credential_cleanup_passed=True,
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
    binding = _real_binding_values()
    code, evidence_path = run_mutation_lifecycle(
        transport,
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=binding["runtime_commit"],
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
    )

    assert code == 0
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "PASS"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["protocol_tag"] == PROTOCOL_TAG
    assert payload["protocol_commit"] == binding["protocol_commit"]
    assert payload["protocol_sha256"] == binding["protocol_sha256"]
    assert payload["runtime_commit"] == binding["runtime_commit"]
    assert payload["lifecycle"]["create_requests"] == 1
    assert payload["lifecycle"]["cancel_requests"] == 1
    assert payload["lifecycle"]["executed_quantity"] == "0"
    assert payload["lifecycle"]["production_contacted"] is False


# ---------------------------------------------------------------------------
# Targeted security regression tests for the v1.6 review findings.
# ---------------------------------------------------------------------------


def _market_close_filters() -> MarketCloseFilters:
    return MarketCloseFilters(
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        market_lot_size_filter_count=1,
        min_notional_filter_count=1,
        uninterpreted_applicable_filter_types=(),
    )


def _clean_final_state() -> dict[str, object]:
    return {
        "nonzero_positions": (),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }


def _dirty_final_state() -> dict[str, object]:
    return {
        "nonzero_positions": ((SYMBOL, Decimal("0.002")),),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }


def _reconcile_owned(residual: Decimal = Decimal("0.002")) -> dict[str, object]:
    return {
        "residual_quantity": residual,
        "position_direction": "LONG",
        "open_remainder_quantity": Decimal("0"),
        "other_activity_absent": True,
    }


def _containment_transport(
    *,
    terminal_status: str = "FILLED",
    terminal_executed_quantity: Decimal = Decimal("0.002"),
    reconcile: dict[str, object] | None = None,
    containment_final: dict[str, object] | None = None,
    second_terminal_status: str = "CANCELED",
    second_terminal_executed_quantity: Decimal = Decimal("0.002"),
) -> FakeLifecycleTransport:
    transport = _happy_transport()
    transport.terminal_status = terminal_status
    transport.terminal_executed_quantity = terminal_executed_quantity
    transport.market_close_filters = _market_close_filters()
    transport.reconcile_state = (
        reconcile
        if reconcile is not None
        else _reconcile_owned(residual=terminal_executed_quantity)
    )
    transport.emergency_close_ack = {
        "orderId": "2",
        "status": "NEW",
        "clientOrderId": "g1b16c-aaaaaaaa-0123456789abcdef-1",
    }
    transport.emergency_query_status = "FILLED"
    transport.emergency_query_executed_quantity = terminal_executed_quantity
    transport.containment_final_state = (
        containment_final if containment_final is not None else _clean_final_state()
    )
    transport.second_terminal_status = second_terminal_status
    transport.second_terminal_executed_quantity = second_terminal_executed_quantity
    return transport


def _real_binding_values() -> dict[str, str]:
    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    protocol_bytes = (PROJECT_ROOT / "protocols/NT_GATE_1B_V1_6.md").read_bytes()
    return {
        "runtime_commit": _git("rev-parse", "HEAD^{commit}"),
        "protocol_commit": _git("rev-parse", f"refs/tags/{PROTOCOL_TAG}^{{commit}}"),
        "protocol_tag_object": _git("rev-parse", f"refs/tags/{PROTOCOL_TAG}"),
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
    }


# --- P0-1: unexpected fill at the terminal query may never PASS --------------


def test_p0_1_terminal_filled_cannot_pass() -> None:
    transport = _happy_transport()
    transport.terminal_status = "FILLED"
    transport.terminal_executed_quantity = Decimal("0.002")

    # Without provable ownership the runner must fail closed rather than PASS.
    with pytest.raises(MutationRunnerError, match="BLOCKED_CLEANUP_UNPROVEN"):
        _runner(transport).execute_lifecycle()


def test_p0_1_terminal_partially_filled_cannot_pass() -> None:
    transport = _containment_transport(
        terminal_status="PARTIALLY_FILLED",
        terminal_executed_quantity=Decimal("0.002"),
        second_terminal_status="CANCELED",
        second_terminal_executed_quantity=Decimal("0.002"),
    )
    evidence = _runner(transport).execute_lifecycle()

    # Even after a successful second cancel + bounded containment, the run is
    # not a clean happy-path PASS.
    assert evidence.emergency_close_requests == 1
    assert evidence.executed_quantity == Decimal("0.002")
    assert evidence.unexpected_mutations == 1
    with pytest.raises(MutationProtocolError):
        validate_lifecycle_pass(evidence)


def test_p0_1_executed_qty_after_cancel_cannot_pass() -> None:
    # Cancel returned CANCELED but the terminal query reveals a partial fill.
    transport = _containment_transport(
        terminal_status="CANCELED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    evidence = _runner(transport).execute_lifecycle()

    assert evidence.emergency_close_requests == 1
    assert evidence.executed_quantity == Decimal("0.002")
    assert evidence.unexpected_mutations == 1
    with pytest.raises(MutationProtocolError):
        validate_lifecycle_pass(evidence)


def test_p0_1_cancel_response_cannot_erase_fill_fact() -> None:
    # The terminal executed quantity drives the verdict, not the cancel status.
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    transport.cancel_status = "CANCELED"
    evidence = _runner(transport).execute_lifecycle()

    assert evidence.executed_quantity == Decimal("0.002")
    assert evidence.emergency_close_requests == 1
    assert evidence.unexpected_mutations == 1


def test_p0_1_unexpected_terminal_state_without_fill_stops() -> None:
    transport = _happy_transport()
    transport.terminal_status = "EXPIRED"
    transport.terminal_executed_quantity = Decimal("0")

    with pytest.raises(MutationRunnerError, match="STOP_UNEXPECTED_TERMINAL_STATE"):
        _runner(transport).execute_lifecycle()


# --- P0-2: missing final-state evidence may never default to clean -----------


def test_p0_2_empty_final_state_cannot_pass() -> None:
    transport = _happy_transport()
    transport.final_state = {}

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_INCOMPLETE"):
        _runner(transport).execute_lifecycle()


def test_p0_2_missing_positions_key_cannot_pass() -> None:
    transport = _happy_transport()
    transport.final_state = {
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_INCOMPLETE"):
        _runner(transport).execute_lifecycle()


def test_p0_2_missing_account_config_key_cannot_pass() -> None:
    transport = _happy_transport()
    transport.final_state = {
        "nonzero_positions": (),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
    }

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_INCOMPLETE"):
        _runner(transport).execute_lifecycle()


def test_p0_2_malformed_final_state_cannot_pass() -> None:
    transport = _happy_transport()
    transport.final_state = {
        "nonzero_positions": "not-a-tuple",
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_MALFORMED"):
        _runner(transport).execute_lifecycle()


# --- P1-1: containment / cleanup wiring -------------------------------------


def test_p1_1_exactly_owned_fill_triggers_bounded_containment() -> None:
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    evidence = _runner(transport).execute_lifecycle()

    # The single contingency reduce-only close is counted against the budget and
    # recorded; the run is STOP_UNEXPECTED_FILL_CONTAINED, never PASS.
    assert evidence.create_requests == 1
    assert evidence.cancel_requests == 1
    assert evidence.emergency_close_requests == 1
    assert evidence.executed_quantity == Decimal("0.002")
    assert evidence.unexpected_mutations == 1
    assert evidence.cleanup_confirmed is True
    assert "FILLED" in evidence.observed_statuses
    with pytest.raises(MutationProtocolError):
        validate_lifecycle_pass(evidence)


def test_p1_1_ownership_unprovable_other_activity_blocks() -> None:
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
        reconcile={
            "residual_quantity": Decimal("0.002"),
            "position_direction": "LONG",
            "open_remainder_quantity": Decimal("0"),
            "other_activity_absent": False,
        },
    )

    with pytest.raises(MutationRunnerError, match="BLOCKED_CLEANUP_UNPROVEN"):
        _runner(transport).execute_lifecycle()


def test_p1_1_mixed_pre_existing_position_blocks_auto_close() -> None:
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
        reconcile={
            # Residual does not equal the proven owned fill -> not attributable
            # solely to the probe.
            "residual_quantity": Decimal("0.005"),
            "position_direction": "LONG",
            "open_remainder_quantity": Decimal("0"),
            "other_activity_absent": True,
        },
    )

    with pytest.raises(MutationRunnerError, match="BLOCKED_CLEANUP_UNPROVEN"):
        _runner(transport).execute_lifecycle()


def test_p1_1_open_remainder_present_blocks() -> None:
    transport = _containment_transport(
        terminal_status="PARTIALLY_FILLED",
        terminal_executed_quantity=Decimal("0.002"),
        second_terminal_status="PARTIALLY_FILLED",
        second_terminal_executed_quantity=Decimal("0.002"),
        reconcile={
            "residual_quantity": Decimal("0.002"),
            "position_direction": "LONG",
            "open_remainder_quantity": Decimal("0.001"),
            "other_activity_absent": True,
        },
    )

    with pytest.raises(MutationRunnerError, match="BLOCKED_CLEANUP_UNPROVEN"):
        _runner(transport).execute_lifecycle()


def test_p1_1_final_still_nonzero_after_containment_blocks() -> None:
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
        containment_final=_dirty_final_state(),
    )

    with pytest.raises(MutationRunnerError, match="BLOCKED_FINAL_NOT_CLEAN_AFTER_CONTAINMENT"):
        _runner(transport).execute_lifecycle()


def test_p1_1_containment_recorded_in_run_mutation_lifecycle(tmp_path: Path) -> None:
    binding = _real_binding_values()
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    code, evidence_path = run_mutation_lifecycle(
        transport,
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=binding["runtime_commit"],
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
    )

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "STOP_UNEXPECTED_FILL_CONTAINED" in payload["reason_codes"]
    assert payload["lifecycle"]["emergency_close_requests"] == 1
    assert payload["lifecycle"]["executed_quantity"] == "0.002"
    assert payload["containment"]["containment_occurred"] is True
    assert payload["containment"]["emergency_close_attempts"] == 1


# --- P1-2: runtime / evidence binding mechanical verification ---------------


def test_p1_2_wrong_runtime_commit_stops(tmp_path: Path) -> None:
    binding = _real_binding_values()
    code, evidence_path = run_mutation_lifecycle(
        _happy_transport(),
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit="b" * 40,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
    )

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "RUNTIME_COMMIT_MISMATCH" in payload["reason_codes"]


def test_p1_2_wrong_protocol_sha256_stops(tmp_path: Path) -> None:
    binding = _real_binding_values()
    code, evidence_path = run_mutation_lifecycle(
        _happy_transport(),
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=binding["runtime_commit"],
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256="0" * 64,
    )

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "PROTOCOL_SHA256_MISMATCH" in payload["reason_codes"]


def test_p1_2_wrong_protocol_commit_stops(tmp_path: Path) -> None:
    binding = _real_binding_values()
    code, evidence_path = run_mutation_lifecycle(
        _happy_transport(),
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=binding["runtime_commit"],
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit="c" * 40,
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
    )

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "PROTOCOL_COMMIT_MISMATCH" in payload["reason_codes"]


def test_p1_2_untracked_source_file_stops(tmp_path: Path) -> None:
    binding = _real_binding_values()
    untracked = PROJECT_ROOT / "src" / "global_quant" / "gate1b" / "_rv_probe_untracked.py"
    untracked.write_text("# rv probe\n")
    try:
        code, evidence_path = run_mutation_lifecycle(
            _happy_transport(),
            project_root=PROJECT_ROOT,
            evidence_dir=tmp_path,
            environ={},
            runtime_commit=binding["runtime_commit"],
            session_nonce=_SESSION_NONCE,
            authorization_id=_AUTHORIZATION_ID,
            protocol_commit=binding["protocol_commit"],
            protocol_tag_object=binding["protocol_tag_object"],
            protocol_sha256=binding["protocol_sha256"],
        )
    finally:
        untracked.unlink(missing_ok=True)

    assert code == 1
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "STOP"
    assert "RUNTIME_UNTRACKED_FILES_PRESENT" in payload["reason_codes"]


# --- P2: evidence booleans are derived, not hardcoded ------------------------


def test_p2_happy_path_booleans_derived_true() -> None:
    evidence = _runner(_happy_transport()).execute_lifecycle()

    assert evidence.preflight_passed is True
    assert evidence.runtime_binding_passed is True
    assert evidence.credential_cleanup_passed is True
    assert evidence.filters_passed is True
    assert evidence.order_parameters_match is True
    assert evidence.cleanup_confirmed is True
    validate_lifecycle_pass(evidence)


def test_p2_runtime_binding_false_fails_closed() -> None:
    transport = _happy_transport()
    runner = MutationRunner(
        transport,
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
        runtime_binding_passed=False,
        credential_cleanup_passed=True,
    )
    evidence = runner.execute_lifecycle()

    assert evidence.runtime_binding_passed is False
    with pytest.raises(MutationProtocolError, match="RUNTIME_BINDING_FAILED"):
        validate_lifecycle_pass(evidence)


def test_p2_dirty_final_state_clears_cleanup_confirmed() -> None:
    transport = _happy_transport()
    transport.final_state = {
        "nonzero_positions": ((SYMBOL, Decimal("0.001")),),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }
    evidence = _runner(transport).execute_lifecycle()

    assert evidence.cleanup_confirmed is False
    assert evidence.final_account_config_matches is True
    with pytest.raises(MutationProtocolError, match="CLEANUP_NOT_CONFIRMED"):
        validate_lifecycle_pass(evidence)


# ---------------------------------------------------------------------------
# Second independent review P1/P3 closure: malformed final-state counts and
# malformed terminal executed quantities must fail closed (no PASS, no crash).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [0.5, "0", False, None, -1],
)
def test_p1_final_state_open_regular_orders_malformed_cannot_pass(bad_value: object) -> None:
    transport = _happy_transport()
    transport.final_state = {
        **_final_state(),
        "open_regular_orders": bad_value,
    }

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_MALFORMED"):
        _runner(transport).execute_lifecycle()


@pytest.mark.parametrize(
    "bad_value",
    [0.5, "0", False, None, -1],
)
def test_p1_final_state_open_algo_orders_malformed_cannot_pass(bad_value: object) -> None:
    transport = _happy_transport()
    transport.final_state = {
        **_final_state(),
        "open_algo_orders": bad_value,
    }

    with pytest.raises(MutationRunnerError, match="FINAL_STATE_EVIDENCE_MALFORMED"):
        _runner(transport).execute_lifecycle()


def test_p1_final_state_exact_integer_zero_still_passes() -> None:
    # Exact non-negative ints remain the only accepted counts.
    evidence = _runner(_happy_transport()).execute_lifecycle()
    assert evidence.final_open_regular_orders == 0
    assert evidence.final_open_algo_orders == 0
    validate_lifecycle_pass(evidence)


@pytest.mark.parametrize(
    "bad_value",
    [
        Decimal("-1"),
        True,
        False,
        None,
        Decimal("NaN"),
        Decimal("Infinity"),
        "0",
        0.0,
        1,
    ],
)
def test_p1_terminal_executed_quantity_malformed_cannot_pass(bad_value: object) -> None:
    transport = _happy_transport()
    transport.terminal_status = "CANCELED"
    transport.terminal_executed_quantity = bad_value

    with pytest.raises(MutationRunnerError, match="STOP_MALFORMED_TERMINAL_EXECUTED_QUANTITY"):
        _runner(transport).execute_lifecycle()


def test_p1_terminal_executed_quantity_malformed_on_second_terminal_stops() -> None:
    """The partial-fill remainder terminal query is validated the same way."""
    transport = _happy_transport()
    transport.terminal_status = "PARTIALLY_FILLED"
    transport.terminal_executed_quantity = Decimal("0.002")
    transport.second_terminal_status = "CANCELED"
    transport.second_terminal_executed_quantity = Decimal("NaN")

    with pytest.raises(MutationRunnerError, match="STOP_MALFORMED_TERMINAL_EXECUTED_QUANTITY"):
        _runner(transport).execute_lifecycle()


def test_p1_terminal_executed_zero_still_passes_clean_path() -> None:
    transport = _happy_transport()
    transport.terminal_status = "CANCELED"
    transport.terminal_executed_quantity = Decimal("0")

    evidence = _runner(transport).execute_lifecycle()
    assert evidence.executed_quantity == Decimal("0")
    validate_lifecycle_pass(evidence)


def test_p1_terminal_executed_positive_still_contains() -> None:
    """A genuine positive fill must still route through containment, not PASS."""
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    evidence = _runner(transport).execute_lifecycle()

    assert evidence.emergency_close_requests == 1
    assert evidence.executed_quantity == Decimal("0.002")
    with pytest.raises(MutationProtocolError):
        validate_lifecycle_pass(evidence)


def test_p3_emergency_query_malformed_quantity_fails_closed() -> None:
    """Malformed contingency-close quantity is a clean STOP, never a crash."""
    transport = _containment_transport(
        terminal_status="FILLED",
        terminal_executed_quantity=Decimal("0.002"),
    )
    transport.emergency_query_executed_quantity = Decimal("NaN")

    with pytest.raises(MutationRunnerError, match="STOP_MALFORMED_EMERGENCY_EXECUTED_QUANTITY"):
        _runner(transport).execute_lifecycle()


def test_p3_credential_cleanup_false_fails_closed() -> None:
    transport = _happy_transport()
    runner = MutationRunner(
        transport,
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
        runtime_binding_passed=True,
        credential_cleanup_passed=False,
    )
    evidence = runner.execute_lifecycle()

    assert evidence.credential_cleanup_passed is False
    with pytest.raises(MutationProtocolError, match="CREDENTIAL_CLEANUP_FAILED"):
        validate_lifecycle_pass(evidence)


def test_p3_credential_cleanup_derived_from_env_validation(tmp_path: Path) -> None:
    """The clean-path credential_cleanup_passed is derived from the executed
    credential-environment validation (env must be empty to proceed)."""
    binding = _real_binding_values()
    code, evidence_path = run_mutation_lifecycle(
        _happy_transport(),
        project_root=PROJECT_ROOT,
        evidence_dir=tmp_path,
        environ={},
        runtime_commit=binding["runtime_commit"],
        session_nonce=_SESSION_NONCE,
        authorization_id=_AUTHORIZATION_ID,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
    )

    assert code == 0
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "PASS"
    assert payload["credential_environment_empty"] is True
