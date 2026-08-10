from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

from global_quant.gate1b.mutation_protocol import (
    AMBIGUOUS_CANCEL_EXTRA_HTTP_REQUESTS,
    CREATE_DEADLINE_SECONDS,
    EMERGENCY_CONTAINMENT_EXTRA_HTTP_REQUESTS,
    MAX_ACCEPTED_TO_CANCEL_SECONDS,
    MAX_HARD_MUTATION_REQUESTS,
    MAX_HTTP_REQUESTS,
    MAX_NOTIONAL_USDT,
    MAX_POST_CREATE_READ_REQUESTS,
    NORMAL_MUTATION_REQUESTS,
    NORMAL_POST_CREATE_HTTP_REQUESTS,
    NORMAL_PRE_CREATE_HTTP_REQUESTS,
    NORMAL_TOTAL_HTTP_REQUESTS,
    POST_CREATE_HTTP_RESERVE,
    PRICE_DISCOUNT_BPS,
    PROTOCOL_STATUS,
    PROTOCOL_VERSION,
    REQUEST_TIMEOUT_SECONDS,
    SYMBOL,
    TOTAL_RUNTIME_SECONDS,
    AccountState,
    DuplicateDisposition,
    DuplicateLookup,
    DurableIntent,
    FrozenLimitOrder,
    LifecycleEvidence,
    LimitOrderFilters,
    LookupOutcome,
    MarketCloseFilters,
    MarketCloseProof,
    MutationLedger,
    MutationProtocolError,
    MutationRequestGuard,
    OrderDerivationProof,
    OwnedOrderProof,
    OwnedPositionProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    SymbolState,
    build_client_order_id,
    classify_duplicate,
    derive_limit_order,
    validate_account_state,
    validate_lifecycle_pass,
    validate_market_close_proof,
    validate_order_payload,
    validate_symbol_state,
)


def filters(**overrides: str) -> LimitOrderFilters:
    values = {
        "min_price": "1000.00",
        "max_price": "5000.00",
        "tick_size": "0.01",
        "min_quantity": "0.001",
        "max_quantity": "100.000",
        "step_size": "0.001",
        "min_notional": "5",
        "percent_price_multiplier_down": "0.85",
        "percent_price_multiplier_up": "1.05",
    }
    values.update(overrides)
    return LimitOrderFilters(**{key: Decimal(value) for key, value in values.items()})


def passing_lifecycle(**overrides: object) -> LifecycleEvidence:
    values: dict[str, object] = {
        "create_requests": 1,
        "cancel_requests": 1,
        "emergency_close_requests": 0,
        "modify_requests": 0,
        "account_setting_mutations": 0,
        "accepted_orders": 1,
        "observed_statuses": ("NEW", "CANCELED"),
        "executed_quantity": Decimal("0"),
        "fee_delta": Decimal("0"),
        "funding_delta": Decimal("0"),
        "wallet_balance_delta": Decimal("0"),
        "total_http_requests": NORMAL_TOTAL_HTTP_REQUESTS,
        "total_runtime_seconds": Decimal("30"),
        "create_elapsed_seconds": Decimal("20"),
        "accepted_to_cancel_seconds": Decimal("1"),
        "final_nonzero_positions": (),
        "final_open_regular_orders": 0,
        "final_open_algo_orders": 0,
        "unexpected_mutations": 0,
        "read_retries": 0,
        "production_contacted": False,
        "preflight_passed": True,
        "final_account_config_matches": True,
        "runtime_binding_passed": True,
        "credential_cleanup_passed": True,
        "filters_passed": True,
        "order_parameters_match": True,
        "cleanup_confirmed": True,
    }
    values.update(overrides)
    return LifecycleEvidence(**values)


def test_frozen_candidate_constants_are_minimal() -> None:
    assert PROTOCOL_VERSION == "1.6"
    assert PROTOCOL_STATUS == "CANDIDATE_NOT_FROZEN"
    assert SYMBOL == "ETHUSDT"
    assert Decimal("10") == MAX_NOTIONAL_USDT
    assert PRICE_DISCOUNT_BPS == 100
    assert NORMAL_MUTATION_REQUESTS == 2
    assert MAX_HARD_MUTATION_REQUESTS == 4
    assert MAX_HTTP_REQUESTS == 31
    assert REQUEST_TIMEOUT_SECONDS == 5
    assert TOTAL_RUNTIME_SECONDS == 180
    assert CREATE_DEADLINE_SECONDS == 60
    assert MAX_ACCEPTED_TO_CANCEL_SECONDS == 3
    assert POST_CREATE_HTTP_RESERVE == 18
    assert MAX_POST_CREATE_READ_REQUESTS == 15
    assert NORMAL_PRE_CREATE_HTTP_REQUESTS == 11
    assert NORMAL_POST_CREATE_HTTP_REQUESTS == 9
    assert NORMAL_TOTAL_HTTP_REQUESTS == 21
    assert AMBIGUOUS_CANCEL_EXTRA_HTTP_REQUESTS == 2
    assert EMERGENCY_CONTAINMENT_EXTRA_HTTP_REQUESTS == 6
    assert (
        NORMAL_POST_CREATE_HTTP_REQUESTS
        + AMBIGUOUS_CANCEL_EXTRA_HTTP_REQUESTS
        + EMERGENCY_CONTAINMENT_EXTRA_HTTP_REQUESTS
        + 1
        == POST_CREATE_HTTP_RESERVE
    )


def derivation_proof() -> OrderDerivationProof:
    active_filters = filters()
    return OrderDerivationProof(
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        filters=active_filters,
        filter_snapshot_sha256="b" * 64,
        filter_contract_sha256=active_filters.canonical_sha256,
        book_age_ms=Decimal("100"),
        mark_age_ms=Decimal("100"),
        observed_elapsed_seconds=Decimal("0.5"),
    )


def frozen_order():
    return derivation_proof().order


def durable_intent(*, persisted: bool = True) -> DurableIntent:
    return DurableIntent(
        authorization_id="g1b16-0123456789abcdef",
        protocol_commit="d" * 40,
        protocol_tag_object="e" * 40,
        protocol_sha256="f" * 64,
        runtime_commit="a" * 40,
        session_nonce="0123456789abcdef",
        order_derivation=derivation_proof(),
        persisted=persisted,
    )


def market_close_proof(*, quantity: str = "0.001", **filter_overrides: str) -> MarketCloseProof:
    filter_values = {
        "min_quantity": "0.001",
        "max_quantity": "100",
        "step_size": "0.001",
        "min_notional": "1",
    }
    filter_values.update(filter_overrides)
    active_filters = MarketCloseFilters(
        **{key: Decimal(value) for key, value in filter_values.items()},
        market_lot_size_filter_count=1,
        min_notional_filter_count=1,
        uninterpreted_applicable_filter_types=(),
    )
    return MarketCloseProof(
        filter_snapshot_sha256="b" * 64,
        filter_contract_sha256=active_filters.canonical_sha256,
        filters=active_filters,
        quantity=Decimal(quantity),
        mark_price=Decimal("2500"),
        mark_price_age_ms=Decimal("100"),
        observed_elapsed_seconds=Decimal("1"),
    )


def reserve_request(
    guard: MutationRequestGuard,
    *,
    elapsed_seconds: Decimal = Decimal("1"),
    retry_index: int = 0,
    **request: object,
) -> ReservedRequest:
    return guard.reserve(
        **request,
        elapsed_seconds=elapsed_seconds,
        retry_index=retry_index,
    )


def reserve_owned_order_read(guard: MutationRequestGuard) -> ReservedRequest:
    reservation = reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=guard.intent.query_parameters,
    )
    guard.note_read_succeeded(reservation)
    return reservation


def reserve_position_proof_reads(
    guard: MutationRequestGuard,
) -> tuple[tuple[str, str], ...]:
    reservations = {"/fapi/v1/order": reserve_owned_order_read(guard).request_sha256}
    for path, parameters in (
        ("/fapi/v1/userTrades", {"symbol": SYMBOL, "recvWindow": "5000"}),
        ("/fapi/v2/account", {"recvWindow": "5000"}),
        ("/fapi/v1/exchangeInfo", {}),
        ("/fapi/v1/premiumIndex", {"symbol": SYMBOL}),
    ):
        reservation = reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=parameters,
        )
        guard.note_read_succeeded(reservation)
        reservations[path] = reservation.request_sha256
    return tuple(sorted(reservations.items()))


def test_account_contract_requires_clean_one_way_isolated_one_x_without_correction() -> None:
    state = AccountState(
        can_trade=True,
        dual_side_position=False,
        multi_assets_margin=False,
        margin_type="ISOLATED",
        leverage=1,
        auto_add_margin=False,
        server_time_skew_ms=Decimal("12"),
        wallet_balance=Decimal("100"),
        available_balance=Decimal("50"),
        nonzero_positions=(),
        open_regular_order_ids=(),
        open_algo_order_ids=(),
    )

    validate_account_state(state, required_notional=Decimal("7.425"))

    with pytest.raises(MutationProtocolError, match="ACCOUNT_CONFIG_MISMATCH"):
        validate_account_state(
            AccountState(**{**state.__dict__, "leverage": 2}),
            required_notional=Decimal("7.425"),
        )
    with pytest.raises(MutationProtocolError, match="ACCOUNT_NOT_CLEAN"):
        validate_account_state(
            AccountState(**{**state.__dict__, "open_regular_order_ids": ("other",)}),
            required_notional=Decimal("7.425"),
        )
    with pytest.raises(MutationProtocolError, match="ACCOUNT_BALANCE_INSUFFICIENT"):
        validate_account_state(
            AccountState(**{**state.__dict__, "available_balance": Decimal("7")}),
            required_notional=Decimal("7.425"),
        )
    with pytest.raises(MutationProtocolError, match="INVALID_ACCOUNT_STATE_TYPE"):
        validate_account_state(
            AccountState(**{**state.__dict__, "leverage": True}),
            required_notional=Decimal("7.425"),
        )
    original_precision = getcontext().prec
    try:
        getcontext().prec = 1
        with pytest.raises(MutationProtocolError, match="SERVER_TIME_SKEW_EXCEEDED"):
            validate_account_state(
                AccountState(**{**state.__dict__, "server_time_skew_ms": Decimal("5400")}),
                required_notional=Decimal("7.425"),
            )
    finally:
        getcontext().prec = original_precision


def test_symbol_contract_has_no_runtime_symbol_or_order_type_choice() -> None:
    state = SymbolState(
        symbol="ETHUSDT",
        status="TRADING",
        contract_type="PERPETUAL",
        quote_asset="USDT",
        margin_asset="USDT",
        order_types=frozenset({"LIMIT", "MARKET"}),
        time_in_force=frozenset({"GTC", "GTX"}),
        filter_type_counts=(
            ("PRICE_FILTER", 1),
            ("LOT_SIZE", 1),
            ("MARKET_LOT_SIZE", 1),
            ("MIN_NOTIONAL", 1),
            ("PERCENT_PRICE", 1),
        ),
        uninterpreted_applicable_filter_types=(),
    )

    validate_symbol_state(state)

    with pytest.raises(MutationProtocolError, match="SYMBOL_CONTRACT_MISMATCH"):
        validate_symbol_state(SymbolState(**{**state.__dict__, "symbol": "BTCUSDT"}))
    with pytest.raises(MutationProtocolError, match="SYMBOL_CONTRACT_MISMATCH"):
        validate_symbol_state(
            SymbolState(**{**state.__dict__, "order_types": frozenset({"LIMIT"})})
        )
    with pytest.raises(MutationProtocolError, match="INVALID_SYMBOL_STATE_TYPE"):
        validate_symbol_state(
            SymbolState(
                **{
                    **state.__dict__,
                    "order_types": "UNSUPPORTED_LIMIT_ALIAS",
                    "time_in_force": "NOTGTX",
                }
            )
        )
    with pytest.raises(MutationProtocolError, match="FILTER_CARDINALITY_MISMATCH"):
        validate_symbol_state(
            SymbolState(
                **{
                    **state.__dict__,
                    "filter_type_counts": (("PRICE_FILTER", 1), ("LOT_SIZE", 2)),
                }
            )
        )
    with pytest.raises(MutationProtocolError, match="FILTER_CARDINALITY_MISMATCH"):
        validate_symbol_state(
            SymbolState(
                **{
                    **state.__dict__,
                    "filter_type_counts": tuple(
                        item for item in state.filter_type_counts if item[0] != "MARKET_LOT_SIZE"
                    ),
                }
            )
        )
    with pytest.raises(MutationProtocolError, match="INVALID_SYMBOL_STATE_TYPE"):
        validate_symbol_state(
            SymbolState(
                **{
                    **state.__dict__,
                    "filter_type_counts": [["PRICE_FILTER", 1]],
                }
            )
        )
    with pytest.raises(MutationProtocolError, match="UNKNOWN_APPLICABLE_FILTER"):
        validate_symbol_state(
            SymbolState(
                **{
                    **state.__dict__,
                    "uninterpreted_applicable_filter_types": ("NEW_LIMIT_FILTER",),
                }
            )
        )


def test_request_guard_rejects_production_put_and_second_create_before_send() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(intent=intent)
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )

    with pytest.raises(MutationProtocolError, match="CREATE_BUDGET_EXCEEDED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )
    with pytest.raises(MutationProtocolError, match="DEMO_ENDPOINT_MISMATCH"):
        reserve_request(
            guard,
            origin="https://fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    with pytest.raises(MutationProtocolError, match="REQUEST_NOT_ALLOWLISTED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="PUT",
            path="/fapi/v1/order",
            purpose=RequestPurpose.READ,
            parameters=intent.query_parameters,
        )


def test_account_mode_read_uses_the_dedicated_position_mode_route() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(intent=intent)
    position_mode = reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/positionSide/dual",
        purpose=RequestPurpose.READ,
        parameters={"recvWindow": "5000"},
    )
    guard.note_read_succeeded(position_mode)

    for forbidden_path in ("/fapi/v1/accountConfig", "/fapi/v3/positionRisk"):
        with pytest.raises(MutationProtocolError, match="REQUEST_NOT_ALLOWLISTED"):
            reserve_request(
                MutationRequestGuard(intent=intent),
                origin="https://demo-fapi.binance.com",
                method="GET",
                path=forbidden_path,
                purpose=RequestPurpose.READ,
                parameters={"recvWindow": "5000"},
            )


def test_request_guard_requires_durable_intent_and_query_proof_for_mutations() -> None:
    without_intent = MutationRequestGuard(intent=durable_intent(persisted=False))
    with pytest.raises(MutationProtocolError, match="DURABLE_INTENT_REQUIRED"):
        reserve_request(
            without_intent,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=without_intent.intent.probe_payload,
        )

    intent = durable_intent()
    fresh_guard = MutationRequestGuard(intent=intent)
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        fresh_guard.note_owned_order_proof(
            OwnedOrderProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                client_order_id=intent.client_order_id,
                status="NEW",
                executed_quantity=Decimal("0"),
                observed_after_http_attempt=0,
                source_request_sha256="0" * 64,
                accepted_elapsed_seconds=Decimal("0.5"),
                observed_elapsed_seconds=Decimal("0.75"),
            )
        )

    guard = MutationRequestGuard(intent=intent)
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    with pytest.raises(MutationProtocolError, match="OPEN_ORDER_PROOF_REQUIRED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="DELETE",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CANCEL,
            parameters=intent.cancel_parameters,
        )

    order_read = reserve_owned_order_read(guard)
    guard.note_owned_order_proof(
        OwnedOrderProof(
            intent_sha256=intent.intent_sha256,
            symbol=SYMBOL,
            client_order_id=intent.client_order_id,
            status="NEW",
            executed_quantity=Decimal("0"),
            observed_after_http_attempt=guard.ledger.total_http_requests,
            source_request_sha256=order_read.request_sha256,
            accepted_elapsed_seconds=Decimal("0.5"),
            observed_elapsed_seconds=Decimal("1"),
        )
    )
    with pytest.raises(MutationProtocolError, match="ORDER_PARAMETER_MISMATCH"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="DELETE",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CANCEL,
            parameters={**intent.cancel_parameters, "origClientOrderId": "wrong"},
        )
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="DELETE",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CANCEL,
        parameters=intent.cancel_parameters,
    )
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        guard.note_owned_order_proof(
            OwnedOrderProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                client_order_id=intent.client_order_id,
                status="NEW",
                executed_quantity=Decimal("0"),
                observed_after_http_attempt=guard.ledger.total_http_requests,
                source_request_sha256=order_read.request_sha256,
                accepted_elapsed_seconds=Decimal("0.5"),
                observed_elapsed_seconds=Decimal("1"),
            )
        )


def test_second_cancel_requires_fresh_new_or_partial_status() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(intent=intent)
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    order_read = reserve_owned_order_read(guard)
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        guard.note_owned_order_proof(
            OwnedOrderProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                client_order_id=intent.client_order_id,
                status="PENDING_CANCEL",
                executed_quantity=Decimal("0"),
                observed_after_http_attempt=2,
                source_request_sha256=order_read.request_sha256,
                accepted_elapsed_seconds=Decimal("0.5"),
                observed_elapsed_seconds=Decimal("1"),
            )
        )


def test_request_guard_blocks_account_mutations_and_unowned_emergency_close() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(intent=intent)
    with pytest.raises(MutationProtocolError, match="REQUEST_NOT_ALLOWLISTED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/leverage",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )
    with pytest.raises(MutationProtocolError, match="OWNED_POSITION_PROOF_REQUIRED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.EMERGENCY_CLOSE,
            parameters=intent.emergency_close_payload(Decimal("0.001")),
        )


def test_request_guard_binds_cancel_and_emergency_close_to_exact_owned_payload() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
        ),
    )
    position_sources = reserve_position_proof_reads(guard)
    order_source = dict(position_sources)["/fapi/v1/order"]
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        guard.note_owned_order_proof(
            OwnedOrderProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                client_order_id="wrong-client-id",
                status="NEW",
                executed_quantity=Decimal("0"),
                observed_after_http_attempt=guard.ledger.total_http_requests,
                source_request_sha256=order_source,
                accepted_elapsed_seconds=Decimal("0.5"),
                observed_elapsed_seconds=Decimal("1"),
            )
        )

    open_remainder = OwnedPositionProof(
        intent_sha256=intent.intent_sha256,
        symbol=SYMBOL,
        residual_quantity=Decimal("0.001"),
        owned_executed_quantity=Decimal("0.001"),
        position_direction="LONG",
        probe_terminal_status="PARTIALLY_FILLED",
        open_remainder_quantity=Decimal("0.002"),
        other_activity_absent=True,
        market_close_proof=market_close_proof(),
        observed_after_http_attempt=guard.ledger.total_http_requests,
        source_request_sha256s=position_sources,
        observed_elapsed_seconds=Decimal("1"),
    )
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        guard.note_owned_position_proof(open_remainder)

    guard.note_owned_position_proof(
        OwnedPositionProof(
            **{
                **open_remainder.__dict__,
                "probe_terminal_status": "CANCELED",
                "open_remainder_quantity": Decimal("0"),
            }
        )
    )
    exact_close = intent.emergency_close_payload(Decimal("0.001"))
    for changed in (
        {"symbol": "BTCUSDT"},
        {"side": "BUY"},
        {"quantity": "0.002"},
        {"reduceOnly": "false"},
        {"newClientOrderId": "wrong"},
    ):
        with pytest.raises(MutationProtocolError, match="ORDER_PARAMETER_MISMATCH"):
            reserve_request(
                guard,
                origin="https://demo-fapi.binance.com",
                method="POST",
                path="/fapi/v1/order",
                purpose=RequestPurpose.EMERGENCY_CLOSE,
                parameters={**exact_close, **changed},
            )
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.EMERGENCY_CLOSE,
        parameters=exact_close,
    )
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=intent.emergency_query_parameters,
    )


def test_position_proof_requires_all_exact_post_create_source_reads() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
        ),
    )
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        guard.note_owned_position_proof(
            OwnedPositionProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                residual_quantity=Decimal("0.001"),
                owned_executed_quantity=Decimal("0.001"),
                position_direction="LONG",
                probe_terminal_status="FILLED",
                open_remainder_quantity=Decimal("0"),
                other_activity_absent=True,
                market_close_proof=market_close_proof(),
                observed_after_http_attempt=1,
                source_request_sha256s=(),
                observed_elapsed_seconds=Decimal("1"),
            )
        )


def test_failed_reads_cannot_be_promoted_to_order_or_position_ownership() -> None:
    intent = durable_intent()
    order_guard = MutationRequestGuard(intent=intent)
    reserve_request(
        order_guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    failed_order = reserve_request(
        order_guard,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=intent.query_parameters,
    )
    order_guard.note_read_failed(failed_order)
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        order_guard.note_owned_order_proof(
            OwnedOrderProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                client_order_id=intent.client_order_id,
                status="NEW",
                executed_quantity=Decimal("0"),
                observed_after_http_attempt=order_guard.ledger.total_http_requests,
                source_request_sha256=failed_order.request_sha256,
                accepted_elapsed_seconds=Decimal("0.5"),
                observed_elapsed_seconds=Decimal("1"),
            )
        )

    position_guard = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
        ),
    )
    prior_sources = reserve_position_proof_reads(position_guard)
    failed_account = reserve_request(
        position_guard,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v2/account",
        purpose=RequestPurpose.READ,
        parameters={"recvWindow": "5000"},
    )
    position_guard.note_read_failed(failed_account)
    with pytest.raises(MutationProtocolError, match="OWNERSHIP_PROOF_MISMATCH"):
        position_guard.note_owned_position_proof(
            OwnedPositionProof(
                intent_sha256=intent.intent_sha256,
                symbol=SYMBOL,
                residual_quantity=Decimal("0.001"),
                owned_executed_quantity=Decimal("0.001"),
                position_direction="LONG",
                probe_terminal_status="FILLED",
                open_remainder_quantity=Decimal("0"),
                other_activity_absent=True,
                market_close_proof=market_close_proof(),
                observed_after_http_attempt=position_guard.ledger.total_http_requests,
                source_request_sha256s=prior_sources,
                observed_elapsed_seconds=Decimal("1"),
            )
        )


def test_post_create_reads_cannot_consume_reserved_mutation_cleanup_slots() -> None:
    intent = durable_intent()
    guard = MutationRequestGuard(intent=intent)
    for _ in range(12):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    for _ in range(MAX_POST_CREATE_READ_REQUESTS - 1):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    order_read = reserve_owned_order_read(guard)
    with pytest.raises(MutationProtocolError, match="POST_CREATE_READ_BUDGET_EXCEEDED"):
        reserve_request(
            guard,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    guard.note_owned_order_proof(
        OwnedOrderProof(
            intent_sha256=intent.intent_sha256,
            symbol=SYMBOL,
            client_order_id=intent.client_order_id,
            status="NEW",
            executed_quantity=Decimal("0"),
            observed_after_http_attempt=guard.ledger.total_http_requests,
            source_request_sha256=order_read.request_sha256,
            accepted_elapsed_seconds=Decimal("0.5"),
            observed_elapsed_seconds=Decimal("1"),
        )
    )
    cleanup = reserve_request(
        guard,
        origin="https://demo-fapi.binance.com",
        method="DELETE",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CANCEL,
        parameters=intent.cancel_parameters,
    )
    assert cleanup.ledger.cancel_requests == 1


@pytest.mark.parametrize(
    "proof",
    [
        market_close_proof(quantity="0.0015"),
        market_close_proof(min_notional="5"),
        MarketCloseProof(
            **{
                **market_close_proof().__dict__,
                "mark_price_age_ms": Decimal("1000.001"),
            }
        ),
    ],
)
def test_market_close_proof_never_rounds_or_ignores_filter_or_freshness(
    proof: MarketCloseProof,
) -> None:
    with pytest.raises(MutationProtocolError, match="MARKET_CLOSE_FILTER_VIOLATION"):
        validate_market_close_proof(proof, max_owned_quantity=Decimal("0.003"))


def test_market_close_lattice_is_independent_of_process_global_precision() -> None:
    original_precision = getcontext().prec
    try:
        getcontext().prec = 1
        non_lattice = market_close_proof(
            quantity="0.0025",
            min_notional="0.0000001",
        )
        with pytest.raises(MutationProtocolError, match="MARKET_CLOSE_FILTER_VIOLATION"):
            validate_market_close_proof(
                non_lattice,
                max_owned_quantity=Decimal("0.003"),
            )
    finally:
        getcontext().prec = original_precision


def test_filter_contract_hashes_and_create_freshness_are_recomputed() -> None:
    with pytest.raises(MutationProtocolError, match="INVALID_ORDER_DERIVATION_PROOF"):
        OrderDerivationProof(
            **{
                **derivation_proof().__dict__,
                "filter_contract_sha256": "0" * 64,
            }
        )
    with pytest.raises(MutationProtocolError, match="MARKET_CLOSE_FILTER_VIOLATION"):
        MarketCloseProof(
            **{
                **market_close_proof().__dict__,
                "filter_contract_sha256": "0" * 64,
            }
        )

    intent = durable_intent()
    with pytest.raises(MutationProtocolError, match="ORDER_INPUT_STALE"):
        reserve_request(
            MutationRequestGuard(intent=intent),
            elapsed_seconds=Decimal("1.401"),
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )

    close_guard = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
        ),
    )
    close_sources = reserve_position_proof_reads(close_guard)
    close_guard.note_owned_position_proof(
        OwnedPositionProof(
            intent_sha256=intent.intent_sha256,
            symbol=SYMBOL,
            residual_quantity=Decimal("0.001"),
            owned_executed_quantity=Decimal("0.001"),
            position_direction="LONG",
            probe_terminal_status="FILLED",
            open_remainder_quantity=Decimal("0"),
            other_activity_absent=True,
            market_close_proof=market_close_proof(),
            observed_after_http_attempt=close_guard.ledger.total_http_requests,
            source_request_sha256s=close_sources,
            observed_elapsed_seconds=Decimal("1"),
        )
    )
    with pytest.raises(MutationProtocolError, match="MARKET_CLOSE_FILTER_VIOLATION"):
        reserve_request(
            close_guard,
            elapsed_seconds=Decimal("1.901"),
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.EMERGENCY_CLOSE,
            parameters=intent.emergency_close_payload(Decimal("0.001")),
        )


def test_durable_intent_recomputes_order_and_binds_frozen_protocol() -> None:
    intent = durable_intent()
    assert intent.probe_order == frozen_order()
    assert intent.protocol_commit == "d" * 40
    assert intent.protocol_tag_object == "e" * 40
    assert intent.protocol_sha256 == "f" * 64
    assert intent.filter_snapshot_sha256 == "b" * 64


def test_request_guard_reserves_cleanup_capacity_and_recovers_durable_budgets() -> None:
    intent = durable_intent()
    exhausted = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(total_http_requests=MAX_HTTP_REQUESTS - POST_CREATE_HTTP_RESERVE + 1),
    )
    with pytest.raises(MutationProtocolError, match="CLEANUP_RESERVE_EXHAUSTED"):
        reserve_request(
            exhausted,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )

    recovered = MutationRequestGuard(
        intent=intent,
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
        ),
    )
    with pytest.raises(MutationProtocolError, match="CREATE_BUDGET_EXCEEDED"):
        reserve_request(
            recovered,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )


def test_request_reservation_hash_binds_stage_timing_parameters_and_ledger() -> None:
    intent = durable_intent()
    first = reserve_request(
        MutationRequestGuard(intent=intent),
        elapsed_seconds=Decimal("1"),
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    later = reserve_request(
        MutationRequestGuard(intent=intent),
        elapsed_seconds=Decimal("1.1"),
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )

    assert first.ledger.stage is RequestStage.CREATE_ATTEMPTED
    assert first.ledger.create_requests == 1
    assert len(first.request_sha256) == 64
    assert first.request_sha256 != later.request_sha256
    with pytest.raises(MutationProtocolError, match="INVALID_MUTATION_LEDGER"):
        MutationLedger(total_http_requests=1, create_requests=1)


def test_request_guard_enforces_deadline_and_retry_before_io() -> None:
    intent = durable_intent()
    with pytest.raises(MutationProtocolError, match="CREATE_DEADLINE_EXCEEDED"):
        reserve_request(
            MutationRequestGuard(intent=intent),
            elapsed_seconds=Decimal("60.001"),
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )
    with pytest.raises(MutationProtocolError, match="MUTATION_RETRY_FORBIDDEN"):
        reserve_request(
            MutationRequestGuard(intent=intent),
            retry_index=1,
            origin="https://demo-fapi.binance.com",
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=intent.probe_payload,
        )

    reads = MutationRequestGuard(intent=intent)
    first_read = reserve_request(
        reads,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/time",
        purpose=RequestPurpose.READ,
        parameters={},
    )
    with pytest.raises(MutationProtocolError, match="READ_RETRY_NOT_PROVEN"):
        reserve_request(
            reads,
            retry_index=1,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    reads.note_read_failed(first_read)
    with pytest.raises(MutationProtocolError, match="READ_RETRY_NOT_PROVEN"):
        reserve_request(
            reads,
            retry_index=1,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/exchangeInfo",
            purpose=RequestPurpose.READ,
            parameters={},
        )
    reserve_request(
        reads,
        retry_index=1,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/time",
        purpose=RequestPurpose.READ,
        parameters={},
    )
    with pytest.raises(MutationProtocolError, match="READ_RETRY_BUDGET_EXCEEDED"):
        reserve_request(
            reads,
            retry_index=1,
            origin="https://demo-fapi.binance.com",
            method="GET",
            path="/fapi/v1/time",
            purpose=RequestPurpose.READ,
            parameters={},
        )

    cancel = MutationRequestGuard(intent=intent)
    reserve_request(
        cancel,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=intent.probe_payload,
    )
    order_read = reserve_owned_order_read(cancel)
    cancel.note_owned_order_proof(
        OwnedOrderProof(
            intent_sha256=intent.intent_sha256,
            symbol=SYMBOL,
            client_order_id=intent.client_order_id,
            status="NEW",
            executed_quantity=Decimal("0"),
            observed_after_http_attempt=2,
            source_request_sha256=order_read.request_sha256,
            accepted_elapsed_seconds=Decimal("1"),
            observed_elapsed_seconds=Decimal("1.5"),
        )
    )
    with pytest.raises(MutationProtocolError, match="REQUEST_DEADLINE_EXCEEDED"):
        reserve_request(
            cancel,
            elapsed_seconds=Decimal("1.4"),
            origin="https://demo-fapi.binance.com",
            method="DELETE",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CANCEL,
            parameters=intent.cancel_parameters,
        )
    late_cleanup = reserve_request(
        cancel,
        elapsed_seconds=Decimal("4.001"),
        origin="https://demo-fapi.binance.com",
        method="DELETE",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CANCEL,
        parameters=intent.cancel_parameters,
    )
    assert late_cleanup.ledger.cancel_requests == 1


def test_quantity_rounds_up_to_step_without_exceeding_cap() -> None:
    order = derive_limit_order(
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        filters=filters(),
    )

    assert order.symbol == "ETHUSDT"
    assert order.side == "BUY"
    assert order.order_type == "LIMIT"
    assert order.time_in_force == "GTX"
    assert order.position_side == "BOTH"
    assert order.reduce_only is False
    assert order.price == Decimal("2475.00")
    assert order.quantity == Decimal("0.003")
    assert order.notional == Decimal("7.42500")


def test_quantity_is_never_scaled_above_frozen_notional_cap() -> None:
    with pytest.raises(MutationProtocolError, match="NOTIONAL_CAP_EXCEEDED"):
        derive_limit_order(
            best_bid=Decimal("2500.00"),
            best_ask=Decimal("2500.01"),
            mark_price=Decimal("2500.00"),
            filters=filters(min_quantity="0.005"),
        )


def test_book_price_and_filters_fail_closed_when_not_reproducible() -> None:
    with pytest.raises(MutationProtocolError, match="BEST_BID_NOT_TICK_ALIGNED"):
        derive_limit_order(
            best_bid=Decimal("2500.005"),
            best_ask=Decimal("2500.01"),
            mark_price=Decimal("2500.00"),
            filters=filters(),
        )

    with pytest.raises(MutationProtocolError, match="INVALID_STEP_SIZE"):
        filters(step_size="0")


def test_price_and_quantity_alignment_use_exchange_filter_origins() -> None:
    order = derive_limit_order(
        best_bid=Decimal("1000.015"),
        best_ask=Decimal("1000.025"),
        mark_price=Decimal("1000.00"),
        filters=filters(
            min_price="0.005",
            tick_size="0.01",
            min_quantity="0.002",
            step_size="0.003",
            min_notional="3",
        ),
    )

    assert order.price == Decimal("990.005")
    assert order.quantity == Decimal("0.005")
    assert (order.price - Decimal("0.005")) % Decimal("0.01") == 0
    assert (order.quantity - Decimal("0.002")) % Decimal("0.003") == 0


def test_percent_price_filter_is_proved_against_fresh_mark_price() -> None:
    with pytest.raises(MutationProtocolError, match="PERCENT_PRICE_VIOLATION"):
        derive_limit_order(
            best_bid=Decimal("2500.00"),
            best_ask=Decimal("2500.01"),
            mark_price=Decimal("2000.00"),
            filters=filters(percent_price_multiplier_up="1.05"),
        )

    # The frozen Binance USD-M rule is side-specific: multiplierDown applies to SELL.
    order = derive_limit_order(
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        filters=filters(percent_price_multiplier_down="0.995"),
    )
    assert order.price == Decimal("2475.00")


def test_decimal_derivation_is_independent_of_process_global_precision() -> None:
    original_precision = getcontext().prec
    try:
        getcontext().prec = 6
        assert frozen_order().quantity == Decimal("0.003")
        assert frozen_order().notional == Decimal("7.42500")
        getcontext().prec = 1
        with pytest.raises(MutationProtocolError, match="FROZEN_ORDER_CONTRACT_MISMATCH"):
            FrozenLimitOrder(
                price=Decimal("9999999"),
                quantity=Decimal("0.0000011"),
            )
    finally:
        getcontext().prec = original_precision


def test_client_order_id_is_deterministic_and_safe() -> None:
    client_order_id = build_client_order_id(
        runtime_commit="a" * 40,
        session_nonce="0123456789abcdef",
    )

    assert client_order_id == "g1b16-aaaaaaaaaa-0123456789abcdef-01"
    assert len(client_order_id) <= 36

    with pytest.raises(MutationProtocolError, match="INVALID_SESSION_NONCE"):
        build_client_order_id(runtime_commit="a" * 40, session_nonce="not-random")


def test_order_payload_must_match_every_frozen_field() -> None:
    order = derive_limit_order(
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        filters=filters(),
    )
    client_order_id = build_client_order_id("a" * 40, "0123456789abcdef")
    payload = order.as_payload(client_order_id=client_order_id)

    validate_order_payload(payload, expected=order, client_order_id=client_order_id)

    with pytest.raises(MutationProtocolError, match="ORDER_PARAMETER_MISMATCH"):
        validate_order_payload(
            {**payload, "timeInForce": "GTC"},
            expected=order,
            client_order_id=client_order_id,
        )

    with pytest.raises(MutationProtocolError, match="UNFROZEN_ORDER_PARAMETER"):
        validate_order_payload(
            {**payload, "priceMatch": "QUEUE"},
            expected=order,
            client_order_id=client_order_id,
        )

    with pytest.raises(MutationProtocolError, match="FROZEN_ORDER_CONTRACT_MISMATCH"):
        FrozenLimitOrder(
            price=order.price,
            quantity=order.quantity,
            side="SELL",
        )


def test_duplicate_protection_distinguishes_not_found_unknown_and_fills() -> None:
    confirmed_absent = DuplicateLookup(
        outcome=LookupOutcome.CONFIRMED_NOT_FOUND,
        status=None,
        executed_quantity=Decimal("0"),
        global_state_clean=True,
    )
    assert (
        classify_duplicate(confirmed_absent, attempt_record_exists=False)
        is DuplicateDisposition.CREATE_ONCE
    )
    assert (
        classify_duplicate(confirmed_absent, attempt_record_exists=True)
        is DuplicateDisposition.SESSION_CONSUMED_NO_CREATE
    )

    with pytest.raises(MutationProtocolError, match="DUPLICATE_LOOKUP_UNKNOWN"):
        classify_duplicate(
            DuplicateLookup(
                outcome=LookupOutcome.UNKNOWN,
                status=None,
                executed_quantity=Decimal("0"),
                global_state_clean=True,
            ),
            attempt_record_exists=False,
        )

    assert (
        classify_duplicate(
            DuplicateLookup(
                outcome=LookupOutcome.FOUND,
                status="CANCELED",
                executed_quantity=Decimal("0.001"),
                global_state_clean=False,
            ),
            attempt_record_exists=True,
        )
        is DuplicateDisposition.RECONCILE_FILL_NO_CREATE
    )
    with pytest.raises(MutationProtocolError, match="UNEXPLAINED_PRIOR_ORDER_STATUS"):
        classify_duplicate(
            DuplicateLookup(
                outcome=LookupOutcome.FOUND,
                status="PENDING_CANCEL",
                executed_quantity=Decimal("0"),
                global_state_clean=False,
            ),
            attempt_record_exists=True,
        )


def test_only_exact_create_query_cancel_lifecycle_can_pass() -> None:
    validate_lifecycle_pass(passing_lifecycle())
    validate_lifecycle_pass(
        passing_lifecycle(
            total_http_requests=NORMAL_TOTAL_HTTP_REQUESTS + 1,
            read_retries=1,
        )
    )


def test_lifecycle_timing_is_independent_of_process_global_precision() -> None:
    original_precision = getcontext().prec
    try:
        getcontext().prec = 1
        with pytest.raises(MutationProtocolError, match="INVALID_EVIDENCE_TIMING"):
            validate_lifecycle_pass(
                passing_lifecycle(
                    total_runtime_seconds=Decimal("20"),
                    create_elapsed_seconds=Decimal("20"),
                    accepted_to_cancel_seconds=Decimal("1"),
                )
            )
    finally:
        getcontext().prec = original_precision


def test_unexpected_fill_cannot_pass_even_after_successful_cleanup() -> None:
    with pytest.raises(MutationProtocolError, match="UNEXPECTED_FILL"):
        validate_lifecycle_pass(
            passing_lifecycle(
                emergency_close_requests=1,
                observed_statuses=("NEW", "PARTIALLY_FILLED", "CANCELED"),
                executed_quantity=Decimal("0.001"),
            )
        )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"cancel_requests": 3}, "MUTATION_BUDGET_EXCEEDED"),
        ({"modify_requests": 1}, "MODIFY_FORBIDDEN"),
        ({"account_setting_mutations": 1}, "ACCOUNT_SETTING_MUTATION_FORBIDDEN"),
        ({"unexpected_mutations": 1}, "UNEXPECTED_MUTATION"),
        ({"production_contacted": True}, "PRODUCTION_CONTACTED"),
        ({"runtime_binding_passed": False}, "RUNTIME_BINDING_FAILED"),
        ({"credential_cleanup_passed": False}, "CREDENTIAL_CLEANUP_FAILED"),
        (
            {"final_nonzero_positions": (("BTCUSDT", Decimal("0.001")),)},
            "FINAL_ACCOUNT_NOT_CLEAN",
        ),
        ({"final_open_regular_orders": 1}, "FINAL_ACCOUNT_NOT_CLEAN"),
        ({"read_retries": 2}, "READ_RETRY_BUDGET_EXCEEDED"),
        ({"read_retries": -1}, "INVALID_EVIDENCE_COUNTER"),
        (
            {"total_http_requests": MAX_HTTP_REQUESTS + 1},
            "HTTP_REQUEST_BUDGET_EXCEEDED",
        ),
        ({"total_http_requests": 0}, "NORMAL_PATH_HTTP_REQUEST_MISMATCH"),
        (
            {"total_http_requests": NORMAL_TOTAL_HTTP_REQUESTS + 1},
            "NORMAL_PATH_HTTP_REQUEST_MISMATCH",
        ),
        ({"total_runtime_seconds": Decimal("180.001")}, "TOTAL_RUNTIME_EXCEEDED"),
        ({"create_elapsed_seconds": Decimal("60.001")}, "CREATE_DEADLINE_EXCEEDED"),
        (
            {"accepted_to_cancel_seconds": Decimal("3.001")},
            "CANCEL_DEADLINE_EXCEEDED",
        ),
        (
            {
                "total_runtime_seconds": Decimal("0"),
                "create_elapsed_seconds": Decimal("0"),
                "accepted_to_cancel_seconds": Decimal("0"),
            },
            "INVALID_EVIDENCE_TIMING",
        ),
        (
            {
                "total_runtime_seconds": Decimal("20"),
                "create_elapsed_seconds": Decimal("20"),
                "accepted_to_cancel_seconds": Decimal("1"),
            },
            "INVALID_EVIDENCE_TIMING",
        ),
        ({"fee_delta": Decimal("0.01")}, "UNEXPECTED_ECONOMIC_DELTA"),
        ({"funding_delta": Decimal("0.01")}, "UNEXPECTED_ECONOMIC_DELTA"),
        ({"wallet_balance_delta": Decimal("0.01")}, "UNEXPECTED_ECONOMIC_DELTA"),
        ({"preflight_passed": False}, "PREFLIGHT_NOT_PROVEN"),
        ({"final_account_config_matches": False}, "FINAL_ACCOUNT_CONFIG_MISMATCH"),
    ],
)
def test_lifecycle_failures_are_stable_and_fail_closed(
    overrides: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(MutationProtocolError, match=reason):
        validate_lifecycle_pass(passing_lifecycle(**overrides))
