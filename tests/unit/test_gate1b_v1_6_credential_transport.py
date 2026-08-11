from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

import global_quant.gate1b.credential_transport as credential_transport_module
import global_quant.gate1b.process_boundary as process_boundary_module
from global_quant.gate1b.credential_http import CredentialHttpError, _RedirectSafeHttpClient
from global_quant.gate1b.credential_transport import (
    CredentialTransportError,
    ProcessBoundCredentialTransport,
    ResponseKind,
    build_production_credential_transport,
)
from global_quant.gate1b.execution_journal import (
    GenerationCapability,
    PreIntentReadReservation,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    DurableIntent,
    LimitOrderFilters,
    MutationLedger,
    MutationRequestGuard,
    OrderDerivationProof,
    RequestPurpose,
)
from global_quant.gate1b.safety import DemoCredentials


class FakeSignedClient:
    def __init__(
        self,
        responses: list[bytes | BaseException],
        *,
        base_url: str = DEMO_HTTP_ORIGIN,
    ) -> None:
        self.base_url = base_url
        self.responses = list(responses)
        self.calls: list[tuple[HttpMethod, str, dict[str, str] | None]] = []
        self.deadlines: list[int | None] = []

    def sign_request(
        self,
        http_method: HttpMethod,
        url_path: str,
        payload: dict[str, str] | None = None,
        ratelimiter_keys: list[str] | None = None,
        *,
        absolute_deadline_ns: int | None = None,
    ) -> bytes:
        self.calls.append((http_method, url_path, payload))
        self.deadlines.append(absolute_deadline_ns)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _TestHardDeadline:
    def __init__(self) -> None:
        self.checks = 0
        self.intact = True

    def assert_intact(self) -> None:
        self.checks += 1
        if not self.intact:
            raise process_boundary_module.ProcessBoundaryError("TEST_HARD_DEADLINE_TAMPERED")


def _test_io_authority():
    identity = process_boundary_module.read_process_identity(os.getpid())
    assert identity is not None
    gate = process_boundary_module._NetworkGate(
        ready=True,
        guard_attestation=process_boundary_module._CREDENTIAL_GUARD_ATTESTATION,
    )
    bootstrap = process_boundary_module.ChildBootstrap(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        deadline=process_boundary_module.AbsoluteDeadline(time.monotonic() + 3_600.0),
        hard_deadline=_TestHardDeadline(),  # type: ignore[arg-type]
        identity=identity,
        channel=object(),  # type: ignore[arg-type]
        workload_kind=process_boundary_module.CredentialWorkloadKind.PRODUCTION,
        _network_gate=gate,
        _bootstrap_attestation=process_boundary_module._CHILD_BOOTSTRAP_ATTESTATION,
    )
    return bootstrap.issue_io_authority()


def _test_transport(
    signed_client: FakeSignedClient,
    *,
    wall_clock_ms=None,
    monotonic_ns=None,
) -> ProcessBoundCredentialTransport:
    """Test-side signer injection backed by an exact live child authority."""

    return ProcessBoundCredentialTransport(
        signed_client,
        io_authority=_test_io_authority(),
        _construction_token=(credential_transport_module._PRODUCTION_TRANSPORT_CONSTRUCTION_TOKEN),
        wall_clock_ms=wall_clock_ms,
        monotonic_ns=monotonic_ns,
    )


_TEST_ABSOLUTE_DEADLINE_NS = 10**30


def _execute(
    transport: ProcessBoundCredentialTransport,
    reservation: object,
):
    return transport.execute(  # type: ignore[arg-type]
        reservation,
        absolute_deadline_ns=_TEST_ABSOLUTE_DEADLINE_NS,
    )


def _execute_pre_intent(
    transport: ProcessBoundCredentialTransport,
    reservation: PreIntentReadReservation,
):
    return transport.execute_pre_intent(
        reservation,
        absolute_deadline_ns=_TEST_ABSOLUTE_DEADLINE_NS,
    )


def _intent() -> DurableIntent:
    filters = LimitOrderFilters(
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
    proof = OrderDerivationProof(
        best_bid=Decimal("2000.00"),
        best_ask=Decimal("2000.01"),
        mark_price=Decimal("2000.00"),
        filters=filters,
        filter_snapshot_sha256="6" * 64,
        filter_contract_sha256=filters.canonical_sha256,
        book_age_ms=Decimal("100"),
        mark_age_ms=Decimal("100"),
        observed_elapsed_seconds=Decimal("1"),
    )
    return DurableIntent(
        authorization_id="g1b16-0123456789abcdef",
        protocol_commit="1" * 40,
        protocol_tag_object="2" * 40,
        protocol_sha256="3" * 64,
        runtime_commit="4" * 40,
        session_nonce="5" * 16,
        order_derivation=proof,
        persisted=True,
    )


def _read_reservation(
    guard: MutationRequestGuard,
    *,
    path: str = "/fapi/v1/order",
    elapsed: str = "2",
):
    parameters: dict[str, str]
    parameters = {
        "/fapi/v1/time": {},
        "/fapi/v1/exchangeInfo": {},
        "/fapi/v1/ticker/bookTicker": {"symbol": "ETHUSDT"},
        "/fapi/v1/premiumIndex": {"symbol": "ETHUSDT"},
        "/fapi/v1/positionSide/dual": {"recvWindow": "5000"},
        "/fapi/v1/symbolConfig": {"recvWindow": "5000", "symbol": "ETHUSDT"},
        "/fapi/v1/openOrders": {"recvWindow": "5000"},
        "/fapi/v1/openAlgoOrders": {"recvWindow": "5000"},
        "/fapi/v1/order": guard.intent.query_parameters,
        "/fapi/v1/userTrades": {"recvWindow": "5000", "symbol": "ETHUSDT"},
        "/fapi/v2/account": {"recvWindow": "5000"},
    }[path]
    return guard.reserve(
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path=path,
        purpose=RequestPurpose.READ,
        parameters=parameters,
        elapsed_seconds=Decimal(elapsed),
        retry_index=0,
    )


def _pre_intent_read_reservation() -> PreIntentReadReservation:
    elapsed = Decimal("0.25")
    return PreIntentReadReservation.build(
        session_authority_sha256="a" * 64,
        generation=1,
        deadline_ns=180_000_000_000,
        path="/fapi/v1/time",
        parameters={},
        ledger=MutationLedger(
            total_http_requests=1,
            last_elapsed_seconds=elapsed,
        ),
        elapsed_seconds=elapsed,
        retry_index=0,
    )


def _exchange_info_payload() -> dict[str, object]:
    return {
        "timezone": "UTC",
        "symbols": [
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "orderTypes": ["MARKET", "LIMIT"],
                "timeInForce": ["GTC", "GTX"],
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "1000.00",
                        "maxPrice": "5000.00",
                        "tickSize": "0.01",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "100.000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "50.000",
                        "stepSize": "0.001",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {
                        "filterType": "PERCENT_PRICE",
                        "multiplierUp": "1.05",
                        "multiplierDown": "0.85",
                    },
                ],
            }
        ],
    }


def _regular_orders_payload() -> list[dict[str, object]]:
    return [
        {
            "orderId": 987654321,
            "clientOrderId": "g1b16-owned-client",
            "symbol": "ETHUSDT",
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.001",
            "origQty": "0.003",
            "reduceOnly": False,
            "side": "BUY",
            "positionSide": "BOTH",
            "type": "LIMIT",
        }
    ]


def _algo_orders_payload() -> list[dict[str, object]]:
    return [
        {
            "algoId": 123456789,
            "clientAlgoId": "external-algo-client",
            "symbol": "ETHUSDT",
            "algoStatus": "NEW",
            "quantity": "0.004",
            "reduceOnly": True,
            "side": "SELL",
            "positionSide": "BOTH",
            "orderType": "STOP_MARKET",
        }
    ]


def _order_payload(client_order_id: str) -> dict[str, object]:
    return {
        "orderId": 987654321,
        "clientOrderId": client_order_id,
        "symbol": "ETHUSDT",
        "status": "NEW",
        "executedQty": "0",
        "origQty": "0.005",
        "price": "1980.00",
        "reduceOnly": False,
        "side": "BUY",
        "positionSide": "BOTH",
        "type": "LIMIT",
        "timeInForce": "GTX",
    }


def _trades_payload() -> list[dict[str, object]]:
    return [
        {
            "id": 112233,
            "orderId": 987654321,
            "symbol": "ETHUSDT",
            "qty": "0.001",
            "commission": "0.00001",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.25",
            "buyer": True,
            "rawCanary": "must-not-be-retained",
        }
    ]


def _account_payload() -> dict[str, object]:
    return {
        "canTrade": True,
        "multiAssetsMargin": False,
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "100.25",
                "availableBalance": "99.75",
                "accountAlias": "must-not-be-retained",
            },
            {
                "asset": "BNB",
                "walletBalance": "0",
                "availableBalance": "0",
            },
        ],
        "positions": [
            {
                "symbol": "ETHUSDT",
                "positionAmt": "0.001",
                "positionSide": "BOTH",
                "entryPrice": "2000",
                "accountIdentifier": "must-not-be-retained",
            },
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0",
                "positionSide": "BOTH",
            },
        ],
        "accountAlias": "must-not-be-retained",
    }


def test_execute_binds_one_response_to_the_exact_reservation_digest() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    client = FakeSignedClient(
        [json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii")]
    )
    transport = _test_transport(client)

    result = _execute(transport, reservation)

    assert result.request_sha256 == reservation.request_sha256
    assert result.logical_request_sha256 == reservation.logical_request_sha256
    assert result.kind is ResponseKind.ORDER_OBSERVATION
    assert result.field("status") == "NEW"
    assert result.field("executedQty") == "0"
    assert len(result.result_sha256) == 64
    assert client.calls == [(HttpMethod.GET, reservation.path, dict(reservation.parameters))]


@pytest.mark.parametrize(
    ("path", "payload", "kind", "expected_fields"),
    [
        (
            "/fapi/v1/time",
            {"serverTime": 1_786_370_000_000, "rawCanary": "drop-me"},
            ResponseKind.SERVER_TIME,
            {"serverTime": 1_786_370_000_000},
        ),
        (
            "/fapi/v1/ticker/bookTicker",
            {
                "symbol": "ETHUSDT",
                "bidPrice": "2500.00",
                "bidQty": "1.500",
                "askPrice": "2500.01",
                "askQty": "1.250",
                "lastUpdateId": 1234,
                "time": 1_786_370_000_001,
            },
            ResponseKind.BOOK_TICKER,
            {
                "askPrice": "2500.01",
                "askQty": "1.25",
                "bidPrice": "2500",
                "bidQty": "1.5",
                "lastUpdateId": 1234,
                "symbol": "ETHUSDT",
                "time": 1_786_370_000_001,
            },
        ),
        (
            "/fapi/v1/premiumIndex",
            {
                "symbol": "ETHUSDT",
                "markPrice": "2500.00",
                "time": 1_786_370_000_002,
                "indexPrice": "2499.9",
            },
            ResponseKind.MARK_PRICE,
            {
                "markPrice": "2500",
                "symbol": "ETHUSDT",
                "time": 1_786_370_000_002,
            },
        ),
        (
            "/fapi/v1/positionSide/dual",
            {"dualSidePosition": False},
            ResponseKind.POSITION_MODE,
            {"dualSidePosition": False},
        ),
        (
            "/fapi/v1/symbolConfig",
            {
                "symbol": "ETHUSDT",
                "marginType": "ISOLATED",
                "leverage": 1,
                "isAutoAddMargin": False,
                "rawCanary": "drop-me",
            },
            ResponseKind.SYMBOL_CONFIG,
            {
                "isAutoAddMargin": False,
                "leverage": 1,
                "marginType": "ISOLATED",
                "symbol": "ETHUSDT",
            },
        ),
    ],
)
def test_simple_read_allowlist_is_strict_typed_and_sanitized(
    path: str,
    payload: object,
    kind: ResponseKind,
    expected_fields: dict[str, object],
) -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path=path)
    transport = _test_transport(FakeSignedClient([json.dumps(payload).encode("ascii")]))

    result = _execute(transport, reservation)

    assert result.kind is kind
    actual_fields = dict(result.fields)
    if kind in {
        ResponseKind.SERVER_TIME,
        ResponseKind.BOOK_TICKER,
        ResponseKind.MARK_PRICE,
    }:
        for name in (
            "localMonotonicAfterNs",
            "localMonotonicBeforeNs",
            "localWallAfterMs",
            "localWallBeforeMs",
        ):
            assert type(actual_fields.pop(name)) is int
    assert actual_fields == expected_fields
    assert "drop-me" not in repr(result)


def test_time_freshness_result_digest_binds_exact_local_clock_bracket() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path="/fapi/v1/time")
    wall_values = iter((1_786_370_000_010, 1_786_370_000_014))
    monotonic_values = iter((8_000_000_000, 8_004_000_000))
    transport = _test_transport(
        FakeSignedClient([b'{"serverTime":1786370000012}']),
        wall_clock_ms=lambda: next(wall_values),
        monotonic_ns=lambda: next(monotonic_values),
    )

    result = _execute(transport, reservation)

    assert dict(result.fields) == {
        "localMonotonicAfterNs": 8_004_000_000,
        "localMonotonicBeforeNs": 8_000_000_000,
        "localWallAfterMs": 1_786_370_000_014,
        "localWallBeforeMs": 1_786_370_000_010,
        "serverTime": 1_786_370_000_012,
    }
    assert result == replace(result, result_sha256=result.result_sha256)
    with pytest.raises(CredentialTransportError, match="SANITIZED_RESULT_DIGEST_MISMATCH"):
        replace(
            result,
            fields=tuple(
                (name, value + 1 if name == "localWallAfterMs" else value)
                for name, value in result.fields
            ),
        )


def test_exchange_info_retains_exact_limit_and_market_filter_contract_only() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path="/fapi/v1/exchangeInfo")
    transport = _test_transport(
        FakeSignedClient([json.dumps(_exchange_info_payload()).encode("ascii")])
    )

    result = _execute(transport, reservation)

    assert result.kind is ResponseKind.EXCHANGE_INFO
    assert result.field("symbol") == "ETHUSDT"
    assert result.field("orderTypes") == ["LIMIT", "MARKET"]
    assert result.field("timeInForce") == ["GTC", "GTX"]
    assert result.field("filterTypeCounts") == {
        "LOT_SIZE": 1,
        "MARKET_LOT_SIZE": 1,
        "MIN_NOTIONAL": 1,
        "PERCENT_PRICE": 1,
        "PRICE_FILTER": 1,
    }
    assert result.field("limitLotSize") == {
        "maxQuantity": "100",
        "minQuantity": "0.001",
        "stepSize": "0.001",
    }
    assert result.field("marketLotSize") == {
        "maxQuantity": "50",
        "minQuantity": "0.001",
        "stepSize": "0.001",
    }
    assert result.field("priceFilter") == {
        "maxPrice": "5000",
        "minPrice": "1000",
        "tickSize": "0.01",
    }
    assert result.field("minNotional") == "5"
    assert result.field("percentPrice") == {
        "multiplierDown": "0.85",
        "multiplierUp": "1.05",
    }


def test_regular_and_algo_open_orders_retain_only_domain_hashed_ids_and_activity() -> None:
    guard = MutationRequestGuard(_intent())
    regular = _read_reservation(guard, path="/fapi/v1/openOrders", elapsed="2")
    guard.note_read_succeeded(regular)
    algo = _read_reservation(guard, path="/fapi/v1/openAlgoOrders", elapsed="3")
    transport = _test_transport(
        FakeSignedClient(
            [
                json.dumps(_regular_orders_payload()).encode("ascii"),
                json.dumps(_algo_orders_payload()).encode("ascii"),
            ]
        )
    )

    regular_result = _execute(transport, regular)
    algo_result = _execute(transport, algo)

    assert regular_result.kind is ResponseKind.OPEN_ORDERS
    regular_activity = regular_result.field("orders")
    assert isinstance(regular_activity, list)
    assert regular_activity == [
        {
            "clientOrderIdSha256": hashlib.sha256(
                b"binance-demo-client-order-id\0g1b16-owned-client"
            ).hexdigest(),
            "executedQty": "0.001",
            "orderIdSha256": hashlib.sha256(b"binance-demo-order-id\x00987654321").hexdigest(),
            "origQty": "0.003",
            "positionSide": "BOTH",
            "reduceOnly": False,
            "side": "BUY",
            "status": "PARTIALLY_FILLED",
            "symbol": "ETHUSDT",
            "type": "LIMIT",
        }
    ]
    assert algo_result.kind is ResponseKind.OPEN_ALGO_ORDERS
    algo_activity = algo_result.field("orders")
    assert isinstance(algo_activity, list)
    assert (
        algo_activity[0]["algoIdSha256"]
        == hashlib.sha256(b"binance-demo-algo-id\x00123456789").hexdigest()
    )
    assert (
        algo_activity[0]["clientAlgoIdSha256"]
        == hashlib.sha256(b"binance-demo-client-algo-id\0external-algo-client").hexdigest()
    )
    rendered = repr((regular_result, algo_result))
    assert "987654321" not in rendered
    assert "g1b16-owned-client" not in rendered
    assert "123456789" not in rendered
    assert "external-algo-client" not in rendered


def test_user_trades_retain_only_hashed_ids_quantity_commission_and_realized_pnl() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path="/fapi/v1/userTrades")
    transport = _test_transport(FakeSignedClient([json.dumps(_trades_payload()).encode("ascii")]))

    result = _execute(transport, reservation)

    assert result.kind is ResponseKind.USER_TRADES
    assert result.field("trades") == [
        {
            "commission": "0.00001",
            "orderIdSha256": hashlib.sha256(b"binance-demo-order-id\x00987654321").hexdigest(),
            "quantity": "0.001",
            "realizedPnl": "-0.25",
            "tradeIdSha256": hashlib.sha256(b"binance-demo-trade-id\x00112233").hexdigest(),
        }
    ]
    rendered = repr(result)
    assert "112233" not in rendered
    assert "987654321" not in rendered
    assert "must-not-be-retained" not in rendered


def test_account_retains_typed_permissions_balances_and_only_nonzero_positions() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path="/fapi/v2/account")
    transport = _test_transport(FakeSignedClient([json.dumps(_account_payload()).encode("ascii")]))

    result = _execute(transport, reservation)

    assert result.kind is ResponseKind.ACCOUNT
    assert result.field("canTrade") is True
    assert result.field("multiAssetsMargin") is False
    assert result.field("balances") == [
        {"asset": "BNB", "availableBalance": "0", "walletBalance": "0"},
        {
            "asset": "USDT",
            "availableBalance": "99.75",
            "walletBalance": "100.25",
        },
    ]
    assert result.field("nonzeroPositions") == [
        {"positionAmt": "0.001", "positionSide": "BOTH", "symbol": "ETHUSDT"}
    ]
    assert "accountAlias" not in repr(result)
    assert "accountIdentifier" not in repr(result)


def test_order_observation_retains_exact_owned_fields_but_not_venue_order_id() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    transport = _test_transport(
        FakeSignedClient([json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii")])
    )

    result = _execute(transport, reservation)

    assert result.kind is ResponseKind.ORDER_OBSERVATION
    assert dict(result.fields) == {
        "clientOrderId": guard.intent.client_order_id,
        "executedQty": "0",
        "orderIdSha256": hashlib.sha256(b"binance-demo-order-id\x00987654321").hexdigest(),
        "origQty": "0.005",
        "positionSide": "BOTH",
        "price": "1980",
        "reduceOnly": False,
        "side": "BUY",
        "status": "NEW",
        "symbol": "ETHUSDT",
        "timeInForce": "GTX",
        "type": "LIMIT",
    }
    assert "987654321" not in repr(result)


def test_result_digest_is_over_canonical_sanitized_payload_not_raw_json() -> None:
    guard = MutationRequestGuard(_intent())
    first = _read_reservation(guard, path="/fapi/v1/time", elapsed="2")
    guard.note_read_succeeded(first)
    second = _read_reservation(guard, path="/fapi/v1/time", elapsed="3")
    raw_one = b'{"serverTime":1786370000000,"ignored":"one"}'
    raw_two = b'{ "ignored" : "two", "serverTime" : 1786370000000 }'
    wall_values = iter((1_786_370_000_001, 1_786_370_000_002) * 2)
    monotonic_values = iter((9_000_000_001, 9_000_000_002) * 2)
    transport = _test_transport(
        FakeSignedClient([raw_one, raw_two]),
        wall_clock_ms=lambda: next(wall_values),
        monotonic_ns=lambda: next(monotonic_values),
    )

    first_result = _execute(transport, first)
    second_result = _execute(transport, second)

    assert first_result.result_sha256 == second_result.result_sha256
    assert first_result.result_sha256 != hashlib.sha256(raw_one).hexdigest()
    assert second_result.result_sha256 != hashlib.sha256(raw_two).hexdigest()
    assert "ignored" not in repr((first_result, second_result))


def test_repeated_path_executes_a_fresh_request_and_never_reuses_stale_cache() -> None:
    guard = MutationRequestGuard(_intent())
    first = _read_reservation(guard, path="/fapi/v1/openOrders", elapsed="2")
    guard.note_read_succeeded(first)
    second = _read_reservation(guard, path="/fapi/v1/openOrders", elapsed="3")
    client = FakeSignedClient([json.dumps(_regular_orders_payload()).encode("ascii"), b"[]"])
    transport = _test_transport(client)

    first_result = _execute(transport, first)
    second_result = _execute(transport, second)

    assert first_result.result_sha256 != second_result.result_sha256
    assert second_result.field("count") == 0
    assert len(client.calls) == 2


def test_pre_intent_read_executes_exact_session_bound_reservation() -> None:
    reservation = _pre_intent_read_reservation()
    client = FakeSignedClient([b'{"serverTime":1786370000000}'])
    transport = _test_transport(
        client,
        wall_clock_ms=iter((1786370000001, 1786370000002)).__next__,
        monotonic_ns=iter((9_000_000_001, 9_000_000_002)).__next__,
    )

    result = _execute_pre_intent(transport, reservation)

    assert result.request_sha256 == reservation.reservation_sha256
    assert result.logical_request_sha256 == reservation.logical_request_sha256
    assert result.kind is ResponseKind.SERVER_TIME
    assert client.calls == [(HttpMethod.GET, "/fapi/v1/time", {})]


def test_pre_intent_reservation_is_consumed_before_signed_io_failure() -> None:
    reservation = _pre_intent_read_reservation()
    client = FakeSignedClient([TimeoutError("raw-canary"), b"must-not-run"])
    transport = _test_transport(client)

    with pytest.raises(CredentialTransportError, match="READ_IO_AMBIGUOUS"):
        _execute_pre_intent(transport, reservation)
    with pytest.raises(CredentialTransportError, match="RESERVATION_ALREADY_EXECUTED"):
        _execute_pre_intent(transport, reservation)

    assert len(client.calls) == 1


def test_exact_reservation_is_single_use_even_when_called_twice() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    response = json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii")
    client = FakeSignedClient(
        [
            response,
            response,
        ]
    )
    transport = _test_transport(client)

    _execute(transport, reservation)
    with pytest.raises(CredentialTransportError, match="RESERVATION_ALREADY_EXECUTED"):
        _execute(transport, reservation)

    assert len(client.calls) == 1


def test_signed_call_failure_is_fixed_sanitized_post_dispatch_ambiguity() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = guard.reserve(
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=guard.intent.probe_payload,
        elapsed_seconds=Decimal("1.5"),
        retry_index=0,
    )
    canary = "credential-canary-must-not-cross"
    transport = _test_transport(FakeSignedClient([TimeoutError(canary)]))

    with pytest.raises(CredentialTransportError) as caught:
        _execute(transport, reservation)

    assert caught.value.reason == "POST_DISPATCH_IO_AMBIGUOUS"
    assert caught.value.post_dispatch is True
    assert canary not in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [b"", b"not-json", b"[]", b'{"status":"NEW"}'],
)
def test_post_dispatch_malformed_mutation_response_is_typed_failure_without_retry(
    raw: bytes,
) -> None:
    guard = MutationRequestGuard(_intent())
    reservation = guard.reserve(
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=guard.intent.probe_payload,
        elapsed_seconds=Decimal("1.5"),
        retry_index=0,
    )
    client = FakeSignedClient([raw])
    transport = _test_transport(client)

    with pytest.raises(
        CredentialTransportError,
        match="POST_DISPATCH_RESPONSE_INVALID",
    ):
        _execute(transport, reservation)

    assert len(client.calls) == 1


def test_mutation_ack_is_typed_and_retains_no_venue_order_identifier() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = guard.reserve(
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=guard.intent.probe_payload,
        elapsed_seconds=Decimal("1.5"),
        retry_index=0,
    )
    client = FakeSignedClient(
        [
            (
                b'{"status":"NEW","clientOrderId":"'
                + guard.intent.client_order_id.encode("ascii")
                + b'","orderId":987654321}'
            )
        ]
    )
    transport = _test_transport(client)

    result = _execute(transport, reservation)

    assert result.kind is ResponseKind.MUTATION_ACK
    assert result.field("status") == "NEW"
    assert result.field("clientOrderId") == guard.intent.client_order_id
    assert (
        result.field("orderIdSha256")
        == hashlib.sha256(b"binance-demo-order-id\x00987654321").hexdigest()
    )
    assert "987654321" not in repr(result)


def test_mutation_ack_must_echo_the_reserved_deterministic_client_id() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = guard.reserve(
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=guard.intent.probe_payload,
        elapsed_seconds=Decimal("1.5"),
        retry_index=0,
    )
    transport = _test_transport(FakeSignedClient([b'{"status":"NEW","clientOrderId":"wrong-id"}']))

    with pytest.raises(CredentialTransportError, match="RESPONSE_CLIENT_ID_MISMATCH"):
        _execute(transport, reservation)


def test_order_observation_must_match_the_queried_reconciliation_key() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    transport = _test_transport(
        FakeSignedClient([b'{"status":"NEW","executedQty":"0","clientOrderId":"wrong-id"}'])
    )

    with pytest.raises(CredentialTransportError, match="RESPONSE_CLIENT_ID_MISMATCH"):
        _execute(transport, reservation)


def test_order_observation_hash_binds_relevant_trade_to_the_probe_order() -> None:
    guard = MutationRequestGuard(_intent())
    order_reservation = _read_reservation(guard, elapsed="2")
    transport = _test_transport(
        FakeSignedClient(
            [
                json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii"),
                json.dumps(_trades_payload()).encode("ascii"),
            ]
        )
    )

    order_result = _execute(transport, order_reservation)
    guard.note_read_succeeded(order_reservation)
    trades_reservation = _read_reservation(
        guard,
        path="/fapi/v1/userTrades",
        elapsed="3",
    )
    trades_result = _execute(transport, trades_reservation)

    order_id_sha256 = order_result.field("orderIdSha256")
    assert order_id_sha256 == hashlib.sha256(b"binance-demo-order-id\x00987654321").hexdigest()
    trades = trades_result.field("trades")
    assert isinstance(trades, list)
    assert trades[0]["orderIdSha256"] == order_id_sha256
    assert "987654321" not in repr((order_result, trades_result))


def test_response_fields_are_fixed_allowlisted_and_cannot_carry_a_canary() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    canary = "credential-canary-must-not-become-status"
    transport = _test_transport(
        FakeSignedClient(
            [
                (
                    '{"status":"'
                    + canary
                    + '","executedQty":"0","clientOrderId":"'
                    + guard.intent.client_order_id
                    + '"}'
                ).encode("ascii")
            ]
        )
    )

    with pytest.raises(CredentialTransportError) as caught:
        _execute(transport, reservation)

    assert caught.value.reason == "READ_RESPONSE_INVALID"
    assert canary not in str(caught.value)


def test_wrong_origin_fails_before_signed_client_is_called() -> None:
    client = FakeSignedClient([], base_url="https://fapi.binance.com")

    with pytest.raises(CredentialTransportError, match="DEMO_HTTP_ORIGIN_MISMATCH"):
        _test_transport(client)

    assert client.calls == []


def test_method_purpose_path_mismatch_fails_before_signed_client_is_called() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = replace(_read_reservation(guard), method="DELETE")
    client = FakeSignedClient([b"must-not-be-consumed"])
    transport = _test_transport(client)

    with pytest.raises(CredentialTransportError, match="REQUEST_CONTRACT_MISMATCH"):
        _execute(transport, reservation)

    assert client.calls == []


def test_transport_uses_no_executor_thread_or_async_timeout_as_safety_owner() -> None:
    source = inspect.getsource(ProcessBoundCredentialTransport)

    assert "ThreadPoolExecutor" not in source
    assert "create_task" not in source
    assert "wait_for" not in source
    assert "Future" not in source


def test_exact_absolute_deadline_is_required_and_forwarded_to_the_signed_leaf() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    client = FakeSignedClient(
        [json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii")]
    )
    transport = _test_transport(client, monotonic_ns=lambda: 10)

    transport.execute(reservation, absolute_deadline_ns=20)

    assert client.deadlines == [20]


def test_http_leaf_derives_socket_and_watchdog_timeout_from_the_same_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RedirectSafeHttpClient(timeout_secs=5.0)
    observed_timeouts: list[float] = []

    def fake_open(_method, _url, _headers, _body, timeout):
        observed_timeouts.append(timeout)
        return SimpleNamespace(status=200, headers={}, body=b"{}")

    monkeypatch.setattr(client, "_open_sync", fake_open)

    async def invoke() -> None:
        absolute_deadline_ns = time.monotonic_ns() + 500_000_000
        client.authorize_absolute_deadline(absolute_deadline_ns)
        await client.request(
            "GET",
            "http://127.0.0.1/never-contacted",
            headers={},
            timeout_secs=5.0,
        )

    try:
        asyncio.run(invoke())
    finally:
        client.close()

    assert len(observed_timeouts) == 1
    assert 0 < observed_timeouts[0] <= 0.5


def test_http_leaf_rejects_io_without_a_supervisor_derived_deadline() -> None:
    client = _RedirectSafeHttpClient()

    async def invoke() -> None:
        with pytest.raises(CredentialHttpError, match="DEMO_HTTP_DEADLINE_EXHAUSTED"):
            await client.request("GET", "http://127.0.0.1/never-contacted", headers={})

    try:
        asyncio.run(invoke())
    finally:
        client.close()


def test_execute_rejects_running_event_loop_instead_of_spawning_background_work() -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    transport = _test_transport(
        FakeSignedClient(
            [
                (
                    '{"status":"NEW","executedQty":"0","clientOrderId":"'
                    + guard.intent.client_order_id
                    + '"}'
                ).encode("ascii")
            ]
        )
    )

    async def invoke() -> None:
        with pytest.raises(CredentialTransportError, match="TRANSPORT_EVENT_LOOP_REENTRANCY"):
            _execute(transport, reservation)

    asyncio.run(invoke())


class _ExistingStackClient:
    def __init__(
        self,
        response: bytes | BaseException,
        *,
        base_url: str = DEMO_HTTP_ORIGIN,
    ) -> None:
        self.base_url = base_url
        self._client: object = object()
        self.response = response
        self.calls: list[tuple[HttpMethod, str, dict[str, str] | None]] = []

    async def sign_request(
        self,
        http_method: HttpMethod,
        url_path: str,
        payload: dict[str, str] | None = None,
        ratelimiter_keys: list[str] | None = None,
    ) -> bytes:
        self.calls.append((http_method, url_path, payload))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_production_factory_reuses_existing_signing_redirect_and_timeout_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import global_quant.gate1b.credential_transport as transport_module

    credentials = DemoCredentials(
        api_key="production-factory-api-key-canary",
        api_secret="production-factory-secret-canary",
    )
    signed_client = _ExistingStackClient(b'{"serverTime":1786370000000}')
    built_with: list[dict[str, object]] = []

    def fake_get_cached_binance_http_client(**kwargs: object) -> object:
        built_with.append(kwargs)
        return signed_client

    close_calls: list[_RedirectSafeHttpClient] = []
    real_close = _RedirectSafeHttpClient.close

    def record_close(client: _RedirectSafeHttpClient) -> None:
        close_calls.append(client)
        real_close(client)

    monkeypatch.setattr(
        transport_module,
        "get_cached_binance_http_client",
        fake_get_cached_binance_http_client,
    )
    monkeypatch.setattr(_RedirectSafeHttpClient, "close", record_close)
    io_authority = _test_io_authority()
    deadline = io_authority._bootstrap.hard_deadline
    checks_before_execute = deadline.checks

    transport = build_production_credential_transport(
        credentials,
        io_authority=io_authority,
    )

    assert len(built_with) == 1
    assert built_with[0]["api_key"] == credentials.api_key
    assert built_with[0]["api_secret"] == credentials.api_secret
    assert built_with[0]["environment"] is BinanceEnvironment.DEMO
    assert isinstance(signed_client._client, _RedirectSafeHttpClient)
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard, path="/fapi/v1/time")
    result = _execute(transport, reservation)
    assert result.kind is ResponseKind.SERVER_TIME
    assert deadline.checks > checks_before_execute
    assert signed_client.calls == [
        (HttpMethod.GET, "/fapi/v1/time", None),
    ]

    transport.close()
    transport.close()
    assert close_calls == [signed_client._client]


def test_production_stack_exact_order_not_found_is_typed_without_raw_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import global_quant.gate1b.credential_transport as transport_module

    raw_canary = "must-never-cross-production-transport"
    signed_client = _ExistingStackClient(
        BinanceClientError(
            status=404,
            message={"code": -2013, "msg": "Order does not exist."},
            headers={"raw-canary": raw_canary},
        )
    )
    monkeypatch.setattr(
        transport_module,
        "get_cached_binance_http_client",
        lambda **_kwargs: signed_client,
    )
    transport = build_production_credential_transport(
        DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
        io_authority=_test_io_authority(),
    )
    guard = MutationRequestGuard(_intent())

    result = _execute(transport, _read_reservation(guard))

    assert result.kind is ResponseKind.ORDER_NOT_FOUND
    assert result.field("venueCode") == -2013
    assert raw_canary not in repr(result)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (500, {"code": -2013, "msg": "Order does not exist."}),
        (404, {"code": -2013, "msg": "Order does not exist"}),
        (404, {"code": -2013, "msg": "Order does not exist.", "extra": "x"}),
        (404, {"code": -1000, "msg": "Order does not exist."}),
    ],
)
def test_production_stack_non_exact_order_error_remains_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    message: dict[str, object],
) -> None:
    import global_quant.gate1b.credential_transport as transport_module

    signed_client = _ExistingStackClient(
        BinanceClientError(status=status, message=message, headers={})
    )
    monkeypatch.setattr(
        transport_module,
        "get_cached_binance_http_client",
        lambda **_kwargs: signed_client,
    )
    transport = build_production_credential_transport(
        DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
        io_authority=_test_io_authority(),
    )
    guard = MutationRequestGuard(_intent())

    with pytest.raises(CredentialTransportError) as caught:
        _execute(transport, _read_reservation(guard))

    assert caught.value.reason == "READ_IO_AMBIGUOUS"
    assert caught.value.post_dispatch is True


def test_production_factory_has_no_direct_http_or_test_client_path() -> None:
    import global_quant.gate1b.credential_transport as transport_module

    source = inspect.getsource(build_production_credential_transport)

    assert "get_cached_binance_http_client" in source
    assert "DemoLifecycleTransport" not in source
    assert "mutation_runner" not in source
    assert "TestOnlyDirectDemoSignedClient" not in source
    assert "DirectDemoSignedClient" not in source
    assert "http.client" not in source
    assert not hasattr(transport_module, "TestOnlyDirectDemoSignedClient")


def test_production_factory_rejects_callback_authority_before_stack_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import global_quant.gate1b.credential_transport as transport_module

    stack_builds: list[dict[str, object]] = []
    monkeypatch.setattr(
        transport_module,
        "get_cached_binance_http_client",
        lambda **kwargs: stack_builds.append(kwargs),
    )

    with pytest.raises(TypeError):
        build_production_credential_transport(
            DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
            assert_io_authorized=lambda: None,
        )

    assert stack_builds == []


def test_transport_ordinary_constructor_cannot_bind_arbitrary_signer() -> None:
    client = FakeSignedClient([b"{}"])

    with pytest.raises(TypeError):
        ProcessBoundCredentialTransport(client)  # type: ignore[call-arg]

    assert client.calls == []


@pytest.mark.parametrize("fake_authority", [None, False, {}, lambda: None])
def test_production_factory_rejects_fake_typed_authority_before_stack_build(
    monkeypatch: pytest.MonkeyPatch,
    fake_authority: object,
) -> None:
    stack_builds: list[dict[str, object]] = []
    monkeypatch.setattr(
        credential_transport_module,
        "get_cached_binance_http_client",
        lambda **kwargs: stack_builds.append(kwargs),
    )

    with pytest.raises(CredentialTransportError, match="PRODUCTION_CHILD_IO_AUTHORITY_REQUIRED"):
        build_production_credential_transport(
            DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
            io_authority=fake_authority,  # type: ignore[arg-type]
        )

    assert stack_builds == []


def test_wrong_pid_authority_fails_before_stack_build_or_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _test_io_authority()
    original_pid = os.getpid()
    stack_builds: list[dict[str, object]] = []
    monkeypatch.setattr(process_boundary_module.os, "getpid", lambda: original_pid + 1)
    monkeypatch.setattr(
        credential_transport_module,
        "get_cached_binance_http_client",
        lambda **kwargs: stack_builds.append(kwargs),
    )

    with pytest.raises(
        process_boundary_module.CredentialBoundaryError,
        match="CHILD_IO_AUTHORITY_PROCESS_CHANGED",
    ):
        build_production_credential_transport(
            DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
            io_authority=authority,
        )

    assert stack_builds == []


def test_authority_cannot_be_reused_for_a_second_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _test_io_authority()
    stack_builds: list[dict[str, object]] = []

    def build_stack(**kwargs: object) -> object:
        stack_builds.append(kwargs)
        return _ExistingStackClient(b'{"serverTime":1786370000000}')

    monkeypatch.setattr(credential_transport_module, "get_cached_binance_http_client", build_stack)
    credentials = DemoCredentials(api_key="api-key-canary", api_secret="secret-canary")
    first = build_production_credential_transport(credentials, io_authority=authority)
    try:
        with pytest.raises(
            process_boundary_module.CredentialBoundaryError,
            match="CHILD_IO_AUTHORITY_ALREADY_BOUND",
        ):
            build_production_credential_transport(credentials, io_authority=authority)
    finally:
        first.close()

    assert len(stack_builds) == 1
    assert stack_builds[0]["api_key"] == credentials.api_key
    assert stack_builds[0]["api_secret"] == credentials.api_secret


@pytest.mark.parametrize("tamper", ["guard", "deadline"])
def test_transport_revalidates_child_authority_before_each_io(tamper: str) -> None:
    guard = MutationRequestGuard(_intent())
    reservation = _read_reservation(guard)
    client = FakeSignedClient(
        [json.dumps(_order_payload(guard.intent.client_order_id)).encode("ascii")]
    )
    transport = _test_transport(client)
    authority = transport._io_authority
    if tamper == "guard":
        authority._bootstrap._network_gate.ready = False
    else:
        authority._bootstrap.hard_deadline.intact = False

    with pytest.raises(process_boundary_module.ProcessBoundaryError):
        _execute(transport, reservation)

    assert client.calls == []


def test_obsolete_mutation_owners_are_removed() -> None:
    assert importlib.util.find_spec("global_quant.gate1b.credential_http") is not None
    assert importlib.util.find_spec("global_quant.gate1b.demo_transport") is None
    assert importlib.util.find_spec("global_quant.gate1b.mutation_runner") is None


def test_http_leaf_has_no_thread_drain_quiescence_contract() -> None:
    credential_http = importlib.import_module("global_quant.gate1b.credential_http")
    source = inspect.getsource(credential_http)

    assert "def drain(" not in source
    assert "pending_count" not in source
    assert "BLOCKED_MUTATION_TIMEOUT_DRAIN_UNCONVERGED" not in source
    assert "kill + exact-reap" in source


def test_importing_credential_boundary_does_not_load_legacy_mutation_runner() -> None:
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    script = "\n".join(
        (
            "import sys",
            f"sys.path.insert(0, {source_root!r})",
            "import global_quant.gate1b.credential_transport",
            "import global_quant.gate1b.credential_session",
            "assert 'global_quant.gate1b.mutation_runner' not in sys.modules",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_factory_rejects_non_demo_base_before_redirect_or_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import global_quant.gate1b.credential_transport as transport_module

    signed_client = _ExistingStackClient(
        b"must-not-be-consumed",
        base_url="https://fapi.binance.com",
    )
    monkeypatch.setattr(
        transport_module,
        "get_cached_binance_http_client",
        lambda **_kwargs: signed_client,
    )

    with pytest.raises(CredentialTransportError, match="DEMO_HTTP_ORIGIN_MISMATCH"):
        build_production_credential_transport(
            DemoCredentials(api_key="api-key-canary", api_secret="secret-canary"),
            io_authority=_test_io_authority(),
        )

    assert signed_client.calls == []
    assert not isinstance(signed_client._client, _RedirectSafeHttpClient)
