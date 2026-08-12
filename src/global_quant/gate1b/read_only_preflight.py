"""Isolated authenticated read-only Binance Demo preflight capability.

This module intentionally imports no authorization, intent, execution, or
order-lifecycle component.  Its signed leaf can issue only ``HttpMethod.GET``
and the public transport adds an exact origin, path, symbol, and parameter
allowlist before the signed client is reached.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.adapters.binance.http.error import BinanceError
from nautilus_trader.common.component import LiveClock
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

from global_quant.gate1b.credential_http import (
    _REQUEST_TIMEOUT_SECONDS,
    _install_redirect_safe_client,
    _RedirectSafeHttpClient,
)
from global_quant.gate1b.safety import (
    DemoCredentials,
    resolve_demo_endpoints,
    validate_demo_endpoints,
)

PROTOCOL_VERSION = "1.10"
DEMO_HTTP_ORIGIN = "https://demo-fapi.binance.com"
SYMBOL = "ETHUSDT"
POSITION_RISK_CONTROL_STATUS = "UNRESOLVED_SAFE_BLOCK"
MAX_NUM_ORDERS_STATUS = "AUTHENTICATED_STATE_AVAILABLE_NOT_EVALUATED"


class ReadOnlyPreflightError(RuntimeError):
    """Fail-closed read-only capability error with a non-secret reason."""

    def __init__(
        self,
        reason: str,
        *,
        diagnostic: SafeReadOnlyDiagnostic | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnostic = diagnostic


class DiagnosticCategory(StrEnum):
    """Complete v1.10 failure-category output allowlist."""

    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    TIMESTAMP_OR_CLOCK_SKEW = "TIMESTAMP_OR_CLOCK_SKEW"
    API_PERMISSION_INSUFFICIENT = "API_PERMISSION_INSUFFICIENT"
    BINANCE_API_ERROR = "BINANCE_API_ERROR"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    TLS_FAILURE = "TLS_FAILURE"
    HTTP_FAILURE = "HTTP_FAILURE"
    RESPONSE_VALIDATION_FAILED = "RESPONSE_VALIDATION_FAILED"
    LOCAL_INPUT_FAILURE = "LOCAL_INPUT_FAILURE"
    OTHER_SAFE_ERROR = "OTHER_SAFE_ERROR"


class DiagnosticStage(StrEnum):
    """Complete v1.10 failure-stage output allowlist."""

    CREDENTIAL_INPUT = "CREDENTIAL_INPUT"
    SYMBOL_CONFIG_REQUEST = "SYMBOL_CONFIG_REQUEST"
    SYMBOL_CONFIG_RESPONSE = "SYMBOL_CONFIG_RESPONSE"
    OPEN_ORDERS_REQUEST = "OPEN_ORDERS_REQUEST"
    OPEN_ORDERS_RESPONSE = "OPEN_ORDERS_RESPONSE"
    POSITION_RISK_REQUEST = "POSITION_RISK_REQUEST"
    POSITION_RISK_RESPONSE = "POSITION_RISK_RESPONSE"


class ReadOnlyEndpoint(StrEnum):
    """The unchanged authenticated endpoint allowlist for protocol v1.10."""

    SYMBOL_CONFIGURATION = "/fapi/v1/symbolConfig"
    OPEN_ORDERS = "/fapi/v1/openOrders"
    POSITION_STATE = "/fapi/v3/positionRisk"


_ENDPOINT_SYMBOLS = {
    ReadOnlyEndpoint.SYMBOL_CONFIGURATION: "SYMBOL_CONFIG",
    ReadOnlyEndpoint.OPEN_ORDERS: "OPEN_ORDERS",
    ReadOnlyEndpoint.POSITION_STATE: "POSITION_RISK",
}
_REQUEST_STAGES = {
    ReadOnlyEndpoint.SYMBOL_CONFIGURATION: DiagnosticStage.SYMBOL_CONFIG_REQUEST,
    ReadOnlyEndpoint.OPEN_ORDERS: DiagnosticStage.OPEN_ORDERS_REQUEST,
    ReadOnlyEndpoint.POSITION_STATE: DiagnosticStage.POSITION_RISK_REQUEST,
}
_RESPONSE_STAGES = {
    ReadOnlyEndpoint.SYMBOL_CONFIGURATION: DiagnosticStage.SYMBOL_CONFIG_RESPONSE,
    ReadOnlyEndpoint.OPEN_ORDERS: DiagnosticStage.OPEN_ORDERS_RESPONSE,
    ReadOnlyEndpoint.POSITION_STATE: DiagnosticStage.POSITION_RISK_RESPONSE,
}


@dataclass(frozen=True, slots=True)
class SafeReadOnlyDiagnostic:
    """Strictly allowlisted diagnostic data; never retains a raw exception."""

    stage: DiagnosticStage
    category: DiagnosticCategory
    endpoint: ReadOnlyEndpoint | None = None
    http_status: int | None = None
    binance_code: int | None = None

    def __post_init__(self) -> None:
        if type(self.stage) is not DiagnosticStage or type(self.category) is not DiagnosticCategory:
            raise ValueError("SAFE_DIAGNOSTIC_INVALID")
        if self.endpoint is not None and type(self.endpoint) is not ReadOnlyEndpoint:
            raise ValueError("SAFE_DIAGNOSTIC_INVALID")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("SAFE_DIAGNOSTIC_INVALID")
        if self.binance_code is not None and (
            type(self.binance_code) is not int or abs(self.binance_code) > 999_999_999
        ):
            raise ValueError("SAFE_DIAGNOSTIC_INVALID")

    def to_stop_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "status": "STOP",
            "stage": self.stage.value,
            "category": self.category.value,
        }
        if self.endpoint is not None:
            payload["endpoint"] = _ENDPOINT_SYMBOLS[self.endpoint]
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.binance_code is not None:
            payload["binance_code"] = self.binance_code
        return payload


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return a bounded type-inspection chain without rendering any exception."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(chain) < 8 and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        nested = current.__cause__
        if nested is None and isinstance(current, urllib.error.URLError):
            reason = current.reason
            nested = reason if isinstance(reason, BaseException) else None
        if nested is None:
            nested = current.__context__
        current = nested
    return tuple(chain)


def _safe_binance_fields(error: BinanceError) -> tuple[int | None, int | None]:
    status = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
    message = error.message
    code: int | None = None
    if type(message) is dict:
        candidate = message.get("code")
        if type(candidate) is int and abs(candidate) <= 999_999_999:
            code = candidate
    return status, code


def _request_failure_diagnostic(
    endpoint: ReadOnlyEndpoint,
    error: BaseException,
) -> SafeReadOnlyDiagnostic:
    chain = _exception_chain(error)
    for candidate in chain:
        if isinstance(candidate, BinanceError):
            http_status, binance_code = _safe_binance_fields(candidate)
            category = {
                -1002: DiagnosticCategory.AUTHENTICATION_FAILED,
                -1021: DiagnosticCategory.TIMESTAMP_OR_CLOCK_SKEW,
                -1022: DiagnosticCategory.SIGNATURE_INVALID,
                -2014: DiagnosticCategory.AUTHENTICATION_FAILED,
                -2015: DiagnosticCategory.AUTHENTICATION_FAILED,
            }.get(binance_code)
            if category is None:
                category = (
                    DiagnosticCategory.BINANCE_API_ERROR
                    if binance_code is not None
                    else DiagnosticCategory.HTTP_FAILURE
                )
            return SafeReadOnlyDiagnostic(
                stage=_REQUEST_STAGES[endpoint],
                category=category,
                endpoint=endpoint,
                http_status=http_status,
                binance_code=binance_code,
            )
    if any(isinstance(candidate, ssl.SSLError | ssl.CertificateError) for candidate in chain):
        category = DiagnosticCategory.TLS_FAILURE
    elif any(isinstance(candidate, urllib.error.HTTPError) for candidate in chain):
        category = DiagnosticCategory.HTTP_FAILURE
    elif any(
        isinstance(candidate, TimeoutError | ConnectionError | urllib.error.URLError | OSError)
        for candidate in chain
    ):
        category = DiagnosticCategory.NETWORK_FAILURE
    else:
        category = DiagnosticCategory.OTHER_SAFE_ERROR
    return SafeReadOnlyDiagnostic(
        stage=_REQUEST_STAGES[endpoint],
        category=category,
        endpoint=endpoint,
    )


def _response_failure(endpoint: ReadOnlyEndpoint) -> ReadOnlyPreflightError:
    return ReadOnlyPreflightError(
        "READ_RESPONSE_INVALID",
        diagnostic=SafeReadOnlyDiagnostic(
            stage=_RESPONSE_STAGES[endpoint],
            category=DiagnosticCategory.RESPONSE_VALIDATION_FAILED,
            endpoint=endpoint,
        ),
    )


_ALLOWED_PATHS = frozenset(endpoint.value for endpoint in ReadOnlyEndpoint)
_ALLOWED_PARAMETERS = {"symbol": SYMBOL}
_SAFE_TOKEN = re.compile(r"[A-Z0-9_]{1,64}\Z")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    return value


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    return value


def _token(value: object, *, allowed: frozenset[str] | None = None) -> str:
    if type(value) is not str or _SAFE_TOKEN.fullmatch(value) is None:
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    if allowed is not None and value not in allowed:
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    return value


def _decimal(value: object) -> str:
    if type(value) is not str:
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID") from exc
    if not parsed.is_finite():
        raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
    return str(parsed)


@dataclass(frozen=True, slots=True)
class ReadOnlyResult:
    """Sanitized account state from one exact allowlisted GET."""

    endpoint: ReadOnlyEndpoint
    fields: tuple[tuple[str, object], ...]

    def to_mapping(self) -> dict[str, object]:
        return {"endpoint": self.endpoint.value, "fields": dict(self.fields)}


class _GetOnlySignedClient(Protocol):
    @property
    def base_url(self) -> str: ...

    def get(
        self,
        path: str,
        parameters: dict[str, str],
        *,
        absolute_deadline_ns: int,
    ) -> bytes: ...

    def close(self) -> None: ...


class _ExistingDemoGetOnlySignedClient:
    """Demo signer whose code surface can emit authenticated GET only."""

    __slots__ = ("__closed", "__http_client", "__loop")

    def __init__(self, http_client: object) -> None:
        if getattr(http_client, "base_url", None) != DEMO_HTTP_ORIGIN:
            raise ReadOnlyPreflightError("DEMO_HTTP_ORIGIN_MISMATCH")
        _install_redirect_safe_client(http_client)
        self.__http_client = http_client
        self.__loop = asyncio.new_event_loop()
        self.__closed = False

    @property
    def base_url(self) -> str:
        return str(self.__http_client.base_url)

    def get(
        self,
        path: str,
        parameters: dict[str, str],
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        if self.__closed:
            raise ReadOnlyPreflightError("READ_ONLY_TRANSPORT_CLOSED")
        if type(absolute_deadline_ns) is not int:
            raise ReadOnlyPreflightError("ABSOLUTE_DEADLINE_REQUIRED")
        remaining_seconds = min(
            _REQUEST_TIMEOUT_SECONDS,
            (absolute_deadline_ns - time.monotonic_ns()) / 1_000_000_000,
        )
        if remaining_seconds <= 0:
            raise ReadOnlyPreflightError("ABSOLUTE_DEADLINE_EXHAUSTED")
        client = getattr(self.__http_client, "_client", None)
        if not isinstance(client, _RedirectSafeHttpClient):
            raise ReadOnlyPreflightError("REDIRECT_SAFE_CLIENT_REQUIRED")
        client.authorize_absolute_deadline(absolute_deadline_ns)
        try:
            return self.__loop.run_until_complete(
                asyncio.wait_for(
                    self.__http_client.sign_request(
                        HttpMethod.GET,
                        path,
                        dict(parameters),
                    ),
                    timeout=remaining_seconds,
                )
            )
        except BaseException as exc:
            raise ReadOnlyPreflightError(
                "READ_ONLY_IO_FAILED",
                diagnostic=_request_failure_diagnostic(ReadOnlyEndpoint(path), exc),
            ) from None
        finally:
            client.cancel_absolute_deadline(absolute_deadline_ns)

    def close(self) -> None:
        if self.__closed:
            return
        self.__closed = True
        if not self.__loop.is_closed():
            self.__loop.close()
        client = getattr(self.__http_client, "_client", None)
        if isinstance(client, _RedirectSafeHttpClient):
            client.close()


_TRANSPORT_CONSTRUCTION_TOKEN = object()


class AuthenticatedReadOnlyPreflightTransport:
    """Exact Demo GET allowlist with no mutation/order-lifecycle dependency."""

    __slots__ = ("__closed", "__signed_client")

    def __init__(
        self,
        signed_client: _GetOnlySignedClient,
        *,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _TRANSPORT_CONSTRUCTION_TOKEN:
            raise ReadOnlyPreflightError("READ_ONLY_TRANSPORT_CONSTRUCTION_FORBIDDEN")
        if getattr(signed_client, "base_url", None) != DEMO_HTTP_ORIGIN:
            raise ReadOnlyPreflightError("DEMO_HTTP_ORIGIN_MISMATCH")
        self.__signed_client = signed_client
        self.__closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(origin={DEMO_HTTP_ORIGIN!r}, closed={self.__closed})"

    def close(self) -> None:
        if self.__closed:
            return
        self.__closed = True
        self.__signed_client.close()

    def request(
        self,
        *,
        method: str,
        origin: str,
        path: str,
        parameters: Mapping[str, str],
        absolute_deadline_ns: int,
    ) -> ReadOnlyResult:
        """Execute one exact request after every safety check passes."""

        if self.__closed:
            raise ReadOnlyPreflightError("READ_ONLY_TRANSPORT_CLOSED")
        if type(method) is not str or method != "GET":
            raise ReadOnlyPreflightError("MUTATION_METHOD_FORBIDDEN")
        if type(origin) is not str or origin != DEMO_HTTP_ORIGIN:
            raise ReadOnlyPreflightError("DEMO_HTTP_ORIGIN_MISMATCH")
        if type(path) is not str or path not in _ALLOWED_PATHS:
            raise ReadOnlyPreflightError("READ_ENDPOINT_NOT_ALLOWLISTED")
        if type(parameters) is not dict or parameters != _ALLOWED_PARAMETERS:
            raise ReadOnlyPreflightError("READ_PARAMETERS_NOT_ALLOWLISTED")
        if type(absolute_deadline_ns) is not int or absolute_deadline_ns <= time.monotonic_ns():
            raise ReadOnlyPreflightError("ABSOLUTE_DEADLINE_EXHAUSTED")

        endpoint = ReadOnlyEndpoint(path)
        try:
            raw = self.__signed_client.get(
                path,
                dict(parameters),
                absolute_deadline_ns=absolute_deadline_ns,
            )
        except ReadOnlyPreflightError as exc:
            if exc.diagnostic is not None:
                raise
            raise ReadOnlyPreflightError(
                "READ_ONLY_IO_FAILED",
                diagnostic=_request_failure_diagnostic(endpoint, exc),
            ) from None
        except BaseException as exc:
            raise ReadOnlyPreflightError(
                "READ_ONLY_IO_FAILED",
                diagnostic=_request_failure_diagnostic(endpoint, exc),
            ) from None
        if type(raw) is not bytes:
            raise _response_failure(endpoint)
        try:
            decoded = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            return self._sanitize(endpoint, decoded)
        except (UnicodeDecodeError, ValueError, ReadOnlyPreflightError):
            raise _response_failure(endpoint) from None

    def run_fixed_preflight(
        self,
        *,
        deadline_factory: Callable[[], int] | None = None,
    ) -> tuple[ReadOnlyResult, ...]:
        """Run the unchanged fixed GET capability without authorizing an order."""

        make_deadline = deadline_factory or (
            lambda: time.monotonic_ns() + int(_REQUEST_TIMEOUT_SECONDS * 1_000_000_000)
        )
        return tuple(
            self.request(
                method="GET",
                origin=DEMO_HTTP_ORIGIN,
                path=endpoint.value,
                parameters={"symbol": SYMBOL},
                absolute_deadline_ns=make_deadline(),
            )
            for endpoint in ReadOnlyEndpoint
        )

    @staticmethod
    def _sanitize(endpoint: ReadOnlyEndpoint, decoded: object) -> ReadOnlyResult:
        if endpoint is ReadOnlyEndpoint.SYMBOL_CONFIGURATION:
            matches = [
                _mapping(value)
                for value in _list(decoded)
                if type(value) is dict and value.get("symbol") == SYMBOL
            ]
            if len(matches) != 1:
                raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
            item = matches[0]
            margin_type = _token(item.get("marginType"), allowed=frozenset({"CROSSED", "ISOLATED"}))
            auto_add = item.get("isAutoAddMargin")
            leverage = item.get("leverage")
            if type(auto_add) is not bool or type(leverage) is not int or leverage < 1:
                raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
            return ReadOnlyResult(
                endpoint=endpoint,
                fields=(
                    ("autoAddMargin", auto_add),
                    ("isolated", margin_type == "ISOLATED"),
                    ("leverage", leverage),
                    ("marginType", margin_type),
                    ("symbol", SYMBOL),
                ),
            )

        if endpoint is ReadOnlyEndpoint.OPEN_ORDERS:
            orders = _list(decoded)
            for raw_order in orders:
                order = _mapping(raw_order)
                if _token(order.get("symbol")) != SYMBOL:
                    raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
            return ReadOnlyResult(
                endpoint=endpoint,
                fields=(
                    ("maxNumOrdersStatus", MAX_NUM_ORDERS_STATUS),
                    ("openOrderCount", len(orders)),
                    ("symbol", SYMBOL),
                ),
            )

        positions: list[dict[str, object]] = []
        seen_sides: set[str] = set()
        for raw_position in _list(decoded):
            position = _mapping(raw_position)
            if _token(position.get("symbol")) != SYMBOL:
                raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
            side = _token(
                position.get("positionSide"),
                allowed=frozenset({"BOTH", "LONG", "SHORT"}),
            )
            if side in seen_sides:
                raise ReadOnlyPreflightError("READ_RESPONSE_INVALID")
            seen_sides.add(side)
            positions.append({"positionAmt": _decimal(position.get("positionAmt")), "side": side})
        positions.sort(key=lambda value: str(value["side"]))
        return ReadOnlyResult(
            endpoint=endpoint,
            fields=(
                ("positionRiskControl", POSITION_RISK_CONTROL_STATUS),
                ("positions", positions),
                ("symbol", SYMBOL),
            ),
        )


def build_authenticated_read_only_transport(
    credentials: DemoCredentials,
) -> AuthenticatedReadOnlyPreflightTransport:
    """Build the dedicated Demo GET-only stack without a Production fallback."""

    if type(credentials) is not DemoCredentials:
        raise ReadOnlyPreflightError("DEMO_CREDENTIALS_REQUIRED")
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
    except BaseException:
        raise ReadOnlyPreflightError("DEMO_READ_ONLY_STACK_BUILD_FAILED") from None
    if getattr(http_client, "base_url", None) != DEMO_HTTP_ORIGIN:
        raise ReadOnlyPreflightError("DEMO_HTTP_ORIGIN_MISMATCH")
    signed_client: _ExistingDemoGetOnlySignedClient | None = None
    try:
        signed_client = _ExistingDemoGetOnlySignedClient(http_client)
        return AuthenticatedReadOnlyPreflightTransport(
            signed_client,
            _construction_token=_TRANSPORT_CONSTRUCTION_TOKEN,
        )
    except BaseException:
        if signed_client is not None:
            signed_client.close()
        raise
