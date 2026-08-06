from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.adapters.binance.futures.http.account import (
    BinanceFuturesAccountHttpAPI,
)
from nautilus_trader.adapters.binance.futures.http.market import BinanceFuturesMarketHttpAPI
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.common.component import LiveClock

from global_quant.gate1b.preflight import AccountPreflight
from global_quant.gate1b.preflight import PreflightResult
from global_quant.gate1b.safety import DemoCredentials
from global_quant.gate1b.safety import resolve_demo_endpoints
from global_quant.gate1b.safety import validate_demo_endpoints


@dataclass(frozen=True)
class DemoHttpApis:
    client: BinanceHttpClient
    account: BinanceFuturesAccountHttpAPI
    market: BinanceFuturesMarketHttpAPI


def build_demo_http_apis(credentials: DemoCredentials) -> DemoHttpApis:
    endpoints = resolve_demo_endpoints()
    validate_demo_endpoints(endpoints)
    account_type = BinanceAccountType.USDT_FUTURES
    client = get_cached_binance_http_client(
        clock=LiveClock(),
        account_type=account_type,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        base_url=None,
        environment=BinanceEnvironment.DEMO,
        is_us=False,
        proxy_url=None,
    )
    if client.base_url != endpoints.http:
        raise RuntimeError("DEMO_HTTP_ENDPOINT_MISMATCH")
    return DemoHttpApis(
        client=client,
        account=BinanceFuturesAccountHttpAPI(client, account_type),
        market=BinanceFuturesMarketHttpAPI(client, account_type),
    )


async def collect_account_preflight(
    *,
    account_api: Any,
    market_api: Any,
    local_time_ms: Callable[[], int] | None = None,
) -> AccountPreflight:
    clock = local_time_ms or (lambda: time.time_ns() // 1_000_000)
    local_before = int(clock())
    server_time = int(await market_api.request_server_time())
    local_after = int(clock())
    local_midpoint = local_before + (local_after - local_before) // 2

    account = await account_api.query_futures_account_info()
    hedge_mode = await account_api.query_futures_hedge_mode()
    positions = await account_api.query_futures_position_risk()
    regular_orders = await account_api.query_open_orders()
    algo_orders = await account_api.query_open_algo_orders()
    exchange_info = await market_api.query_futures_exchange_info()

    wallet_balance = Decimal("0")
    for asset in account.assets:
        if asset.asset == "USDT":
            wallet_balance = Decimal(asset.walletBalance)
            break

    nonzero_positions = tuple(
        sorted(
            (position.symbol, Decimal(position.positionAmt))
            for position in positions
            if Decimal(position.positionAmt) != 0
        ),
    )
    trading_instruments = frozenset(
        symbol.symbol
        for symbol in exchange_info.symbols
        if _enum_value(symbol.status) == "TRADING"
    )
    return AccountPreflight(
        can_trade=bool(account.canTrade),
        dual_side_position=bool(hedge_mode.dualSidePosition),
        wallet_balance=wallet_balance,
        nonzero_positions=nonzero_positions,
        open_regular_order_ids=tuple(sorted(str(order.orderId) for order in regular_orders)),
        open_algo_order_ids=tuple(sorted(str(order.algoId) for order in algo_orders)),
        server_time_skew_ms=server_time - local_midpoint,
        trading_instruments=trading_instruments,
    )


def sanitized_preflight_evidence(
    *,
    snapshot: AccountPreflight,
    result: PreflightResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "reason_codes": list(result.reason_codes),
        "automated_cleanup_allowed": result.automated_cleanup_allowed,
        "can_trade": snapshot.can_trade,
        "dual_side_position": snapshot.dual_side_position,
        "wallet_balance": str(snapshot.wallet_balance),
        "nonzero_positions": [
            {"symbol": symbol, "quantity": str(quantity)}
            for symbol, quantity in snapshot.nonzero_positions
        ],
        "open_regular_order_ids": list(snapshot.open_regular_order_ids),
        "open_algo_order_ids": list(snapshot.open_algo_order_ids),
        "server_time_skew_ms": snapshot.server_time_skew_ms,
        "trading_instruments": sorted(snapshot.trading_instruments),
    }


def _enum_value(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value)
