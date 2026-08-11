"""Typed, cache-free signed transport owned by one credential process.

The typed transport has no retry, cache, or independent mutation ownership.
One :class:`ReservedRequest` produces at most one signed client call and the
returned sanitized result is cryptographically tied to that exact reservation.
The existing bounded HTTP worker remains an in-child leaf detail; dispatch
durability and hard quiescence come from the execution kernel and process
boundary around it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.common.component import LiveClock
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

from global_quant.gate1b.credential_http import (
    _REQUEST_TIMEOUT_SECONDS,
    CredentialHttpError,
    _install_redirect_safe_client,
    _RedirectSafeHttpClient,
)
from global_quant.gate1b.execution_journal import PreIntentReadReservation
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RequestPurpose,
    ReservedRequest,
)
from global_quant.gate1b.process_boundary import ChildIOAuthority
from global_quant.gate1b.safety import (
    DemoCredentials,
    resolve_demo_endpoints,
    validate_demo_endpoints,
)


class CredentialTransportError(RuntimeError):
    """Fail-closed transport error with explicit dispatch classification."""

    def __init__(self, reason: str, *, post_dispatch: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.post_dispatch = post_dispatch


class ResponseKind(StrEnum):
    """Sanitized response domains consumed by the lifecycle runner."""

    MUTATION_ACK = "MUTATION_ACK"
    ORDER_OBSERVATION = "ORDER_OBSERVATION"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    SERVER_TIME = "SERVER_TIME"
    EXCHANGE_INFO = "EXCHANGE_INFO"
    BOOK_TICKER = "BOOK_TICKER"
    MARK_PRICE = "MARK_PRICE"
    POSITION_MODE = "POSITION_MODE"
    SYMBOL_CONFIG = "SYMBOL_CONFIG"
    OPEN_ORDERS = "OPEN_ORDERS"
    OPEN_ALGO_ORDERS = "OPEN_ALGO_ORDERS"
    USER_TRADES = "USER_TRADES"
    ACCOUNT = "ACCOUNT"


_RESULT_SCHEMA = "gate1b.credential-transport-result.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ASSET = re.compile(r"[A-Z0-9]{2,20}\Z")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CredentialTransportError("SANITIZED_RESULT_INVALID") from exc


def _assert_sanitized_value(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is list:
        for item in value:
            _assert_sanitized_value(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise CredentialTransportError("SANITIZED_RESULT_INVALID")
        for nested in value.values():
            _assert_sanitized_value(nested)
        return
    raise CredentialTransportError("SANITIZED_RESULT_INVALID")


def _sanitized_result_sha256(
    kind: ResponseKind,
    fields: tuple[tuple[str, object], ...],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "fields": dict(fields),
                "kind": kind.value,
                "schema_version": _RESULT_SCHEMA,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TransportResult:
    """A sanitized response bound to the exact canonical reservation."""

    request_sha256: str
    logical_request_sha256: str
    result_sha256: str
    kind: ResponseKind
    fields: tuple[tuple[str, object], ...]

    @classmethod
    def build(
        cls,
        *,
        request_sha256: str,
        logical_request_sha256: str,
        kind: ResponseKind,
        fields: tuple[tuple[str, object], ...],
    ) -> TransportResult:
        normalized = tuple(sorted(fields))
        return cls(
            request_sha256=request_sha256,
            logical_request_sha256=logical_request_sha256,
            result_sha256=_sanitized_result_sha256(kind, normalized),
            kind=kind,
            fields=normalized,
        )

    def __post_init__(self) -> None:
        if (
            type(self.request_sha256) is not str
            or _SHA256.fullmatch(self.request_sha256) is None
            or type(self.logical_request_sha256) is not str
            or _SHA256.fullmatch(self.logical_request_sha256) is None
            or type(self.result_sha256) is not str
            or _SHA256.fullmatch(self.result_sha256) is None
            or type(self.kind) is not ResponseKind
            or type(self.fields) is not tuple
            or tuple(sorted(self.fields)) != self.fields
            or len({name for name, _value in self.fields}) != len(self.fields)
            or any(type(name) is not str or not name for name, _value in self.fields)
        ):
            raise CredentialTransportError("SANITIZED_RESULT_INVALID")
        for _name, value in self.fields:
            _assert_sanitized_value(value)
        expected = _sanitized_result_sha256(self.kind, self.fields)
        if not hmac.compare_digest(expected, self.result_sha256):
            raise CredentialTransportError("SANITIZED_RESULT_DIGEST_MISMATCH")

    def field(self, name: str) -> object:
        for key, value in self.fields:
            if key == name:
                return value
        raise KeyError(name)


class _SignedClient(Protocol):
    @property
    def base_url(self) -> str: ...

    def sign_request(
        self,
        http_method: HttpMethod,
        url_path: str,
        payload: dict[str, str] | None = None,
        ratelimiter_keys: list[str] | None = None,
        *,
        absolute_deadline_ns: int,
    ) -> bytes: ...


class _ExistingDemoStackSignedClient:
    """Synchronous child-owned driver for the existing signed Demo client."""

    def __init__(self, http_client: object) -> None:
        if getattr(http_client, "base_url", None) != DEMO_HTTP_ORIGIN:
            raise CredentialTransportError("DEMO_HTTP_ORIGIN_MISMATCH")
        _install_redirect_safe_client(http_client)
        self._http_client = http_client
        self._loop = asyncio.new_event_loop()
        self._closed = False

    @property
    def base_url(self) -> str:
        return str(self._http_client.base_url)

    def sign_request(
        self,
        http_method: HttpMethod,
        url_path: str,
        payload: dict[str, str] | None = None,
        ratelimiter_keys: list[str] | None = None,
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        if self._closed:
            raise CredentialTransportError("TRANSPORT_CLOSED")
        if ratelimiter_keys is not None and ratelimiter_keys != []:
            raise CredentialTransportError("RATELIMITER_KEYS_FORBIDDEN")
        try:
            return self._loop.run_until_complete(
                self._signed(
                    http_method,
                    url_path,
                    dict(payload or {}),
                    absolute_deadline_ns=absolute_deadline_ns,
                )
            )
        except CredentialHttpError as exc:
            if (
                http_method == HttpMethod.GET
                and url_path == "/fapi/v1/order"
                and self._is_exact_order_not_found(exc.__cause__)
            ):
                raise CredentialTransportError(
                    "DEMO_ORDER_CONFIRMED_NOT_FOUND",
                    post_dispatch=True,
                ) from None
            raise

    async def _signed(
        self,
        method: HttpMethod,
        path: str,
        params: dict[str, str],
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        if type(absolute_deadline_ns) is not int:
            raise CredentialTransportError("ABSOLUTE_DEADLINE_REQUIRED")
        remaining_seconds = min(
            _REQUEST_TIMEOUT_SECONDS,
            (absolute_deadline_ns - time.monotonic_ns()) / 1_000_000_000,
        )
        if remaining_seconds <= 0:
            raise CredentialTransportError("ABSOLUTE_DEADLINE_EXHAUSTED")
        payload = dict(params) or None
        client = getattr(self._http_client, "_client", None)
        if not isinstance(client, _RedirectSafeHttpClient):
            raise CredentialTransportError("REDIRECT_SAFE_CLIENT_REQUIRED")
        client.authorize_absolute_deadline(absolute_deadline_ns)
        try:
            return await asyncio.wait_for(
                self._http_client.sign_request(method, path, payload),
                timeout=remaining_seconds,
            )
        except TimeoutError as exc:
            raise CredentialHttpError("DEMO_HTTP_TIMEOUT") from exc
        except CredentialHttpError:
            raise
        except Exception as exc:
            raise CredentialHttpError(f"DEMO_HTTP_FAILURE_{type(exc).__name__.upper()}") from exc
        finally:
            client.cancel_absolute_deadline(absolute_deadline_ns)

    @staticmethod
    def _is_exact_order_not_found(error: BaseException | None) -> bool:
        if type(error) is not BinanceClientError:
            return False
        message = error.message
        return (
            type(error.status) is int
            and error.status in {400, 404}
            and type(message) is dict
            and set(message) == {"code", "msg"}
            and type(message["code"]) is int
            and message["code"] == -2013
            and type(message["msg"]) is str
            and message["msg"] == "Order does not exist."
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._loop.is_closed():
            self._loop.close()
        client = getattr(self._http_client, "_client", None)
        if isinstance(client, _RedirectSafeHttpClient):
            client.close()


_PRODUCTION_TRANSPORT_CONSTRUCTION_TOKEN = object()


def build_production_credential_transport(
    credentials: DemoCredentials,
    *,
    io_authority: ChildIOAuthority,
) -> ProcessBoundCredentialTransport:
    """Build the sole signed stack for one attested production child."""

    if type(credentials) is not DemoCredentials or type(io_authority) is not ChildIOAuthority:
        raise CredentialTransportError("PRODUCTION_CHILD_IO_AUTHORITY_REQUIRED")
    # Reject wrong-process, stale, unguarded, test-only, or reused authority
    # before even constructing the credential-derived HTTP stack.
    io_authority._assert_transport_bindable()
    try:
        endpoints = resolve_demo_endpoints()
        validate_demo_endpoints(endpoints)
        http_client = get_cached_binance_http_client(
            clock=LiveClock(),
            account_type=BinanceAccountType.USDT_FUTURES,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            base_url=None,
            environment=BinanceEnvironment.DEMO,
            is_us=False,
            proxy_url=None,
        )
    except Exception:
        raise CredentialTransportError("PRODUCTION_DEMO_STACK_BUILD_FAILED") from None
    if getattr(http_client, "base_url", None) != DEMO_HTTP_ORIGIN:
        raise CredentialTransportError("DEMO_HTTP_ORIGIN_MISMATCH")
    signed_client: _ExistingDemoStackSignedClient | None = None
    try:
        signed_client = _ExistingDemoStackSignedClient(http_client)
        return ProcessBoundCredentialTransport(
            signed_client,
            io_authority=io_authority,
            _construction_token=_PRODUCTION_TRANSPORT_CONSTRUCTION_TOKEN,
        )
    except BaseException:
        if signed_client is not None:
            signed_client.close()
        raise


_HTTP_METHODS = {
    "GET": HttpMethod.GET,
    "POST": HttpMethod.POST,
    "DELETE": HttpMethod.DELETE,
}
_READ_PATHS = frozenset(
    {
        "/fapi/v1/time",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/symbolConfig",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
        "/fapi/v2/account",
    }
)
_ORDER_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
)
_SIDES = frozenset({"BUY", "SELL"})
_POSITION_SIDES = frozenset({"BOTH", "LONG", "SHORT"})
_ORDER_TYPES = frozenset(
    {
        "LIMIT",
        "MARKET",
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }
)
_ALGO_STATUSES = frozenset(
    {"NEW", "WORKING", "TRIGGERED", "FINISHED", "CANCELED", "EXPIRED", "REJECTED"}
)
_REQUIRED_FILTERS = frozenset(
    {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "PERCENT_PRICE"}
)
_TIMED_RESPONSE_KINDS = frozenset(
    {
        ResponseKind.SERVER_TIME,
        ResponseKind.BOOK_TICKER,
        ResponseKind.MARK_PRICE,
    }
)


class _ReadParseError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _ReadParseError
    return value


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise _ReadParseError
    return value


def _string(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if type(value) is not str or not value or len(value) > 128:
        raise _ReadParseError
    return value


def _token(
    mapping: dict[str, object],
    key: str,
    *,
    allowed: frozenset[str] | None = None,
) -> str:
    value = _string(mapping, key)
    if _SAFE_TOKEN.fullmatch(value) is None or (allowed is not None and value not in allowed):
        raise _ReadParseError
    return value


def _boolean(mapping: dict[str, object], key: str) -> bool:
    value = mapping.get(key)
    if type(value) is not bool:
        raise _ReadParseError
    return value


def _integer(mapping: dict[str, object], key: str, *, minimum: int = 0) -> int:
    value = mapping.get(key)
    if type(value) is not int or value < minimum:
        raise _ReadParseError
    return value


def _decimal(
    mapping: dict[str, object],
    key: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> str:
    value = mapping.get(key)
    if type(value) is not str or not value or len(value) > 64:
        raise _ReadParseError
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _ReadParseError from None
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise _ReadParseError
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _domain_sha256(domain: str, value: object) -> str:
    if type(value) is int:
        if value < 0:
            raise _ReadParseError
        normalized = str(value)
    elif type(value) is str and value and len(value) <= 128:
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            raise _ReadParseError from None
        normalized = value
    else:
        raise _ReadParseError
    return hashlib.sha256(f"{domain}\0{normalized}".encode("ascii")).hexdigest()


class ProcessBoundCredentialTransport:
    """Execute one reservation synchronously in the credential-bearing child."""

    def __init__(
        self,
        signed_client: _SignedClient,
        *,
        io_authority: ChildIOAuthority,
        _construction_token: object,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        if (
            _construction_token is not _PRODUCTION_TRANSPORT_CONSTRUCTION_TOKEN
            or type(io_authority) is not ChildIOAuthority
        ):
            raise CredentialTransportError("PRODUCTION_TRANSPORT_CONSTRUCTION_FORBIDDEN")
        if getattr(signed_client, "base_url", None) != DEMO_HTTP_ORIGIN:
            raise CredentialTransportError("DEMO_HTTP_ORIGIN_MISMATCH")
        if (wall_clock_ms is not None and not callable(wall_clock_ms)) or (
            monotonic_ns is not None and not callable(monotonic_ns)
        ):
            raise CredentialTransportError("TRANSPORT_CLOCK_INVALID")
        io_authority._bind_transport()
        self._signed_client = signed_client
        self._io_authority = io_authority
        self._wall_clock_ms = wall_clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._consumed_reservations: set[str] = set()
        self._closed = False

    def close(self) -> None:
        """Close the one owned production stack exactly once."""

        if self._closed:
            return
        self._closed = True
        close = getattr(self._signed_client, "close", None)
        if callable(close):
            close()

    def execute(
        self,
        reservation: ReservedRequest,
        *,
        absolute_deadline_ns: int,
    ) -> TransportResult:
        if type(reservation) is not ReservedRequest:
            raise CredentialTransportError("INVALID_REQUEST_RESERVATION")
        return self._execute_exact(
            reservation,
            request_sha256=reservation.request_sha256,
            absolute_deadline_ns=absolute_deadline_ns,
        )

    def execute_pre_intent(
        self,
        reservation: PreIntentReadReservation,
        *,
        absolute_deadline_ns: int,
    ) -> TransportResult:
        """Execute one exact session-authority read without inventing an intent."""

        if type(reservation) is not PreIntentReadReservation:
            raise CredentialTransportError("INVALID_PRE_INTENT_RESERVATION")
        return self._execute_exact(
            reservation,
            request_sha256=reservation.reservation_sha256,
            absolute_deadline_ns=absolute_deadline_ns,
        )

    def _execute_exact(
        self,
        reservation: ReservedRequest | PreIntentReadReservation,
        *,
        request_sha256: str,
        absolute_deadline_ns: int,
    ) -> TransportResult:
        if self._closed:
            raise CredentialTransportError("TRANSPORT_CLOSED")
        self._io_authority.assert_io_authorized()
        if type(absolute_deadline_ns) is not int or absolute_deadline_ns <= 0:
            raise CredentialTransportError("ABSOLUTE_DEADLINE_EXHAUSTED")
        if reservation.origin != DEMO_HTTP_ORIGIN:
            raise CredentialTransportError("DEMO_HTTP_ORIGIN_MISMATCH")
        if (
            reservation.purpose is RequestPurpose.READ
            and (reservation.method != "GET" or reservation.path not in _READ_PATHS)
        ) or (
            reservation.purpose is not RequestPurpose.READ
            and (
                reservation.path != "/fapi/v1/order"
                or {
                    RequestPurpose.CREATE: "POST",
                    RequestPurpose.CANCEL: "DELETE",
                    RequestPurpose.EMERGENCY_CLOSE: "POST",
                }.get(reservation.purpose)
                != reservation.method
            )
        ):
            raise CredentialTransportError("REQUEST_CONTRACT_MISMATCH")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CredentialTransportError("TRANSPORT_EVENT_LOOP_REENTRANCY")

        try:
            method = _HTTP_METHODS[reservation.method]
        except KeyError as exc:  # ReservedRequest already rejects this.
            raise CredentialTransportError("REQUEST_METHOD_NOT_ALLOWLISTED") from exc

        local_wall_before_ms = self._wall_clock_ms()
        local_monotonic_before_ns = self._monotonic_ns()
        if (
            type(local_wall_before_ms) is not int
            or local_wall_before_ms <= 0
            or type(local_monotonic_before_ns) is not int
            or local_monotonic_before_ns <= 0
        ):
            raise CredentialTransportError("LOCAL_OBSERVATION_CLOCK_INVALID")
        if absolute_deadline_ns <= local_monotonic_before_ns:
            raise CredentialTransportError("ABSOLUTE_DEADLINE_EXHAUSTED")
        if request_sha256 in self._consumed_reservations:
            raise CredentialTransportError("RESERVATION_ALREADY_EXECUTED")
        # Consume before entering the signed client.  Every exception after
        # this point is conservatively post-dispatch; the slot is never rolled
        # back or made available for a second call.
        self._consumed_reservations.add(request_sha256)
        try:
            raw = self._signed_client.sign_request(
                method,
                reservation.path,
                dict(reservation.parameters),
                absolute_deadline_ns=absolute_deadline_ns,
            )
        except CredentialTransportError as exc:
            if (
                exc.reason == "DEMO_ORDER_CONFIRMED_NOT_FOUND"
                and reservation.purpose is RequestPurpose.READ
                and reservation.method == "GET"
                and reservation.path == "/fapi/v1/order"
            ):
                client_order_id = dict(reservation.parameters).get("origClientOrderId")
                if type(client_order_id) is not str:
                    self._raise_invalid_response(reservation)
                return TransportResult.build(
                    request_sha256=request_sha256,
                    logical_request_sha256=reservation.logical_request_sha256,
                    kind=ResponseKind.ORDER_NOT_FOUND,
                    fields=(
                        ("clientOrderId", client_order_id),
                        ("outcome", "CONFIRMED_NOT_FOUND"),
                        ("venueCode", -2013),
                    ),
                )
            reason = (
                "READ_IO_AMBIGUOUS"
                if reservation.purpose is RequestPurpose.READ
                else "POST_DISPATCH_IO_AMBIGUOUS"
            )
            raise CredentialTransportError(reason, post_dispatch=True) from None
        except Exception:
            reason = (
                "READ_IO_AMBIGUOUS"
                if reservation.purpose is RequestPurpose.READ
                else "POST_DISPATCH_IO_AMBIGUOUS"
            )
            raise CredentialTransportError(reason, post_dispatch=True) from None
        if type(raw) is not bytes:
            self._raise_invalid_response(reservation)

        local_monotonic_after_ns = self._monotonic_ns()
        local_wall_after_ms = self._wall_clock_ms()
        if (
            type(local_wall_after_ms) is not int
            or type(local_monotonic_after_ns) is not int
            or local_wall_after_ms < local_wall_before_ms
            or local_monotonic_after_ns < local_monotonic_before_ns
        ):
            self._raise_invalid_response(reservation)

        try:
            decoded = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._raise_invalid_response(reservation)

        kind, fields = self._sanitize(reservation, decoded)
        if kind in _TIMED_RESPONSE_KINDS:
            fields = (
                *fields,
                ("localMonotonicAfterNs", local_monotonic_after_ns),
                ("localMonotonicBeforeNs", local_monotonic_before_ns),
                ("localWallAfterMs", local_wall_after_ms),
                ("localWallBeforeMs", local_wall_before_ms),
            )
        return TransportResult.build(
            request_sha256=request_sha256,
            logical_request_sha256=reservation.logical_request_sha256,
            kind=kind,
            fields=fields,
        )

    @classmethod
    def _sanitize(
        cls,
        reservation: ReservedRequest | PreIntentReadReservation,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        if reservation.purpose is not RequestPurpose.READ:
            if type(decoded) is not dict:
                cls._raise_invalid_response(reservation)
            status = decoded.get("status")
            client_order_id = decoded.get("clientOrderId")
            if (
                type(status) is not str
                or status not in _ORDER_STATUSES
                or type(client_order_id) is not str
            ):
                cls._raise_invalid_response(reservation)
            expected_client_order_id = dict(reservation.parameters).get("newClientOrderId") or dict(
                reservation.parameters
            ).get("origClientOrderId")
            if client_order_id != expected_client_order_id:
                raise CredentialTransportError(
                    "RESPONSE_CLIENT_ID_MISMATCH",
                    post_dispatch=True,
                )
            fields: list[tuple[str, object]] = [
                ("clientOrderId", client_order_id),
                ("status", status),
            ]
            if "orderId" in decoded:
                try:
                    order_id_sha256 = _domain_sha256(
                        "binance-demo-order-id",
                        decoded["orderId"],
                    )
                except _ReadParseError:
                    cls._raise_invalid_response(reservation)
                fields.append(("orderIdSha256", order_id_sha256))
            return ResponseKind.MUTATION_ACK, tuple(fields)
        try:
            return cls._sanitize_read(reservation, decoded)
        except _ReadParseError:
            cls._raise_invalid_response(reservation)

    @classmethod
    def _sanitize_read(
        cls,
        reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        parser = {
            "/fapi/v1/time": cls._server_time,
            "/fapi/v1/exchangeInfo": cls._exchange_info,
            "/fapi/v1/ticker/bookTicker": cls._book_ticker,
            "/fapi/v1/premiumIndex": cls._mark_price,
            "/fapi/v1/positionSide/dual": cls._position_mode,
            "/fapi/v1/symbolConfig": cls._symbol_config,
            "/fapi/v1/openOrders": cls._open_orders,
            "/fapi/v1/openAlgoOrders": cls._open_algo_orders,
            "/fapi/v1/order": cls._order,
            "/fapi/v1/userTrades": cls._user_trades,
            "/fapi/v2/account": cls._account,
        }.get(reservation.path)
        if parser is None:
            raise _ReadParseError
        return parser(reservation, decoded)

    @staticmethod
    def _server_time(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        return ResponseKind.SERVER_TIME, (("serverTime", _integer(item, "serverTime", minimum=1)),)

    @staticmethod
    def _book_ticker(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        symbol = _token(item, "symbol")
        if symbol != "ETHUSDT":
            raise _ReadParseError
        bid = _decimal(item, "bidPrice", positive=True)
        ask = _decimal(item, "askPrice", positive=True)
        if Decimal(ask) <= Decimal(bid):
            raise _ReadParseError
        fields: tuple[tuple[str, object], ...] = (
            ("askPrice", ask),
            ("askQty", _decimal(item, "askQty", positive=True)),
            ("bidPrice", bid),
            ("bidQty", _decimal(item, "bidQty", positive=True)),
            ("lastUpdateId", _integer(item, "lastUpdateId", minimum=1)),
            ("symbol", symbol),
            ("time", _integer(item, "time", minimum=1)),
        )
        return ResponseKind.BOOK_TICKER, fields

    @staticmethod
    def _mark_price(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        symbol = _token(item, "symbol")
        if symbol != "ETHUSDT":
            raise _ReadParseError
        return ResponseKind.MARK_PRICE, (
            ("markPrice", _decimal(item, "markPrice", positive=True)),
            ("symbol", symbol),
            ("time", _integer(item, "time", minimum=1)),
        )

    @staticmethod
    def _position_mode(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        return ResponseKind.POSITION_MODE, (
            ("dualSidePosition", _boolean(item, "dualSidePosition")),
        )

    @staticmethod
    def _symbol_config(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        if type(decoded) is list:
            matches = [
                _mapping(value)
                for value in decoded
                if type(value) is dict and value.get("symbol") == "ETHUSDT"
            ]
            if len(matches) != 1:
                raise _ReadParseError
            item = matches[0]
        else:
            item = _mapping(decoded)
        symbol = _token(item, "symbol")
        margin_type = _token(
            item,
            "marginType",
            allowed=frozenset({"ISOLATED", "CROSSED"}),
        )
        if symbol != "ETHUSDT":
            raise _ReadParseError
        return ResponseKind.SYMBOL_CONFIG, (
            ("isAutoAddMargin", _boolean(item, "isAutoAddMargin")),
            ("leverage", _integer(item, "leverage", minimum=1)),
            ("marginType", margin_type),
            ("symbol", symbol),
        )

    @staticmethod
    def _exchange_info(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        root = _mapping(decoded)
        symbols = _list(root.get("symbols"))
        matches = [
            _mapping(value)
            for value in symbols
            if type(value) is dict and value.get("symbol") == "ETHUSDT"
        ]
        if len(matches) != 1:
            raise _ReadParseError
        symbol = matches[0]
        filters = [_mapping(value) for value in _list(symbol.get("filters"))]
        by_type: dict[str, list[dict[str, object]]] = {}
        for item in filters:
            filter_type = _token(item, "filterType")
            by_type.setdefault(filter_type, []).append(item)
        if any(len(by_type.get(name, ())) != 1 for name in _REQUIRED_FILTERS):
            raise _ReadParseError

        price = by_type["PRICE_FILTER"][0]
        limit_lot = by_type["LOT_SIZE"][0]
        market_lot = by_type["MARKET_LOT_SIZE"][0]
        notional = by_type["MIN_NOTIONAL"][0]
        percent = by_type["PERCENT_PRICE"][0]
        order_types = _list(symbol.get("orderTypes"))
        time_in_force = _list(symbol.get("timeInForce"))
        if (
            any(
                type(value) is not str or _SAFE_TOKEN.fullmatch(value) is None
                for value in order_types
            )
            or any(
                type(value) is not str or _SAFE_TOKEN.fullmatch(value) is None
                for value in time_in_force
            )
            or len(set(order_types)) != len(order_types)
            or len(set(time_in_force)) != len(time_in_force)
        ):
            raise _ReadParseError
        fields: tuple[tuple[str, object], ...] = (
            ("contractType", _token(symbol, "contractType")),
            (
                "filterTypeCounts",
                {name: len(values) for name, values in sorted(by_type.items())},
            ),
            (
                "limitLotSize",
                {
                    "maxQuantity": _decimal(limit_lot, "maxQty", positive=True),
                    "minQuantity": _decimal(limit_lot, "minQty", positive=True),
                    "stepSize": _decimal(limit_lot, "stepSize", positive=True),
                },
            ),
            ("marginAsset", _token(symbol, "marginAsset")),
            (
                "marketLotSize",
                {
                    "maxQuantity": _decimal(market_lot, "maxQty", positive=True),
                    "minQuantity": _decimal(market_lot, "minQty", positive=True),
                    "stepSize": _decimal(market_lot, "stepSize", positive=True),
                },
            ),
            ("minNotional", _decimal(notional, "notional", positive=True)),
            ("orderTypes", sorted(order_types)),
            (
                "percentPrice",
                {
                    "multiplierDown": _decimal(percent, "multiplierDown", positive=True),
                    "multiplierUp": _decimal(percent, "multiplierUp", positive=True),
                },
            ),
            (
                "priceFilter",
                {
                    "maxPrice": _decimal(price, "maxPrice", positive=True),
                    "minPrice": _decimal(price, "minPrice", positive=True),
                    "tickSize": _decimal(price, "tickSize", positive=True),
                },
            ),
            ("quoteAsset", _token(symbol, "quoteAsset")),
            ("status", _token(symbol, "status")),
            ("symbol", _token(symbol, "symbol")),
            ("timeInForce", sorted(time_in_force)),
            (
                "uninterpretedFilterTypes",
                sorted(set(by_type).difference(_REQUIRED_FILTERS)),
            ),
        )
        if dict(fields)["symbol"] != "ETHUSDT":
            raise _ReadParseError
        return ResponseKind.EXCHANGE_INFO, fields

    @staticmethod
    def _open_orders(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        orders: list[dict[str, object]] = []
        identities: set[str] = set()
        for raw in _list(decoded):
            item = _mapping(raw)
            order_id = _domain_sha256("binance-demo-order-id", item.get("orderId"))
            if order_id in identities:
                raise _ReadParseError
            identities.add(order_id)
            symbol = _token(item, "symbol")
            if symbol != "ETHUSDT":
                raise _ReadParseError
            orders.append(
                {
                    "clientOrderIdSha256": _domain_sha256(
                        "binance-demo-client-order-id",
                        _string(item, "clientOrderId"),
                    ),
                    "executedQty": _decimal(item, "executedQty", nonnegative=True),
                    "orderIdSha256": order_id,
                    "origQty": _decimal(item, "origQty", positive=True),
                    "positionSide": _token(item, "positionSide", allowed=_POSITION_SIDES),
                    "reduceOnly": _boolean(item, "reduceOnly"),
                    "side": _token(item, "side", allowed=_SIDES),
                    "status": _token(item, "status", allowed=_ORDER_STATUSES),
                    "symbol": symbol,
                    "type": _token(item, "type", allowed=_ORDER_TYPES),
                }
            )
        orders.sort(key=lambda value: str(value["orderIdSha256"]))
        return ResponseKind.OPEN_ORDERS, (("count", len(orders)), ("orders", orders))

    @staticmethod
    def _open_algo_orders(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        orders: list[dict[str, object]] = []
        identities: set[str] = set()
        for raw in _list(decoded):
            item = _mapping(raw)
            algo_id = _domain_sha256("binance-demo-algo-id", item.get("algoId"))
            if algo_id in identities:
                raise _ReadParseError
            identities.add(algo_id)
            symbol = _token(item, "symbol")
            if symbol != "ETHUSDT":
                raise _ReadParseError
            orders.append(
                {
                    "algoIdSha256": algo_id,
                    "clientAlgoIdSha256": _domain_sha256(
                        "binance-demo-client-algo-id",
                        _string(item, "clientAlgoId"),
                    ),
                    "positionSide": _token(item, "positionSide", allowed=_POSITION_SIDES),
                    "quantity": _decimal(item, "quantity", positive=True),
                    "reduceOnly": _boolean(item, "reduceOnly"),
                    "side": _token(item, "side", allowed=_SIDES),
                    "status": _token(item, "algoStatus", allowed=_ALGO_STATUSES),
                    "symbol": symbol,
                    "type": _token(item, "orderType", allowed=_ORDER_TYPES),
                }
            )
        orders.sort(key=lambda value: str(value["algoIdSha256"]))
        return ResponseKind.OPEN_ALGO_ORDERS, (("count", len(orders)), ("orders", orders))

    @staticmethod
    def _order(
        reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        client_order_id = _string(item, "clientOrderId")
        if client_order_id != dict(reservation.parameters).get("origClientOrderId"):
            raise CredentialTransportError(
                "RESPONSE_CLIENT_ID_MISMATCH",
                post_dispatch=True,
            )
        symbol = _token(item, "symbol")
        if symbol != "ETHUSDT":
            raise _ReadParseError
        return ResponseKind.ORDER_OBSERVATION, (
            ("clientOrderId", client_order_id),
            ("executedQty", _decimal(item, "executedQty", nonnegative=True)),
            (
                "orderIdSha256",
                _domain_sha256("binance-demo-order-id", item.get("orderId")),
            ),
            ("origQty", _decimal(item, "origQty", positive=True)),
            ("positionSide", _token(item, "positionSide", allowed=_POSITION_SIDES)),
            ("price", _decimal(item, "price", nonnegative=True)),
            ("reduceOnly", _boolean(item, "reduceOnly")),
            ("side", _token(item, "side", allowed=_SIDES)),
            ("status", _token(item, "status", allowed=_ORDER_STATUSES)),
            ("symbol", symbol),
            ("timeInForce", _token(item, "timeInForce")),
            ("type", _token(item, "type", allowed=_ORDER_TYPES)),
        )

    @staticmethod
    def _user_trades(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        trades: list[dict[str, object]] = []
        identities: set[str] = set()
        for raw in _list(decoded):
            item = _mapping(raw)
            if _token(item, "symbol") != "ETHUSDT":
                raise _ReadParseError
            _token(item, "commissionAsset")
            trade_id = _domain_sha256("binance-demo-trade-id", item.get("id"))
            if trade_id in identities:
                raise _ReadParseError
            identities.add(trade_id)
            trades.append(
                {
                    "commission": _decimal(item, "commission", nonnegative=True),
                    "orderIdSha256": _domain_sha256(
                        "binance-demo-order-id",
                        item.get("orderId"),
                    ),
                    "quantity": _decimal(item, "qty", positive=True),
                    "realizedPnl": _decimal(item, "realizedPnl"),
                    "tradeIdSha256": trade_id,
                }
            )
        trades.sort(key=lambda value: str(value["tradeIdSha256"]))
        return ResponseKind.USER_TRADES, (("count", len(trades)), ("trades", trades))

    @staticmethod
    def _account(
        _reservation: ReservedRequest,
        decoded: object,
    ) -> tuple[ResponseKind, tuple[tuple[str, object], ...]]:
        item = _mapping(decoded)
        balances: list[dict[str, object]] = []
        asset_names: set[str] = set()
        for raw in _list(item.get("assets")):
            asset = _mapping(raw)
            name = _token(asset, "asset")
            if _ASSET.fullmatch(name) is None or name in asset_names:
                raise _ReadParseError
            asset_names.add(name)
            balances.append(
                {
                    "asset": name,
                    "availableBalance": _decimal(
                        asset,
                        "availableBalance",
                        nonnegative=True,
                    ),
                    "walletBalance": _decimal(asset, "walletBalance", nonnegative=True),
                }
            )
        if "USDT" not in asset_names:
            raise _ReadParseError
        balances.sort(key=lambda value: str(value["asset"]))

        positions: list[dict[str, object]] = []
        position_keys: set[tuple[str, str]] = set()
        for raw in _list(item.get("positions")):
            position = _mapping(raw)
            symbol = _token(position, "symbol")
            side = _token(position, "positionSide", allowed=_POSITION_SIDES)
            key = (symbol, side)
            if key in position_keys:
                raise _ReadParseError
            position_keys.add(key)
            quantity = _decimal(position, "positionAmt")
            if Decimal(quantity) != 0:
                positions.append(
                    {
                        "positionAmt": quantity,
                        "positionSide": side,
                        "symbol": symbol,
                    }
                )
        positions.sort(key=lambda value: (str(value["symbol"]), str(value["positionSide"])))
        return ResponseKind.ACCOUNT, (
            ("balances", balances),
            ("canTrade", _boolean(item, "canTrade")),
            ("multiAssetsMargin", _boolean(item, "multiAssetsMargin")),
            ("nonzeroPositions", positions),
        )

    @staticmethod
    def _raise_invalid_response(reservation: ReservedRequest) -> None:
        if reservation.purpose is RequestPurpose.READ:
            raise CredentialTransportError("READ_RESPONSE_INVALID", post_dispatch=True)
        raise CredentialTransportError(
            "POST_DISPATCH_RESPONSE_INVALID",
            post_dispatch=True,
        )
