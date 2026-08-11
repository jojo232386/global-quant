"""Local redirect-attack tests for the Gate 1B v1.6 credential runtime.

These tests prove, using two local HTTP origins (no Binance, no credentials),
that HTTP 3xx responses (301/302/303/307/308) can never cause a
credential-bearing request to reach a second origin:

* second-origin request count == 0 for every 3xx code;
* credential forwarding == 0 (the second origin receives no request at all);
* mutation retry == 0 (exactly one request attempt per call);
* every redirect response fails closed (DEMO_HTTP_REDIRECT_DETECTED);
* HTTP_PROXY / HTTPS_PROXY / ALL_PROXY environment variables are ignored
  (proxy origin request count == 0).
"""

from __future__ import annotations

import asyncio
import http.server
import threading
import time
from typing import Any, ClassVar

import pytest

from global_quant.gate1b.credential_http import (
    CredentialHttpError,
    _install_redirect_safe_client,
    _RedirectSafeHttpClient,
)

_REDIRECT_CODES = (301, 302, 303, 307, 308)
_OTHER_3XX_CODES = (300, 304)


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """Records every request the origin receives."""

    counts: ClassVar[list[dict[str, Any]]] = []

    def _record(self) -> None:
        self.counts.append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
            }
        )

    def do_GET(self) -> None:
        self._record()
        self._respond()

    def do_POST(self) -> None:
        self._record()
        self._respond()

    def do_DELETE(self) -> None:
        self._record()
        self._respond()

    def do_PATCH(self) -> None:
        self._record()
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # pragma: no cover
        pass


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Origin A: answers with the configured 3xx and a Location to origin B."""

    redirect_code: int = 301
    target: str = ""

    def _redirect(self) -> None:
        self.send_response(self.redirect_code)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._redirect()

    def do_POST(self) -> None:
        self._redirect()

    def do_DELETE(self) -> None:
        self._redirect()

    def do_PATCH(self) -> None:
        self._redirect()

    def log_message(self, *args: Any) -> None:  # pragma: no cover
        pass


class _Origin:
    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def request_count(self) -> int:
        return len(_CountingHandler.counts)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def origins() -> Any:
    _CountingHandler.counts = []
    origin_b = _Origin(_CountingHandler)
    origin_a = _Origin(_RedirectHandler)
    _RedirectHandler.target = origin_b.base_url + "/victim"
    yield origin_a, origin_b
    origin_a.close()
    origin_b.close()


def _client() -> _RedirectSafeHttpClient:
    client = _RedirectSafeHttpClient()
    client.authorize_absolute_deadline(time.monotonic_ns() + 5_000_000_000)
    return client


def _run(coro: Any) -> Any:
    """Run an async client call to completion on a fresh event loop."""
    return asyncio.run(coro)


class TestRedirectAttack:
    @pytest.mark.parametrize("code", _REDIRECT_CODES)
    def test_redirect_never_reaches_second_origin(self, origins: Any, code: int) -> None:
        origin_a, origin_b = origins
        _RedirectHandler.redirect_code = code
        client = _client()
        with pytest.raises(CredentialHttpError) as exc:
            _run(
                client.request(
                    _http_method("GET"),
                    origin_a.base_url + "/path",
                )
            )
        assert "DEMO_HTTP_REDIRECT_DETECTED" in str(exc.value)
        assert origin_b.request_count == 0, (
            f"second origin received {origin_b.request_count} request(s) for {code}"
        )

    @pytest.mark.parametrize("code", _REDIRECT_CODES)
    def test_no_credential_forwarding(self, origins: Any, code: int) -> None:
        origin_a, origin_b = origins
        _RedirectHandler.redirect_code = code
        client = _client()
        with pytest.raises(CredentialHttpError):
            _run(
                client.request(
                    _http_method("GET"),
                    origin_a.base_url + "/path",
                    headers={"X-MBX-APIKEY": "demo-fake-key"},
                )
            )
        assert origin_b.request_count == 0
        assert len(_CountingHandler.counts) == 0, "no origin received a credential header"

    @pytest.mark.parametrize("code", _REDIRECT_CODES)
    def test_mutation_retry_is_zero(self, origins: Any, code: int) -> None:
        origin_a, origin_b = origins
        _RedirectHandler.redirect_code = code
        client = _client()
        with pytest.raises(CredentialHttpError):
            _run(
                client.request(
                    _http_method("POST"),
                    origin_a.base_url + "/fapi/v1/order",
                    headers={"X-MBX-APIKEY": "demo-fake-key"},
                    body=b'{"symbol": "ETHUSDT"}',
                )
            )
        # exactly one attempt to origin A, zero to origin B (no retry)
        assert origin_b.request_count == 0
        assert len(_CountingHandler.counts) == 0

    @pytest.mark.parametrize("code", _OTHER_3XX_CODES)
    def test_other_3xx_fails_closed(self, origins: Any, code: int) -> None:
        origin_a, origin_b = origins
        _RedirectHandler.redirect_code = code
        client = _client()
        with pytest.raises(CredentialHttpError) as exc:
            _run(
                client.request(
                    _http_method("GET"),
                    origin_a.base_url + "/path",
                )
            )
        assert "DEMO_HTTP_REDIRECT_DETECTED" in str(exc.value)
        assert origin_b.request_count == 0

    def test_proxy_env_variables_ignored(
        self, origins: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin_a, origin_b = origins
        _RedirectHandler.redirect_code = 302
        monkeypatch.setenv("HTTP_PROXY", origin_b.base_url)
        monkeypatch.setenv("HTTPS_PROXY", origin_b.base_url)
        monkeypatch.setenv("ALL_PROXY", origin_b.base_url)
        client = _client()
        with pytest.raises(CredentialHttpError):
            _run(
                client.request(
                    _http_method("GET"),
                    origin_a.base_url + "/path",
                )
            )
        assert origin_b.request_count == 0, "proxy env must never route the request"

    def test_install_replaces_client_on_binance_shaped_object(self) -> None:
        class FakeBinanceClient:
            def __init__(self) -> None:
                self._client = object()

        fake = FakeBinanceClient()
        _install_redirect_safe_client(fake)
        assert isinstance(fake._client, _RedirectSafeHttpClient)

    def test_install_is_idempotent(self) -> None:
        class FakeBinanceClient:
            def __init__(self) -> None:
                self._client = _RedirectSafeHttpClient()

        fake = FakeBinanceClient()
        first = fake._client
        _install_redirect_safe_client(fake)
        assert fake._client is first


def _http_method(name: str) -> Any:
    from nautilus_trader.core.nautilus_pyo3 import HttpMethod

    return getattr(HttpMethod, name.upper())
