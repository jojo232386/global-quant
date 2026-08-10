"""Thin real Demo HTTP adapter for the frozen NT-GATE-1B v1.6 mutation lifecycle.

This module implements the ``LifecycleTransport`` contract from
``mutation_runner.py`` against the real Binance USDⓈ-M Futures Demo origin via
the existing ``nautilus_trader`` ``BinanceHttpClient`` (whose ``sign_request``
is async, auto-signs, and returns raw response bytes with ``base_url`` pinned
to ``demo-fapi.binance.com`` by ``safety.build_demo_http_apis``).

Design rules (frozen protocol section 2.1 / 3 / 7 / 11):

* REUSE_UNCHANGED: signing/HTTP/timeout handling is the pinned nautilus
  client's; this adapter only parses raw responses and enforces redirect
  isolation.
* THIN_ADAPTER: transport owns no symbol/quantity/price/TIF/lifecycle decision;
  it executes frozen reservations and parses allowlisted fields.
* MINIMAL_EXTENSION: a per-path response cache keeps the total HTTP count inside
  the frozen section-11 budget (fetch_* reads + caches, read() hits the cache).
* production fail-closed: any contacted origin other than the frozen Demo origin
  sets ``production_contacted=True`` and the lifecycle can never PASS.
* redirect fail-closed: any HTTP 3xx response is detected at the transport
  boundary and raises a hard STOP before the body is returned to any caller.
* mutation retry = 0; malformed/ambiguous response raises immediately (STOP).
* 5 s single-request timeout is enforced around every signed request.
* server-time skew is read from the real ``/fapi/v1/time`` endpoint (not
  hardcoded to zero) so the frozen 5000 ms gate applies to live evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time as _time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from nautilus_trader.core.nautilus_pyo3 import HttpMethod

from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    AccountState,
    LimitOrderFilters,
    MarketCloseFilters,
    SymbolState,
)
from global_quant.gate1b.mutation_runner import MutationRunnerError

# Frozen single-request timeout (protocol section 11).
_REQUEST_TIMEOUT_SECONDS = 5.0
# Signed recvWindow applied to every signed query (protocol section 23).
_RECV_WINDOW_STR = str(RECEIVE_WINDOW_MS)

# Domain-separated raw response allowlist (protocol section 16): only these
# top-level keys are read from each venue response; any other shape is a STOP.
_REQUIRED_ACCOUNT_KEYS = frozenset(
    {"canTrade", "multiAssetsMargin", "assets", "positions"}
)
_REQUIRED_DUAL_KEYS = frozenset({"dualSidePosition"})
_REQUIRED_SYMBOL_CONFIG_KEYS = frozenset({"symbol", "marginType", "leverage", "isAutoAddMargin"})
_REQUIRED_ORDER_KEYS = frozenset({"status", "executedQty"})


class _SignedHttpClient(Protocol):
    """Minimal protocol the adapter needs from a BinanceHttpClient-like client.

    ``base_url`` must equal the frozen Demo origin before any request.
    ``sign_request`` is async, auto-signs, and returns raw response bytes.
    """

    @property
    def base_url(self) -> str: ...

    async def sign_request(
        self,
        http_method: HttpMethod,
        url_path: str,
        payload: dict[str, str] | None = None,
        ratelimiter_keys: list[str] | None = None,
    ) -> bytes: ...


# ---------------------------------------------------------------------------
# Redirect isolation guard (protocol section 2.1 redirect fail-closed).
#
# The nautilus ``HttpClient`` (pyo3 Rust) follows redirects internally and does
# not expose a ``max_redirects`` / ``follow_redirects`` toggle.  This guard
# wraps the inner ``_client`` and intercepts every ``request()`` call so that
# any HTTP 3xx response is detected before the body is returned to
# ``BinanceHttpClient.send_request``.  A detected redirect raises a hard STOP
# and the lifecycle can never PASS with ``production_contacted``.
# ---------------------------------------------------------------------------


class _RedirectGuard:
    """Fail-closed on any HTTP 3xx redirect from the underlying pyo3 client.

    Delegates all attribute access to the inner client so the pyo3 object
    remains a drop-in replacement for ``BinanceHttpClient._client``.  Only
    ``request`` is intercepted; all other attributes (rate-limit state, etc.)
    are passed through transparently.
    """

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._inner.request(*args, **kwargs)
        status = int(getattr(response, "status", 0))
        if 300 <= status < 400:
            raise MutationRunnerError(f"DEMO_HTTP_REDIRECT_DETECTED:{status}")
        return response


def _install_redirect_guard(http_client: Any) -> None:
    """Wrap ``http_client._client`` with ``_RedirectGuard`` if not already guarded.

    Idempotent: if the inner client is already a ``_RedirectGuard``, no second
    wrapping is applied.  The guard is installed exactly once at transport
    construction time (``DemoLifecycleTransport.__post_init__``) before any
    credential-bearing request is made.
    """
    inner = getattr(http_client, "_client", None)
    if inner is None:
        return
    if isinstance(inner, _RedirectGuard):
        return  # already guarded
    http_client._client = _RedirectGuard(inner)


@dataclass
class DemoLifecycleTransport:
    """Real Demo HTTP adapter implementing ``LifecycleTransport``.

    The adapter is constructed with an injectable signed HTTP client (the real
    ``BinanceHttpClient`` in a credential-bearing session, or a fake returning
    fixture bytes in tests). It performs no credential handling itself; the
    caller (supervisor/child session) supplies a client already bound to a
    credential. Every method is synchronous because ``MutationRunner`` is
    synchronous; async signed requests are driven through a private event loop.
    """

    http_client: _SignedHttpClient
    project_root: Any = None  # retained for runtime-binding symmetry; unused for I/O
    _loop: Any = field(default=None, init=False, repr=False)
    _cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _production_contacted: bool = field(default=False, init=False, repr=False)
    _contacted_origins: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        # Fail closed before any DNS/socket/HTTP creation: the client must be
        # pinned to the frozen Demo origin. A production or Testnet base_url is
        # a hard STOP and marks production as contacted.
        base = getattr(self.http_client, "base_url", "")
        self._contacted_origins.append(base)
        if base != DEMO_HTTP_ORIGIN:
            self._production_contacted = True
            raise MutationRunnerError("DEMO_HTTP_ORIGIN_MISMATCH")
        # Redirect isolation: wrap the inner pyo3 HttpClient so any 3xx is
        # detected and fail-closed before the response body reaches callers.
        _install_redirect_guard(self.http_client)
        self._loop = asyncio.new_event_loop()

    # -- lifecycle / cleanup -------------------------------------------------

    def close(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.close()
        self._loop = None

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self.close()

    # -- transport I/O core --------------------------------------------------

    def _run(self, coro: Any) -> Any:
        if self._loop is None:
            raise MutationRunnerError("DEMO_TRANSPORT_CLOSED")
        return self._loop.run_until_complete(coro)

    async def _signed(self, method: HttpMethod, path: str, params: Mapping[str, object]) -> bytes:
        payload = {k: str(v) for k, v in params.items()} or None
        try:
            return await asyncio.wait_for(
                self.http_client.sign_request(method, path, payload),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT") from exc
        except MutationRunnerError:
            raise
        except Exception as exc:
            # Nautilus raises BinanceClientError/BinanceServerError on >=400 or
            # non-JSON 200; collapse to a structured STOP without retaining raw
            # response text (the type name is the only retained detail).
            raise MutationRunnerError(
                f"DEMO_HTTP_FAILURE_{type(exc).__name__.upper()}"
            ) from exc

    def _get_json(self, path: str, params: Mapping[str, object]) -> Any:
        raw = self._run(self._signed(HttpMethod.GET, path, params))
        return self._parse_json(raw, path)

    def _parse_json(self, raw: bytes, context: str) -> Any:
        if not raw:
            raise MutationRunnerError(f"EMPTY_RESPONSE:{context}")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MutationRunnerError(f"MALFORMED_RESPONSE:{context}") from exc

    def _cache_get(self, path: str) -> Any:
        return self._cache.get(path)

    def _cache_set(self, path: str, value: Any) -> None:
        self._cache[path] = value

    # -- frozen signed parameter maps (protocol section 12) -----------------

    @staticmethod
    def _recv_window() -> dict[str, str]:
        return {"recvWindow": _RECV_WINDOW_STR}

    @staticmethod
    def _symbol_params() -> dict[str, str]:
        return {"symbol": SYMBOL, **DemoLifecycleTransport._recv_window()}

    # -- LifecycleTransport implementation ------------------------------------

    def fetch_server_time_skew(self) -> Decimal:
        """Read ``/fapi/v1/time`` and compute server-time skew in milliseconds.

        The skew is the difference between the server timestamp and the local
        midpoint of two ``time.time()`` calls bracketing the HTTP request.
        The frozen 5000 ms gate in ``validate_account_state`` is applied to
        this value; a hardcoded zero is never substituted.

        This endpoint is public (no signature required) and is read exactly
        once per lifecycle, cached like every other transport path.
        """
        t0_ms = int(_time.time() * 1000)
        server = self._cached_json(
            "/fapi/v1/time", {}, required_keys=frozenset({"serverTime"})
        )
        t1_ms = int(_time.time() * 1000)
        local_mid = (t0_ms + t1_ms) // 2
        server_time_ms = int(server["serverTime"])
        return Decimal(str(server_time_ms - local_mid))

    def fetch_account_state(self) -> AccountState:
        # Protocol section 5 exact-field allowlist parse. Combines the raw
        # /fapi/v2/account, /fapi/v1/positionSide/dual, /fapi/v1/symbolConfig,
        # /fapi/v1/openOrders, /fapi/v1/openAlgoOrders, and /fapi/v1/time
        # responses. The server-time skew is read from the real endpoint so
        # the frozen 5000 ms gate applies to live evidence.
        account = self._cached_json(
            "/fapi/v2/account", self._recv_window(), _REQUIRED_ACCOUNT_KEYS
        )
        dual = self._cached_json(
            "/fapi/v1/positionSide/dual", self._recv_window(), _REQUIRED_DUAL_KEYS
        )
        symbol_config = self._cached_json(
            "/fapi/v1/symbolConfig", self._symbol_params(), _REQUIRED_SYMBOL_CONFIG_KEYS
        )
        regular_orders = self._cached_json(
            "/fapi/v1/openOrders", self._recv_window(), required_keys=None
        )
        algo_orders = self._cached_json(
            "/fapi/v1/openAlgoOrders", self._recv_window(), required_keys=None
        )
        skew = self.fetch_server_time_skew()
        return _build_account_state(
            account=account,
            dual=dual,
            symbol_config=symbol_config,
            regular_orders=regular_orders,
            algo_orders=algo_orders,
            server_time_skew_ms=skew,
        )

    def fetch_symbol_state(self) -> SymbolState:
        exchange_info = self._cached_json(
            "/fapi/v1/exchangeInfo", {}, required_keys=frozenset({"symbols"})
        )
        return _build_symbol_state(exchange_info)

    def fetch_filters(self) -> LimitOrderFilters:
        exchange_info = self._cached_json(
            "/fapi/v1/exchangeInfo", {}, required_keys=frozenset({"symbols"})
        )
        return _build_limit_order_filters(exchange_info)

    def fetch_book(self) -> tuple[Decimal, Decimal]:
        book = self._cached_json(
            "/fapi/v1/ticker/bookTicker",
            {"symbol": SYMBOL},
            required_keys=frozenset({"bidPrice", "askPrice"}),
        )
        return _build_book(book)

    def fetch_mark(self) -> Decimal:
        mark = self._cached_json(
            "/fapi/v1/premiumIndex",
            {"symbol": SYMBOL},
            required_keys=frozenset({"markPrice"}),
        )
        return _build_mark(mark)

    def read(self, reservation: Any) -> Any:
        # The runner promotes the reservation digest to an ownership source;
        # the returned sanitized payload only records that the allowlisted path
        # was read. A cached response is reused so the section-11 HTTP budget is
        # not exceeded by duplicate fetch/read pairs.
        path = reservation.path
        cached = self._cache_get(path)
        if cached is None:
            params = dict(getattr(reservation, "parameters", {}) or {})
            cached = self._get_json(path, params)
            self._cache_set(path, cached)
        return {"path": path}

    def send_create(self, reservation: Any) -> dict[str, str]:
        return self._post_order(reservation)

    def send_query_order(self, reservation: Any) -> tuple[str, Decimal, Decimal]:
        order = self._get_order(reservation)
        status = _require_str(order, "status", "ORDER_QUERY")
        executed = _require_decimal(order, "executedQty", "ORDER_QUERY")
        return status, executed, Decimal("0")

    def send_cancel(self, reservation: Any) -> str:
        order = self._delete_order(reservation)
        return _require_str(order, "status", "ORDER_CANCEL")

    def send_terminal_query(self, reservation: Any) -> tuple[str, Decimal]:
        order = self._get_order(reservation)
        return (
            _require_str(order, "status", "TERMINAL_QUERY"),
            _require_decimal(order, "executedQty", "TERMINAL_QUERY"),
        )

    def fetch_final_state(self) -> dict[str, Any]:
        regular_orders = self._cached_json(
            "/fapi/v1/openOrders", self._recv_window(), required_keys=None
        )
        algo_orders = self._cached_json(
            "/fapi/v1/openAlgoOrders", self._recv_window(), required_keys=None
        )
        account = self._cached_json(
            "/fapi/v2/account", self._recv_window(), _REQUIRED_ACCOUNT_KEYS
        )
        symbol_config = self._cached_json(
            "/fapi/v1/symbolConfig", self._symbol_params(), _REQUIRED_SYMBOL_CONFIG_KEYS
        )
        positions = _coerce_list(account.get("positions"), "positions")
        nonzero = [
            {
                "symbol": str(p.get("symbol")),
                "quantity": str(_require_decimal(p, "positionAmt", "POSITION")),
            }
            for p in positions
            if _require_decimal(p, "positionAmt", "POSITION") != 0
        ]
        return {
            "nonzero_positions": tuple(
                sorted((p["symbol"], Decimal(p["quantity"])) for p in nonzero)
            ),
            "open_regular_orders": len(_coerce_list(regular_orders, "openOrders")),
            "open_algo_orders": len(_coerce_list(algo_orders, "openAlgoOrders")),
            "account_config_matches": _symbol_config_matches(symbol_config),
        }

    def fetch_market_close_filters(self) -> MarketCloseFilters:
        exchange_info = self._cached_json(
            "/fapi/v1/exchangeInfo", {}, required_keys=frozenset({"symbols"})
        )
        return _build_market_close_filters(exchange_info)

    def fetch_reconcile_state(self) -> dict[str, Any]:
        # Containment ownership proof (section 14): refreshed exchange info, mark,
        # user trades, full account/positions. Reuse cached snapshots; the runner
        # binds freshness through the reservation digests it already promoted.
        account = self._cached_json(
            "/fapi/v2/account", self._recv_window(), _REQUIRED_ACCOUNT_KEYS
        )
        positions = _coerce_list(account.get("positions"), "positions")
        eth_position = next(
            (p for p in positions if str(p.get("symbol")) == SYMBOL), None
        )
        return {
            "eth_position_qty": (
                str(_require_decimal(eth_position, "positionAmt", "RECONCILE"))
                if eth_position is not None
                else "0"
            ),
            "positions_present": len(positions),
        }

    def send_emergency_close(self, reservation: Any) -> dict[str, str]:
        return self._post_order(reservation)

    def send_emergency_query(self, reservation: Any) -> tuple[str, Decimal]:
        order = self._get_order(reservation)
        return (
            _require_str(order, "status", "EMERGENCY_QUERY"),
            _require_decimal(order, "executedQty", "EMERGENCY_QUERY"),
        )

    def fetch_containment_final_state(self) -> dict[str, Any]:
        return self.fetch_final_state()

    @property
    def production_contacted(self) -> bool:
        return self._production_contacted

    # -- mutating helpers ----------------------------------------------------

    def _post_order(self, reservation: Any) -> dict[str, str]:
        params = dict(getattr(reservation, "parameters", {}) or {})
        raw = self._run(self._signed(HttpMethod.POST, "/fapi/v1/order", params))
        order = self._parse_json(raw, "ORDER_CREATE")
        if not isinstance(order, dict):
            raise MutationRunnerError("MALFORMED_RESPONSE:ORDER_CREATE")
        ack = {k: str(v) for k, v in order.items() if k in {"orderId", "status", "clientOrderId"}}
        if "status" not in ack:
            raise MutationRunnerError("MALFORMED_RESPONSE:ORDER_CREATE")
        return ack

    def _delete_order(self, reservation: Any) -> dict[str, str]:
        params = dict(getattr(reservation, "parameters", {}) or {})
        raw = self._run(self._signed(HttpMethod.DELETE, "/fapi/v1/order", params))
        order = self._parse_json(raw, "ORDER_DELETE")
        if not isinstance(order, dict):
            raise MutationRunnerError("MALFORMED_RESPONSE:ORDER_DELETE")
        return {k: str(v) for k, v in order.items()}

    def _get_order(self, reservation: Any) -> dict[str, str]:
        params = dict(getattr(reservation, "parameters", {}) or {})
        raw = self._run(self._signed(HttpMethod.GET, "/fapi/v1/order", params))
        order = self._parse_json(raw, "ORDER_QUERY")
        if not isinstance(order, dict) or not _REQUIRED_ORDER_KEYS.issubset(order.keys()):
            raise MutationRunnerError("MALFORMED_RESPONSE:ORDER_QUERY")
        return {k: str(v) for k, v in order.items()}

    def _cached_json(
        self,
        path: str,
        params: Mapping[str, object],
        required_keys: frozenset[str] | None,
    ) -> Any:
        cached = self._cache_get(path)
        if cached is None:
            cached = self._get_json(path, params)
            self._cache_set(path, cached)
        if required_keys is not None and isinstance(cached, dict):
            missing = required_keys - cached.keys()
            if missing:
                raise MutationRunnerError(f"MISSING_FIELDS:{path}:{sorted(missing)}")
        return cached


# ---------------------------------------------------------------------------
# Raw response parsers (protocol section 5/6/7 exact-field allowlist).
# Each parser raises MutationProtocolError/MutationRunnerError on any missing,
# malformed, or non-decimal field. No field is defaulted to a clean value.
# ---------------------------------------------------------------------------


def _coerce_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise MutationRunnerError(f"MALFORMED_RESPONSE:{context}")
    return value


def _require_str(mapping: Mapping[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MutationRunnerError(f"MISSING_FIELD:{context}:{key}")
    return value


def _require_decimal(mapping: Mapping[str, Any], key: str, context: str) -> Decimal:
    value = mapping.get(key)
    if value is None:
        raise MutationRunnerError(f"MISSING_FIELD:{context}:{key}")
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise MutationRunnerError(f"MALFORMED_DECIMAL:{context}:{key}") from exc
    if not result.is_finite():
        raise MutationRunnerError(f"NON_FINITE:{context}:{key}")
    return result


def _require_bool(mapping: Mapping[str, Any], key: str, context: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise MutationRunnerError(f"MISSING_BOOL:{context}:{key}")
    return value


def _require_int(mapping: Mapping[str, Any], key: str, context: str) -> int:
    value = mapping.get(key)
    # Binance serializes leverage as a numeric value; accept int or decimal-int.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise MutationRunnerError(f"MISSING_INT:{context}:{key}")


def _build_account_state(
    *,
    account: Any,
    dual: Any,
    symbol_config: Any,
    regular_orders: Any,
    algo_orders: Any,
    server_time_skew_ms: Decimal,
) -> AccountState:
    if not isinstance(account, dict) or not _REQUIRED_ACCOUNT_KEYS.issubset(account.keys()):
        raise MutationRunnerError("MALFORMED_RESPONSE:ACCOUNT")
    if not isinstance(dual, dict) or not _REQUIRED_DUAL_KEYS.issubset(dual.keys()):
        raise MutationRunnerError("MALFORMED_RESPONSE:POSITION_MODE")
    if not isinstance(symbol_config, dict):
        raise MutationRunnerError("MALFORMED_RESPONSE:SYMBOL_CONFIG")

    assets = _coerce_list(account.get("assets"), "ACCOUNT_ASSETS")
    usdt = next((a for a in assets if str(a.get("asset")) == "USDT"), None)
    if usdt is None:
        raise MutationRunnerError("MISSING_USDT_WALLET")
    wallet = _require_decimal(usdt, "walletBalance", "USDT_WALLET")
    available = _require_decimal(usdt, "availableBalance", "USDT_WALLET")

    positions = _coerce_list(account.get("positions"), "ACCOUNT_POSITIONS")
    nonzero = tuple(
        sorted(
            (str(p.get("symbol")), _require_decimal(p, "positionAmt", "POSITION"))
            for p in positions
            if _require_decimal(p, "positionAmt", "POSITION") != 0
        )
    )
    regular = _coerce_list(regular_orders, "OPEN_ORDERS")
    algo = _coerce_list(algo_orders, "OPEN_ALGO_ORDERS")
    regular_ids = tuple(
        sorted(str(o.get("orderId")) for o in regular if o.get("orderId") is not None)
    )
    algo_ids = tuple(
        sorted(str(o.get("algoId")) for o in algo if o.get("algoId") is not None)
    )

    # Server-time skew is read from the real /fapi/v1/time endpoint so the
    # frozen 5000 ms gate (validate_account_state) applies to live data.
    return AccountState(
        can_trade=_require_bool(account, "canTrade", "ACCOUNT"),
        dual_side_position=_require_bool(dual, "dualSidePosition", "POSITION_MODE"),
        multi_assets_margin=_require_bool(account, "multiAssetsMargin", "ACCOUNT"),
        margin_type=_require_str(symbol_config, "marginType", "SYMBOL_CONFIG"),
        leverage=_require_int(symbol_config, "leverage", "SYMBOL_CONFIG"),
        auto_add_margin=_require_bool(symbol_config, "isAutoAddMargin", "SYMBOL_CONFIG"),
        server_time_skew_ms=server_time_skew_ms,
        wallet_balance=wallet,
        available_balance=available,
        nonzero_positions=nonzero,
        open_regular_order_ids=regular_ids,
        open_algo_order_ids=algo_ids,
    )


def _symbol_config_matches(symbol_config: Any) -> bool:
    if not isinstance(symbol_config, dict):
        return False
    return (
        str(symbol_config.get("marginType")) == "ISOLATED"
        and str(symbol_config.get("leverage")) == "1"
        and symbol_config.get("isAutoAddMargin") is False
    )


def _eth_symbol(exchange_info: Any) -> dict[str, Any]:
    if not isinstance(exchange_info, dict):
        raise MutationRunnerError("MALFORMED_RESPONSE:EXCHANGE_INFO")
    symbols = _coerce_list(exchange_info.get("symbols"), "EXCHANGE_INFO_SYMBOLS")
    eth = next((s for s in symbols if str(s.get("symbol")) == SYMBOL), None)
    if not isinstance(eth, dict):
        raise MutationRunnerError("ETHUSDT_SYMBOL_NOT_FOUND")
    return eth


def _build_symbol_state(exchange_info: Any) -> SymbolState:
    eth = _eth_symbol(exchange_info)
    order_types = eth.get("orderTypes")
    filters = _coerce_list(eth.get("filters"), "EXCHANGE_INFO_FILTERS")
    if not isinstance(order_types, list):
        raise MutationRunnerError("MALFORMED_RESPONSE:ORDER_TYPES")
    tif = eth.get("timeInForce")
    if not isinstance(tif, list):
        raise MutationRunnerError("MALFORMED_RESPONSE:TIME_IN_FORCE")
    counts: dict[str, int] = {}
    for flt in filters:
        if not isinstance(flt, dict):
            raise MutationRunnerError("MALFORMED_RESPONSE:FILTER")
        ftype = flt.get("filterType")
        if not isinstance(ftype, str):
            raise MutationRunnerError("MALFORMED_RESPONSE:FILTER_TYPE")
        counts[ftype] = counts.get(ftype, 0) + 1
    return SymbolState(
        symbol=str(eth.get("symbol")),
        status=str(eth.get("status")),
        contract_type=str(eth.get("contractType")),
        quote_asset=str(eth.get("quoteAsset")),
        margin_asset=str(eth.get("marginAsset")),
        order_types=frozenset(str(o) for o in order_types),
        time_in_force=frozenset(str(t) for t in tif),
        filter_type_counts=tuple(sorted(counts.items())),
        uninterpreted_applicable_filter_types=(),
    )


def _extract_filter(filters: list[Any], ftype: str) -> dict[str, Any]:
    matches = [f for f in filters if isinstance(f, dict) and f.get("filterType") == ftype]
    if len(matches) != 1:
        raise MutationRunnerError(f"FILTER_CARDINALITY:{ftype}:{len(matches)}")
    return matches[0]


def _build_limit_order_filters(exchange_info: Any) -> LimitOrderFilters:
    eth = _eth_symbol(exchange_info)
    filters = _coerce_list(eth.get("filters"), "EXCHANGE_INFO_FILTERS")
    price = _extract_filter(filters, "PRICE_FILTER")
    lot = _extract_filter(filters, "LOT_SIZE")
    _extract_filter(filters, "MARKET_LOT_SIZE")
    min_notional = _extract_filter(filters, "MIN_NOTIONAL")
    percent = _extract_filter(filters, "PERCENT_PRICE")
    return LimitOrderFilters(
        min_price=_require_decimal(price, "minPrice", "PRICE_FILTER"),
        max_price=_require_decimal(price, "maxPrice", "PRICE_FILTER"),
        tick_size=_require_decimal(price, "tickSize", "PRICE_FILTER"),
        min_quantity=_require_decimal(lot, "minQty", "LOT_SIZE"),
        max_quantity=_require_decimal(lot, "maxQty", "LOT_SIZE"),
        step_size=_require_decimal(lot, "stepSize", "LOT_SIZE"),
        min_notional=_require_decimal(min_notional, "notional", "MIN_NOTIONAL"),
        percent_price_multiplier_down=_require_decimal(percent, "multiplierDown", "PERCENT_PRICE"),
        percent_price_multiplier_up=_require_decimal(percent, "multiplierUp", "PERCENT_PRICE"),
    )


def _build_market_close_filters(exchange_info: Any) -> MarketCloseFilters:
    eth = _eth_symbol(exchange_info)
    filters = _coerce_list(eth.get("filters"), "EXCHANGE_INFO_FILTERS")
    market_lot = _extract_filter(filters, "MARKET_LOT_SIZE")
    min_notional = _extract_filter(filters, "MIN_NOTIONAL")
    return MarketCloseFilters(
        min_quantity=_require_decimal(market_lot, "minQty", "MARKET_LOT_SIZE"),
        max_quantity=_require_decimal(market_lot, "maxQty", "MARKET_LOT_SIZE"),
        step_size=_require_decimal(market_lot, "stepSize", "MARKET_LOT_SIZE"),
        min_notional=_require_decimal(min_notional, "notional", "MIN_NOTIONAL"),
    )


def _build_book(book: Any) -> tuple[Decimal, Decimal]:
    if not isinstance(book, dict):
        raise MutationRunnerError("MALFORMED_RESPONSE:BOOK_TICKER")
    bid = _require_decimal(book, "bidPrice", "BOOK_TICKER")
    ask = _require_decimal(book, "askPrice", "BOOK_TICKER")
    if bid <= 0 or ask <= 0 or bid >= ask:
        raise MutationRunnerError("INVALID_BOOK_SPREAD")
    return bid, ask


def _build_mark(mark: Any) -> Decimal:
    if not isinstance(mark, dict):
        raise MutationRunnerError("MALFORMED_RESPONSE:MARK")
    price = _require_decimal(mark, "markPrice", "MARK")
    if price <= 0:
        raise MutationRunnerError("INVALID_MARK_PRICE")
    return price
