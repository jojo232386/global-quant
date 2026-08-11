from __future__ import annotations

import asyncio
import inspect
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import global_quant.gate1b.demo_preflight as demo_preflight_module
from global_quant.gate1b.demo_preflight import (
    collect_account_preflight,
    run_signed_preflight,
    sanitized_preflight_evidence,
)
from global_quant.gate1b.preflight import evaluate_account_preflight
from global_quant.gate1b.safety import DemoCredentials


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


def test_legacy_public_signed_preflight_fails_closed_without_network() -> None:
    with pytest.raises(RuntimeError, match="PROCESS_BOUND_CREDENTIAL_SESSION_REQUIRED"):
        asyncio.run(
            run_signed_preflight(
                DemoCredentials(
                    api_key="demo-key-test-only",
                    api_secret="demo-secret-test-only",
                )
            )
        )


def test_public_preflight_module_exposes_no_raw_signed_account_api_builder() -> None:
    source = inspect.getsource(demo_preflight_module)

    assert not hasattr(demo_preflight_module, "DemoHttpApis")
    assert not hasattr(demo_preflight_module, "build_demo_http_apis")
    assert "BinanceFuturesAccountHttpAPI" not in source
    assert "new_order" not in source
    assert "cancel_order" not in source


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
