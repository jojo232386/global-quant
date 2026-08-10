from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

from global_quant.gate1b.demo_preflight import (
    build_demo_http_apis,
    collect_account_preflight,
    sanitized_preflight_evidence,
)
from global_quant.gate1b.preflight import evaluate_account_preflight
from global_quant.gate1b.safety import EXPECTED_DEMO_ENDPOINTS, DemoCredentials


class FakeAccountApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def query_futures_account_info(self):
        self.calls.append("account")
        return SimpleNamespace(
            canTrade=True,
            assets=[
                SimpleNamespace(asset="BNB", walletBalance="12"),
                SimpleNamespace(asset="USDT", walletBalance="10000.25"),
            ],
        )

    async def query_futures_hedge_mode(self):
        self.calls.append("hedge")
        return SimpleNamespace(dualSidePosition=False)

    async def query_futures_position_risk(self):
        self.calls.append("positions")
        return [
            SimpleNamespace(symbol="BTCUSDT", positionAmt="0"),
            SimpleNamespace(symbol="ETHUSDT", positionAmt="-0.015"),
        ]

    async def query_open_orders(self):
        self.calls.append("regular_orders")
        return [SimpleNamespace(orderId=101)]

    async def query_open_algo_orders(self):
        self.calls.append("algo_orders")
        return [SimpleNamespace(algoId=202)]


class FakeMarketApi:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request_server_time(self):
        self.calls.append("time")
        return 1_050

    async def query_futures_exchange_info(self):
        self.calls.append("exchange_info")
        return SimpleNamespace(
            symbols=[
                SimpleNamespace(symbol="BTCUSDT", status="TRADING"),
                SimpleNamespace(
                    symbol="ETHUSDT",
                    status=SimpleNamespace(value="TRADING"),
                ),
                SimpleNamespace(symbol="SOLUSDT", status="TRADING_HALT"),
            ],
        )


def test_collect_account_preflight_queries_every_risk_surface() -> None:
    account = FakeAccountApi()
    market = FakeMarketApi()
    times = iter((1_000, 1_100))

    snapshot = asyncio.run(
        collect_account_preflight(
            account_api=account,
            market_api=market,
            local_time_ms=lambda: next(times),
        ),
    )

    assert account.calls == [
        "account",
        "hedge",
        "positions",
        "regular_orders",
        "algo_orders",
    ]
    assert market.calls == ["time", "exchange_info"]
    assert snapshot.wallet_balance == Decimal("10000.25")
    assert snapshot.nonzero_positions == (("ETHUSDT", Decimal("-0.015")),)
    assert snapshot.open_regular_order_ids == ("101",)
    assert snapshot.open_algo_order_ids == ("202",)
    assert snapshot.server_time_skew_ms == 0
    assert snapshot.trading_instruments == frozenset({"BTCUSDT", "ETHUSDT"})


def test_http_apis_are_pinned_to_demo_usdt_futures() -> None:
    apis = build_demo_http_apis(
        DemoCredentials(api_key="demo-key-test-only", api_secret="demo-secret-test-only"),
    )

    assert apis.client.base_url == EXPECTED_DEMO_ENDPOINTS.http
    assert apis.account.client is apis.client
    assert apis.market.client is apis.client
    assert apis.account._timestamp().isdigit()


def test_sanitized_evidence_cannot_contain_demo_credentials() -> None:
    credentials = DemoCredentials(
        api_key="sensitive-demo-key",
        api_secret="sensitive-demo-secret",
    )
    clean = FakeAccountApi()
    clean.query_futures_position_risk = lambda: _async_value([])
    clean.query_open_orders = lambda: _async_value([])
    clean.query_open_algo_orders = lambda: _async_value([])
    times = iter((1_000, 1_100))
    snapshot = asyncio.run(
        collect_account_preflight(
            account_api=clean,
            market_api=FakeMarketApi(),
            local_time_ms=lambda: next(times),
        ),
    )
    result = evaluate_account_preflight(snapshot)

    payload = sanitized_preflight_evidence(snapshot=snapshot, result=result)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "PASS"
    assert payload["automated_cleanup_allowed"] is False
    assert credentials.api_key not in encoded
    assert credentials.api_secret not in encoded


async def _async_value(value):
    return value
