from __future__ import annotations

import json
import ssl
import urllib.error

import pytest
from nautilus_trader.adapters.binance.http.error import (
    BinanceClientError,
    BinanceServerError,
)

import global_quant.gate1b.read_only_preflight as read_only_module
from global_quant.gate1b.read_only_preflight import (
    DEMO_HTTP_ORIGIN,
    AuthenticatedReadOnlyPreflightTransport,
    ReadOnlyEndpoint,
    ReadOnlyPreflightError,
)
from global_quant.gate1b.read_only_preflight_cli import run_prompted_read_only_preflight

_DEADLINE_NS = 10**30
_API_KEY = "synthetic-demo-api-key-canary"
_API_SECRET = "synthetic-demo-api-secret-canary"
_SIGNATURE = "synthetic-signature-canary"
_SIGNED_URL = (
    "https://demo-fapi.binance.com/fapi/v1/symbolConfig?"
    f"symbol=ETHUSDT&signature={_SIGNATURE}&secret={_API_SECRET}"
)


class FakeGetOnlySignedClient:
    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self.base_url = DEMO_HTTP_ORIGIN
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(
        self,
        path: str,
        parameters: dict[str, str],
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        assert parameters == {"symbol": "ETHUSDT"}
        assert type(absolute_deadline_ns) is int
        self.calls.append(path)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        return None


def _transport(client: FakeGetOnlySignedClient) -> AuthenticatedReadOnlyPreflightTransport:
    return AuthenticatedReadOnlyPreflightTransport(
        client,
        _construction_token=read_only_module._TRANSPORT_CONSTRUCTION_TOKEN,
    )


def _request(
    client: FakeGetOnlySignedClient,
    endpoint: ReadOnlyEndpoint = ReadOnlyEndpoint.SYMBOL_CONFIGURATION,
) -> None:
    _transport(client).request(
        method="GET",
        origin=DEMO_HTTP_ORIGIN,
        path=endpoint.value,
        parameters={"symbol": "ETHUSDT"},
        absolute_deadline_ns=_DEADLINE_NS,
    )


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (-1021, "TIMESTAMP_OR_CLOCK_SKEW"),
        (-1022, "SIGNATURE_INVALID"),
        (-987654, "BINANCE_API_ERROR"),
    ],
)
def test_binance_error_exposes_only_structured_numeric_diagnostics(
    code: int,
    category: str,
) -> None:
    client = FakeGetOnlySignedClient(
        [
            BinanceClientError(
                status=400,
                message={"code": code, "msg": _SIGNED_URL},
                headers={"X-MBX-APIKEY": _API_KEY},
            )
        ]
    )

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client)

    payload = caught.value.diagnostic.to_stop_payload()  # type: ignore[union-attr]
    assert payload == {
        "binance_code": code,
        "category": category,
        "endpoint": "SYMBOL_CONFIG",
        "http_status": 400,
        "protocol_version": "1.11",
        "stage": "SYMBOL_CONFIG_REQUEST",
        "status": "STOP",
    }
    encoded = json.dumps(payload)
    for forbidden in (_API_KEY, _API_SECRET, _SIGNATURE, _SIGNED_URL, "X-MBX-APIKEY"):
        assert forbidden not in encoded


def test_http_error_without_safe_binance_code_omits_raw_response_data() -> None:
    client = FakeGetOnlySignedClient(
        [BinanceServerError(status=503, message=_SIGNED_URL, headers={"cookie": _API_SECRET})]
    )

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client)

    assert caught.value.diagnostic.to_stop_payload() == {  # type: ignore[union-attr]
        "category": "HTTP_FAILURE",
        "endpoint": "SYMBOL_CONFIG",
        "http_status": 503,
        "protocol_version": "1.11",
        "stage": "SYMBOL_CONFIG_REQUEST",
        "status": "STOP",
    }


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (urllib.error.URLError(_SIGNED_URL), "NETWORK_FAILURE"),
        (ssl.SSLError(_SIGNED_URL), "TLS_FAILURE"),
    ],
)
def test_network_and_tls_failures_expose_no_raw_exception(
    error: BaseException,
    category: str,
) -> None:
    client = FakeGetOnlySignedClient([error])

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client)

    payload = caught.value.diagnostic.to_stop_payload()  # type: ignore[union-attr]
    assert payload == {
        "category": category,
        "endpoint": "SYMBOL_CONFIG",
        "protocol_version": "1.11",
        "stage": "SYMBOL_CONFIG_REQUEST",
        "status": "STOP",
    }
    assert _SIGNED_URL not in json.dumps(payload)


def test_untrusted_transport_error_reason_is_not_propagated() -> None:
    client = FakeGetOnlySignedClient([ReadOnlyPreflightError(_SIGNED_URL)])

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client)

    assert str(caught.value) == "READ_ONLY_IO_FAILED"
    assert _SIGNED_URL not in repr(caught.value)
    assert _SIGNED_URL not in json.dumps(caught.value.diagnostic.to_stop_payload())  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("endpoint", "stage", "endpoint_name"),
    [
        (ReadOnlyEndpoint.SYMBOL_CONFIGURATION, "SYMBOL_CONFIG_REQUEST", "SYMBOL_CONFIG"),
        (ReadOnlyEndpoint.OPEN_ORDERS, "OPEN_ORDERS_REQUEST", "OPEN_ORDERS"),
        (ReadOnlyEndpoint.POSITION_STATE, "POSITION_RISK_REQUEST", "POSITION_RISK"),
    ],
)
def test_request_failure_stage_is_endpoint_specific(
    endpoint: ReadOnlyEndpoint,
    stage: str,
    endpoint_name: str,
) -> None:
    client = FakeGetOnlySignedClient([RuntimeError(_SIGNED_URL)])

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client, endpoint)

    assert caught.value.diagnostic.to_stop_payload() == {  # type: ignore[union-attr]
        "category": "OTHER_SAFE_ERROR",
        "endpoint": endpoint_name,
        "protocol_version": "1.11",
        "stage": stage,
        "status": "STOP",
    }


@pytest.mark.parametrize(
    ("endpoint", "stage", "endpoint_name"),
    [
        (ReadOnlyEndpoint.SYMBOL_CONFIGURATION, "SYMBOL_CONFIG_RESPONSE", "SYMBOL_CONFIG"),
        (ReadOnlyEndpoint.OPEN_ORDERS, "OPEN_ORDERS_RESPONSE", "OPEN_ORDERS"),
        (ReadOnlyEndpoint.POSITION_STATE, "POSITION_RISK_RESPONSE", "POSITION_RISK"),
    ],
)
def test_response_validation_stage_is_endpoint_specific(
    endpoint: ReadOnlyEndpoint,
    stage: str,
    endpoint_name: str,
) -> None:
    client = FakeGetOnlySignedClient([_SIGNED_URL.encode()])

    with pytest.raises(ReadOnlyPreflightError) as caught:
        _request(client, endpoint)

    assert caught.value.diagnostic.to_stop_payload() == {  # type: ignore[union-attr]
        "category": "RESPONSE_VALIDATION_FAILED",
        "endpoint": endpoint_name,
        "protocol_version": "1.11",
        "stage": stage,
        "status": "STOP",
    }


def test_cli_redacts_exception_attack_and_stops_after_one_attempt(capsys) -> None:
    prompts = iter(("hmac", _API_KEY, _API_SECRET))
    client = FakeGetOnlySignedClient(
        [
            BinanceClientError(
                status=400,
                message={"code": -1022, "msg": _SIGNED_URL},
                headers={"X-MBX-APIKEY": _API_KEY, "cookie": _API_SECRET},
            ),
            b"must-not-be-retried",
        ]
    )

    assert (
        run_prompted_read_only_preflight(
            prompt_secret=lambda _label: next(prompts),
            environ={},
            input_is_tty=True,
            core_dump_guard=lambda: None,
            transport_builder=lambda _credentials: _transport(client),
        )
        == 1
    )

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "binance_code": -1022,
        "category": "SIGNATURE_INVALID",
        "endpoint": "SYMBOL_CONFIG",
        "http_status": 400,
        "protocol_version": "1.11",
        "stage": "SYMBOL_CONFIG_REQUEST",
        "status": "STOP",
    }
    assert client.calls == [ReadOnlyEndpoint.SYMBOL_CONFIGURATION.value]
    assert len(client.responses) == 1
    for forbidden in (_API_KEY, _API_SECRET, _SIGNATURE, _SIGNED_URL, "X-MBX-APIKEY"):
        assert forbidden not in output
