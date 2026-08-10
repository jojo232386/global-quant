"""Tests for the real Demo HTTP adapter (``demo_transport.py``).

All tests use a fake signed HTTP client returning fixture bytes; no test ever
contacts Binance or reads a real credential. Coverage per task section F:

* Demo origin only / production URL rejected;
* proxy/redirect cannot escape (the adapter never constructs a proxy or follows
  a redirect; any non-Demo base_url is a hard STOP with ``production_contacted``);
* 5 s timeout fails closed;
* mutation retry = 0 (exactly one signed POST per create);
* malformed / missing-field responses fail closed;
* happy-path parse of account/symbol/filters/book/mark;
* per-path cache keeps duplicate fetch/read within the HTTP budget.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

import global_quant.gate1b.demo_transport as dt
from global_quant.gate1b.demo_transport import (
    DemoLifecycleTransport,
    _build_account_state,
)
from global_quant.gate1b.mutation_runner import MutationRunnerError

_DEMO = "https://demo-fapi.binance.com"
_PRODUCTION = "https://fapi.binance.com"


def _account_payload() -> dict:
    return {
        "canTrade": True,
        "multiAssetsMargin": False,
        "assets": [{"asset": "USDT", "walletBalance": "100", "availableBalance": "100"}],
        "positions": [],
    }


def _dual_payload() -> dict:
    return {"dualSidePosition": False}


def _symbol_config_payload() -> dict:
    return {"symbol": "ETHUSDT", "marginType": "ISOLATED", "leverage": 1, "isAutoAddMargin": False}


def _exchange_info_payload() -> dict:
    filters = [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "1000.00",
            "maxPrice": "5000.00",
            "tickSize": "0.01",
        },
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100.000", "stepSize": "0.001"},
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.001",
            "maxQty": "100.000",
            "stepSize": "0.001",
        },
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.85"},
    ]
    return {
        "symbols": [
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "timeInForce": ["GTX"],
                "filters": filters,
            }
        ]
    }


def _book_payload() -> dict:
    return {"symbol": "ETHUSDT", "bidPrice": "2500.00", "askPrice": "2500.01"}


def _mark_payload() -> dict:
    return {"symbol": "ETHUSDT", "markPrice": "2500.00"}


def _responses(**overrides: dict) -> dict[str, bytes]:
    base = {
        "/fapi/v2/account": json.dumps(_account_payload()).encode(),
        "/fapi/v1/positionSide/dual": json.dumps(_dual_payload()).encode(),
        "/fapi/v1/symbolConfig": json.dumps(_symbol_config_payload()).encode(),
        "/fapi/v1/openOrders": b"[]",
        "/fapi/v1/openAlgoOrders": b"[]",
        "/fapi/v1/exchangeInfo": json.dumps(_exchange_info_payload()).encode(),
        "/fapi/v1/ticker/bookTicker": json.dumps(_book_payload()).encode(),
        "/fapi/v1/premiumIndex": json.dumps(_mark_payload()).encode(),
    }
    base.update(overrides)
    return base


class FakeClient:
    """Fake signed HTTP client returning fixture bytes, counting calls."""

    def __init__(self, responses: dict[str, bytes], base_url: str = _DEMO) -> None:
        self.base_url = base_url
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.hang = False

    async def sign_request(self, http_method, url_path, payload=None, ratelimiter_keys=None):
        if self.hang:
            await asyncio.sleep(30)
        self.calls.append((http_method, url_path, dict(payload or {})))
        return self.responses.get(url_path, b"{}")


def _transport(
    responses: dict[str, bytes] | None = None, base_url: str = _DEMO
) -> DemoLifecycleTransport:
    client = FakeClient(responses or _responses(), base_url=base_url)
    return DemoLifecycleTransport(http_client=client), client


class TestEndpointIsolation:
    def test_demo_origin_only_accepted(self) -> None:
        transport, _ = _transport()
        assert transport.production_contacted is False
        assert transport._contacted_origins == [_DEMO]

    def test_production_origin_rejected_fail_closed(self) -> None:
        with pytest.raises(MutationRunnerError) as exc:
            _transport(base_url=_PRODUCTION)
        assert "DEMO_HTTP_ORIGIN_MISMATCH" in str(exc.value)

    def test_testnet_origin_rejected(self) -> None:
        with pytest.raises(MutationRunnerError):
            _transport(base_url="https://testnet.binancefuture.com")

    def test_no_proxy_or_redirect_capability(self) -> None:
        # The adapter owns no HTTP stack: it passes only the frozen path to the
        # pinned client. Any request must target the exact Demo origin + path.
        transport, client = _transport()
        transport.fetch_account_state()
        for _method, path, _params in client.calls:
            assert path in {
                "/fapi/v2/account",
                "/fapi/v1/positionSide/dual",
                "/fapi/v1/symbolConfig",
                "/fapi/v1/openOrders",
                "/fapi/v1/openAlgoOrders",
            }


class TestParsing:
    def test_account_state_parses_clean(self) -> None:
        transport, _client = _transport()
        state = transport.fetch_account_state()
        assert state.can_trade is True
        assert state.dual_side_position is False
        assert state.multi_assets_margin is False
        assert state.margin_type == "ISOLATED"
        assert state.leverage == 1
        assert state.auto_add_margin is False
        assert state.wallet_balance == Decimal("100")
        assert state.available_balance == Decimal("100")
        assert state.nonzero_positions == ()
        assert state.open_regular_order_ids == ()
        assert state.open_algo_order_ids == ()

    def test_symbol_state_and_filters_parse(self) -> None:
        transport, _client = _transport()
        symbol = transport.fetch_symbol_state()
        filters = transport.fetch_filters()
        assert symbol.symbol == "ETHUSDT"
        assert symbol.status == "TRADING"
        assert "GTX" in symbol.time_in_force
        assert filters.min_price == Decimal("1000.00")
        assert filters.tick_size == Decimal("0.01")
        assert filters.min_notional == Decimal("5")
        assert filters.percent_price_multiplier_up == Decimal("1.05")

    def test_book_and_mark_parse(self) -> None:
        transport, _client = _transport()
        bid, ask = transport.fetch_book()
        mark = transport.fetch_mark()
        assert bid == Decimal("2500.00")
        assert ask == Decimal("2500.01")
        assert mark == Decimal("2500.00")

    def test_missing_required_fields_fails_closed(self) -> None:
        payload = dict(_account_payload())
        del payload["multiAssetsMargin"]
        transport, _client = _transport({"/fapi/v2/account": json.dumps(payload).encode()})
        with pytest.raises(MutationRunnerError):
            transport.fetch_account_state()

    def test_malformed_json_fails_closed(self) -> None:
        transport, _client = _transport({"/fapi/v2/account": b"not-json{{{"})
        with pytest.raises(MutationRunnerError) as exc:
            transport.fetch_account_state()
        assert "MALFORMED_RESPONSE" in str(exc.value)

    def test_non_positive_book_fails_closed(self) -> None:
        transport, _client = _transport(
            {"/fapi/v1/ticker/bookTicker": b'{"bidPrice":"0","askPrice":"1"}'}
        )
        with pytest.raises(MutationRunnerError):
            transport.fetch_book()


class TestTimeoutAndRetry:
    def test_timeout_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 0.1)
        client = FakeClient(_responses())
        client.hang = True
        transport = DemoLifecycleTransport(http_client=client)
        with pytest.raises(MutationRunnerError) as exc:
            transport.fetch_book()
        assert "DEMO_HTTP_TIMEOUT" in str(exc.value)

    def test_create_sends_exactly_one_request(self) -> None:
        from nautilus_trader.core.nautilus_pyo3 import HttpMethod

        transport, client = _transport()
        client.responses["/fapi/v1/order"] = (
            b'{"orderId":"1","status":"NEW","clientOrderId":"g1b16-aaaaaaaaaaaaaaaa-01"}'
        )
        ack = transport.send_create(
            _reservation(path="/fapi/v1/order", params={"symbol": "ETHUSDT"})
        )
        assert ack["status"] == "NEW"
        post_calls = [c for c in client.calls if c[0] == HttpMethod.POST]
        assert len(post_calls) == 1  # mutation retry = 0

    def test_malformed_create_ack_fails_closed(self) -> None:
        transport, client = _transport()
        client.responses["/fapi/v1/order"] = b'{"orderId":"1"}'
        with pytest.raises(MutationRunnerError):
            transport.send_create(_reservation(path="/fapi/v1/order", params={}))


class TestCacheAndFinalState:
    def test_fetch_then_read_does_not_duplicate_request(self) -> None:
        transport, client = _transport()
        transport.fetch_account_state()
        account_calls = [c for c in client.calls if c[1] == "/fapi/v2/account"]
        assert len(account_calls) == 1
        # read() of the same path must hit the cache, not re-request.
        transport.read(_reservation(path="/fapi/v2/account", params={}))
        account_calls = [c for c in client.calls if c[1] == "/fapi/v2/account"]
        assert len(account_calls) == 1

    def test_final_state_clean(self) -> None:
        transport, _client = _transport()
        final = transport.fetch_final_state()
        assert final["nonzero_positions"] == ()
        assert final["open_regular_orders"] == 0
        assert final["open_algo_orders"] == 0
        assert final["account_config_matches"] is True

    def test_final_state_detects_open_order(self) -> None:
        transport, _client = _transport(
            _responses(**{"/fapi/v1/openOrders": b'[{"orderId":"99"}]'})
        )
        final = transport.fetch_final_state()
        assert final["open_regular_orders"] == 1


class TestBuildAccountState:
    def test_nonzero_position_detected(self) -> None:
        payload = dict(_account_payload())
        payload["positions"] = [{"symbol": "ETHUSDT", "positionAmt": "0.001"}]
        state = _build_account_state(
            account=payload,
            dual=_dual_payload(),
            symbol_config=_symbol_config_payload(),
            regular_orders=[],
            algo_orders=[],
        )
        assert state.nonzero_positions == (("ETHUSDT", Decimal("0.001")),)

    def test_usdt_wallet_required(self) -> None:
        payload = dict(_account_payload())
        payload["assets"] = [{"asset": "BTC", "walletBalance": "1", "availableBalance": "1"}]
        with pytest.raises(MutationRunnerError):
            _build_account_state(
                account=payload,
                dual=_dual_payload(),
                symbol_config=_symbol_config_payload(),
                regular_orders=[],
                algo_orders=[],
            )


def _reservation(*, path: str, params: dict[str, str]):
    reservation = _SimpleReservation()
    reservation.path = path
    reservation.parameters = params
    reservation.request_sha256 = "0" * 64
    return reservation


class _SimpleReservation:
    pass
