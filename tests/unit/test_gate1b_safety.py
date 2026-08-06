from __future__ import annotations

import socket

import pytest

from global_quant.gate1b.safety import DemoCredentialError
from global_quant.gate1b.safety import DemoCredentials
from global_quant.gate1b.safety import EndpointContractError
from global_quant.gate1b.safety import assert_secret_free
from global_quant.gate1b.safety import load_demo_credentials
from global_quant.gate1b.safety import resolve_demo_endpoints
from global_quant.gate1b.safety import validate_demo_endpoints


def test_missing_demo_credentials_fail_before_network(monkeypatch) -> None:
    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("network was attempted before credential validation")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)

    with pytest.raises(DemoCredentialError, match="MISSING_DEMO_CREDENTIALS"):
        load_demo_credentials({}, confirm_demo_only=True)


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_FUTURES_TESTNET_API_KEY",
        "BINANCE_FUTURES_TESTNET_API_SECRET",
    ],
)
def test_live_or_testnet_credentials_block_demo(forbidden_name) -> None:
    environment = {
        "BINANCE_DEMO_API_KEY": "demo-key-test-only",
        "BINANCE_DEMO_API_SECRET": "demo-secret-test-only",
        forbidden_name: "must-never-be-read",
    }

    with pytest.raises(DemoCredentialError, match="CONFLICTING_CREDENTIAL_SCOPE"):
        load_demo_credentials(environment, confirm_demo_only=True)


def test_explicit_demo_arming_is_required() -> None:
    environment = {
        "BINANCE_DEMO_API_KEY": "demo-key-test-only",
        "BINANCE_DEMO_API_SECRET": "demo-secret-test-only",
    }

    with pytest.raises(DemoCredentialError, match="DEMO_ARMING_REQUIRED"):
        load_demo_credentials(environment, confirm_demo_only=False)


def test_credentials_never_render_or_hash() -> None:
    credentials = DemoCredentials(
        api_key="demo-key-test-only",
        api_secret="demo-secret-test-only",
    )

    rendered = repr(credentials)
    assert "demo-key-test-only" not in rendered
    assert "demo-secret-test-only" not in rendered
    assert "redacted" in rendered.lower()
    assert_secret_free("safe evidence", credentials)

    with pytest.raises(DemoCredentialError, match="SECRET_IN_EVIDENCE"):
        assert_secret_free("contains demo-key-test-only", credentials)


def test_pinned_adapter_resolves_only_frozen_demo_endpoints() -> None:
    endpoints = resolve_demo_endpoints()

    assert endpoints.http == "https://demo-fapi.binance.com"
    assert endpoints.stream == "wss://demo-fstream.binance.com"
    assert endpoints.ws_api == "wss://testnet.binancefuture.com/ws-fapi/v1"
    validate_demo_endpoints(endpoints)


def test_endpoint_contract_rejects_production_or_broad_testnet() -> None:
    endpoints = resolve_demo_endpoints()

    with pytest.raises(EndpointContractError, match="DEMO_ENDPOINT_MISMATCH"):
        validate_demo_endpoints(
            endpoints.__class__(
                http="https://fapi.binance.com",
                stream=endpoints.stream,
                ws_api=endpoints.ws_api,
            ),
        )

    with pytest.raises(EndpointContractError, match="DEMO_ENDPOINT_MISMATCH"):
        validate_demo_endpoints(
            endpoints.__class__(
                http=endpoints.http,
                stream=endpoints.stream,
                ws_api="wss://testnet.binancefuture.com/ws-fapi/v2",
            ),
        )
