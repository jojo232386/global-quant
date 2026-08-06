from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.common.urls import get_http_base_url
from nautilus_trader.adapters.binance.common.urls import get_ws_api_base_url
from nautilus_trader.adapters.binance.common.urls import get_ws_private_base_url


DEMO_KEY_NAME = "BINANCE_DEMO_API_KEY"
DEMO_SECRET_NAME = "BINANCE_DEMO_API_SECRET"
CONFLICTING_CREDENTIAL_NAMES = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BINANCE_FUTURES_TESTNET_API_KEY",
    "BINANCE_FUTURES_TESTNET_API_SECRET",
)


class DemoCredentialError(RuntimeError):
    """Raised before network access when the credential boundary is unsafe."""


class EndpointContractError(RuntimeError):
    """Raised when the pinned adapter no longer resolves the frozen endpoints."""


@dataclass(frozen=True, repr=False, eq=False)
class DemoCredentials:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:
        return "DemoCredentials(api_key=<redacted>, api_secret=<redacted>)"


@dataclass(frozen=True)
class DemoEndpoints:
    http: str
    stream: str
    ws_api: str


EXPECTED_DEMO_ENDPOINTS = DemoEndpoints(
    http="https://demo-fapi.binance.com",
    stream="wss://demo-fstream.binance.com",
    ws_api="wss://testnet.binancefuture.com/ws-fapi/v1",
)


def load_demo_credentials(
    environ: Mapping[str, str],
    *,
    confirm_demo_only: bool,
) -> DemoCredentials:
    conflicting = [name for name in CONFLICTING_CREDENTIAL_NAMES if name in environ]
    if conflicting:
        joined = ",".join(sorted(conflicting))
        raise DemoCredentialError(f"CONFLICTING_CREDENTIAL_SCOPE:{joined}")
    if not confirm_demo_only:
        raise DemoCredentialError("DEMO_ARMING_REQUIRED")
    api_key = environ.get(DEMO_KEY_NAME, "")
    api_secret = environ.get(DEMO_SECRET_NAME, "")
    if not api_key or not api_secret:
        raise DemoCredentialError("MISSING_DEMO_CREDENTIALS")
    return DemoCredentials(api_key=api_key, api_secret=api_secret)


def resolve_demo_endpoints() -> DemoEndpoints:
    account_type = BinanceAccountType.USDT_FUTURES
    environment = BinanceEnvironment.DEMO
    return DemoEndpoints(
        http=get_http_base_url(account_type, environment, False),
        stream=get_ws_private_base_url(account_type, environment, False),
        ws_api=get_ws_api_base_url(account_type, environment, False),
    )


def validate_demo_endpoints(endpoints: DemoEndpoints) -> None:
    if endpoints != EXPECTED_DEMO_ENDPOINTS:
        raise EndpointContractError(
            "DEMO_ENDPOINT_MISMATCH:"
            f"expected={EXPECTED_DEMO_ENDPOINTS!r}:actual={endpoints!r}",
        )


def assert_secret_free(text: str, credentials: DemoCredentials) -> None:
    if credentials.api_key in text or credentials.api_secret in text:
        raise DemoCredentialError("SECRET_IN_EVIDENCE")

