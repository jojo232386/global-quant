from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.common.component import LiveClock

import global_quant.gate1b.read_only_preflight as read_only_module
from global_quant.gate1b.credential_http import _HttpResponse, _RedirectSafeHttpClient
from global_quant.gate1b.read_only_preflight import (
    DEMO_HTTP_ORIGIN,
    AuthenticatedReadOnlyPreflightTransport,
    ReadOnlyEndpoint,
    ReadOnlyPreflightError,
)

_API_KEY = "synthetic-test-key"
_SECRET = "synthetic-test-secret"
_TIMESTAMP_MS = 1_700_000_000_123
_EXPECTED_HMAC = "c0375dac8f368edbbab440bc7d34503795d4201cc384afe091ef40c4538831cc"
_DEADLINE_NS = 10**30


def _signed_client(*, timestamp: object = _TIMESTAMP_MS):
    http_client = BinanceHttpClient(
        clock=LiveClock(),
        api_key=_API_KEY,
        api_secret=_SECRET,
        base_url=DEMO_HTTP_ORIGIN,
    )
    return read_only_module._ExistingDemoGetOnlySignedClient(
        http_client,
        timestamp_ms=lambda: timestamp,  # type: ignore[return-value]
    )


def _transport(*, timestamp: object = _TIMESTAMP_MS) -> AuthenticatedReadOnlyPreflightTransport:
    return AuthenticatedReadOnlyPreflightTransport(
        _signed_client(timestamp=timestamp),
        _construction_token=read_only_module._TRANSPORT_CONSTRUCTION_TOKEN,
    )


def test_all_signed_gets_match_official_query_and_deterministic_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []

    def capture(
        _self: _RedirectSafeHttpClient,
        method: object,
        url: str,
        headers: object,
        body: bytes | None,
        _timeout_secs: float | None,
        _absolute_deadline_ns: int,
    ) -> _HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        captures.append(
            {
                "method": str(method).split(".")[-1].upper(),
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "path": parsed.path,
                "pairs": pairs,
                "headers": dict(headers or {}),
                "body": body,
            }
        )
        response = {
            ReadOnlyEndpoint.SYMBOL_CONFIGURATION.value: (
                b'[{"symbol":"ETHUSDT","marginType":"ISOLATED",'
                b'"isAutoAddMargin":false,"leverage":1}]'
            ),
            ReadOnlyEndpoint.OPEN_ORDERS.value: b"[]",
            ReadOnlyEndpoint.POSITION_STATE.value: b"[]",
        }[parsed.path]
        return _HttpResponse(status=200, headers={}, body=response)

    monkeypatch.setattr(_RedirectSafeHttpClient, "_request_sync", capture)
    transport = _transport()
    try:
        transport.run_fixed_preflight(deadline_factory=lambda: _DEADLINE_NS)
    finally:
        transport.close()

    assert [capture["path"] for capture in captures] == [
        endpoint.value for endpoint in ReadOnlyEndpoint
    ]
    for captured in captures:
        pairs = captured["pairs"]
        assert captured["method"] == "GET"
        assert captured["origin"] == DEMO_HTTP_ORIGIN
        assert captured["body"] is None
        assert [key for key, _value in pairs] == ["symbol", "timestamp", "signature"]
        assert pairs[0] == ("symbol", "ETHUSDT")
        assert pairs[1] == ("timestamp", str(_TIMESTAMP_MS))
        assert pairs[2] == ("signature", _EXPECTED_HMAC)
        assert captured["headers"]["X-MBX-APIKEY"] == _API_KEY
        assert [key for key, _value in pairs[:-1]] == ["symbol", "timestamp"]


@pytest.mark.parametrize(
    "timestamp",
    [None, "", True, 0, 1_700_000_000, 10_000_000_000_000],
)
def test_missing_empty_or_malformed_timestamp_fails_before_dispatch(
    timestamp: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched = False

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> _HttpResponse:
        nonlocal dispatched
        dispatched = True
        raise AssertionError("network dispatch must not occur")

    monkeypatch.setattr(_RedirectSafeHttpClient, "_request_sync", forbidden_dispatch)
    signed_client = _signed_client(timestamp=timestamp)
    try:
        with pytest.raises(ReadOnlyPreflightError, match="READ_TIMESTAMP_INVALID"):
            signed_client.get(
                ReadOnlyEndpoint.SYMBOL_CONFIGURATION.value,
                {"symbol": "ETHUSDT"},
                absolute_deadline_ns=_DEADLINE_NS,
            )
    finally:
        signed_client.close()

    assert dispatched is False
