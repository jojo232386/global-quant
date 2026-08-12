from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import global_quant.gate1b.read_only_preflight as read_only_module
from global_quant.gate1b.read_only_preflight import (
    DEMO_HTTP_ORIGIN,
    MAX_NUM_ORDERS_STATUS,
    POSITION_RISK_CONTROL_STATUS,
    AuthenticatedReadOnlyPreflightTransport,
    ReadOnlyEndpoint,
    ReadOnlyPreflightError,
)
from global_quant.gate1b.read_only_preflight_cli import (
    main,
    run_prompted_read_only_preflight,
)

_DEADLINE_NS = 10**30
_API_KEY = "synthetic-demo-api-key-canary"
_API_SECRET = "synthetic-demo-api-secret-canary"


class FakeGetOnlySignedClient:
    def __init__(
        self,
        responses: list[bytes | BaseException],
        *,
        base_url: str = DEMO_HTTP_ORIGIN,
    ) -> None:
        self.base_url = base_url
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], int]] = []
        self.closed = False

    def get(
        self,
        path: str,
        parameters: dict[str, str],
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        self.calls.append((path, parameters, absolute_deadline_ns))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _transport(client: FakeGetOnlySignedClient) -> AuthenticatedReadOnlyPreflightTransport:
    return AuthenticatedReadOnlyPreflightTransport(
        client,
        _construction_token=read_only_module._TRANSPORT_CONSTRUCTION_TOKEN,
    )


def _request(
    transport: AuthenticatedReadOnlyPreflightTransport,
    *,
    method: str = "GET",
    origin: str = DEMO_HTTP_ORIGIN,
    path: str = ReadOnlyEndpoint.SYMBOL_CONFIGURATION.value,
    parameters: dict[str, str] | None = None,
):
    return transport.request(
        method=method,
        origin=origin,
        path=path,
        parameters={"symbol": "ETHUSDT"} if parameters is None else parameters,
        absolute_deadline_ns=_DEADLINE_NS,
    )


def test_allowed_authenticated_demo_get_is_sanitized() -> None:
    client = FakeGetOnlySignedClient(
        [
            json.dumps(
                [
                    {
                        "symbol": "ETHUSDT",
                        "marginType": "ISOLATED",
                        "isAutoAddMargin": False,
                        "leverage": 1,
                        "maxNotionalValue": "1000000",
                    }
                ]
            ).encode()
        ]
    )
    result = _request(_transport(client))

    assert result.endpoint is ReadOnlyEndpoint.SYMBOL_CONFIGURATION
    assert dict(result.fields) == {
        "autoAddMargin": False,
        "isolated": True,
        "leverage": 1,
        "marginType": "ISOLATED",
        "symbol": "ETHUSDT",
    }
    assert client.calls == [("/fapi/v1/symbolConfig", {"symbol": "ETHUSDT"}, _DEADLINE_NS)]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "post", "PATCH"])
def test_mutation_methods_are_denied_before_network(method: str) -> None:
    client = FakeGetOnlySignedClient([])
    transport = _transport(client)

    with pytest.raises(ReadOnlyPreflightError, match="MUTATION_METHOD_FORBIDDEN"):
        _request(transport, method=method)

    assert client.calls == []


def test_non_allowlisted_get_is_denied_before_network() -> None:
    client = FakeGetOnlySignedClient([])
    with pytest.raises(ReadOnlyPreflightError, match="READ_ENDPOINT_NOT_ALLOWLISTED"):
        _request(_transport(client), path="/fapi/v1/order")
    assert client.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://fapi.binance.com",
        "https://testnet.binancefuture.com",
        "https://demo-fapi.binance.com.evil.example",
    ],
)
def test_production_and_non_demo_origins_are_denied_before_network(origin: str) -> None:
    client = FakeGetOnlySignedClient([])
    with pytest.raises(ReadOnlyPreflightError, match="DEMO_HTTP_ORIGIN_MISMATCH"):
        _request(_transport(client), origin=origin)
    assert client.calls == []


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"symbol": "BTCUSDT"},
        {"symbol": "ETHUSDT", "recvWindow": "60000"},
    ],
)
def test_parameters_are_exactly_allowlisted(parameters: dict[str, str]) -> None:
    client = FakeGetOnlySignedClient([])
    with pytest.raises(ReadOnlyPreflightError, match="READ_PARAMETERS_NOT_ALLOWLISTED"):
        _request(_transport(client), parameters=parameters)
    assert client.calls == []


def test_fixed_preflight_exposes_state_without_authorizing_orders() -> None:
    client = FakeGetOnlySignedClient(
        [
            b'[{"symbol":"ETHUSDT","marginType":"ISOLATED","isAutoAddMargin":false,"leverage":1}]',
            b'[{"symbol":"ETHUSDT"},{"symbol":"ETHUSDT"}]',
            b'[{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"0"}]',
        ]
    )
    results = _transport(client).run_fixed_preflight(deadline_factory=lambda: _DEADLINE_NS)

    assert tuple(result.endpoint for result in results) == tuple(ReadOnlyEndpoint)
    assert dict(results[1].fields) == {
        "maxNumOrdersStatus": MAX_NUM_ORDERS_STATUS,
        "openOrderCount": 2,
        "symbol": "ETHUSDT",
    }
    assert dict(results[2].fields) == {
        "positionRiskControl": POSITION_RISK_CONTROL_STATUS,
        "positions": [{"positionAmt": "0", "side": "BOTH"}],
        "symbol": "ETHUSDT",
    }
    assert all(call[1] == {"symbol": "ETHUSDT"} for call in client.calls)


def test_read_only_module_has_no_mutation_lifecycle_dependency() -> None:
    source = Path(read_only_module.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "AuthorizationRecord",
        "execute_primary",
        "credential_execution_session",
        "mutation_protocol",
        "ReservedRequest",
    ):
        assert forbidden not in source
    assert "HttpMethod.POST" not in source
    assert "HttpMethod.PUT" not in source
    assert "HttpMethod.DELETE" not in source


def test_prompted_entrypoint_never_prints_credentials_signature_or_exception(capsys) -> None:
    prompts = iter(("hmac", _API_KEY, _API_SECRET))

    def prompt(_label: str) -> str:
        return next(prompts)

    def fail_builder(_credentials):
        raise RuntimeError(f"{_API_KEY} signature={_API_SECRET}")

    assert (
        run_prompted_read_only_preflight(
            prompt_secret=prompt,
            environ={},
            input_is_tty=True,
            core_dump_guard=lambda: None,
            transport_builder=fail_builder,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert _API_KEY not in output
    assert _API_SECRET not in output
    assert "signature" not in output.lower()
    assert json.loads(output)["reason"] == "AUTHENTICATED_READ_ONLY_PREFLIGHT_FAILED"


def test_cli_requires_exact_demo_only_arming_before_prompt_or_network(capsys) -> None:
    assert main([]) == 1
    assert main(["--confirm-demo-only", "--origin=https://fapi.binance.com"]) == 1
    assert capsys.readouterr().out.count('"status": "STOP"') == 2


def test_constructor_rejects_production_signer() -> None:
    client = FakeGetOnlySignedClient([], base_url="https://fapi.binance.com")
    with pytest.raises(ReadOnlyPreflightError, match="DEMO_HTTP_ORIGIN_MISMATCH"):
        _transport(client)


def test_expired_deadline_is_denied_before_network() -> None:
    client = FakeGetOnlySignedClient([])
    with pytest.raises(ReadOnlyPreflightError, match="ABSOLUTE_DEADLINE_EXHAUSTED"):
        _transport(client).request(
            method="GET",
            origin=DEMO_HTTP_ORIGIN,
            path=ReadOnlyEndpoint.OPEN_ORDERS.value,
            parameters={"symbol": "ETHUSDT"},
            absolute_deadline_ns=time.monotonic_ns() - 1,
        )
    assert client.calls == []
