"""Credential-process low-level HTTP leaf for Gate1B v1.6.

This redirect-safe, proxy-free implementation has no lifecycle or mutation
ownership.  Callers provide an already credential-bound signed client and the
process-bound execution kernel retains all dispatch/reconciliation decisions.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import socket
import threading
import time as _time
import urllib.error
import urllib.request
from typing import Any


class CredentialHttpError(RuntimeError):
    """Fail-closed error from the credential child HTTP leaf."""


# Frozen single-request timeout (protocol section 11).
_REQUEST_TIMEOUT_SECONDS = 5.0
# Dedicated in-child worker pool for synchronous urllib calls.  It is a low
# level implementation detail only: the supervisor's kill + exact reap is the
# sole hard-quiescence proof.
_HTTP_WORKER_THREADS = 4
# ---------------------------------------------------------------------------
# Redirect isolation (protocol section 2.1 redirect fail-closed).
#
# Source-verified behaviour of nautilus_trader 1.230.0
# (crates/network/src/http/client.rs + crates/network/src/python/http.rs):
#
#   * the pyo3 ``HttpClient`` exposes NO redirect policy parameter
#     (``__new__`` signature: default_headers, header_keys, keyed_quotas,
#     default_quota, timeout_secs, proxy_url);
#   * its reqwest ``Client::builder()`` never calls ``.redirect(...)``, so the
#     reqwest default ``Policy::limited(10)`` applies — the client silently
#     follows up to 10 redirects, including cross-origin ones, forwarding
#     non-sensitive headers (e.g. ``X-MBX-APIKEY``).
#
# Because redirects cannot be disabled on the pyo3 client and are followed by
# default, a post-hoc status check can never prove a request was not
# redirected.  The credential-bearing path therefore REPLACES the pyo3 client
# with ``_RedirectSafeHttpClient``, a stdlib-only client that:
#
#   * never follows redirects: any 3xx (301/302/303/307/308 and 300/304/305/306)
#     raises ``DEMO_HTTP_REDIRECT_DETECTED`` before a second origin is contacted;
#   * never reads HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY environment
#     variables (``ProxyHandler({})``) — proxy isolation is unconditional;
#   * enforces the frozen 5 s single-request timeout.
#
# The returned response object exposes the same ``status`` / ``headers`` /
# ``body`` attributes that ``BinanceHttpClient.send_request`` reads, so the
# Python signing layer (HMAC signing, URL assembly, error classification)
# remains unchanged.
# ---------------------------------------------------------------------------


class _HttpResponse:
    """Minimal response contract compatible with ``BinanceHttpClient``."""

    __slots__ = ("body", "headers", "status")

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed on any HTTP 3xx before a second origin is contacted.

    ``HTTPRedirectHandler.redirect_request`` is invoked for 301/302/303/307/308
    before any follow-up request is made; raising here guarantees the second
    origin receives zero requests.  Codes not routed through
    ``redirect_request`` (300/304/305/306) are rejected by the defensive
    status check in ``_RedirectSafeHttpClient._request_sync``.
    """

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        raise CredentialHttpError(f"DEMO_HTTP_REDIRECT_DETECTED:{code}")


# ---------------------------------------------------------------------------
# Total-request deadline watchdog + mutation byte-emission tracking.
#
# A socket timeout is a *per-blocking-operation* bound, not a total-request
# bound: an origin that trickles one byte inside every timeout window keeps the
# worker thread alive without limit. Each request therefore arms a watchdog at
# the supervisor-derived absolute phase deadline; on expiry it shuts the
# underlying socket down so the worker can unwind. Hard quiescence remains the
# enclosing process boundary's kill + exact-reap property.
#
# The same per-request state records whether any request byte was handed to the
# socket.  Failure classification is then based on "were mutation bytes possibly
# emitted", not on the Python exception type: a connect-phase failure is
# provably zero-bytes (plain STOP), while any post-send failure is an UNKNOWN
# mutation that must be routed through the deterministic ownership query.
# ---------------------------------------------------------------------------


def _force_close_connection(connection: Any) -> None:
    """Best-effort forced teardown of one HTTP connection."""
    sock = getattr(connection, "sock", None)
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        connection.close()


class _RequestExecutionState:
    """Per-request state owned by the worker thread executing that request."""

    __slots__ = ("_bytes_emitted", "_connections", "_expired", "_lock", "_timer")

    def __init__(self, total_deadline_secs: float) -> None:
        self._lock = threading.Lock()
        self._connections: list[Any] = []
        self._expired = False
        self._bytes_emitted = False
        self._timer = threading.Timer(total_deadline_secs, self._expire)
        self._timer.daemon = True

    # -- total-request watchdog ---------------------------------------------

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.cancel()

    def attach(self, connection: Any) -> None:
        """Register a live connection so the watchdog can tear it down."""
        with self._lock:
            if not self._expired:
                self._connections.append(connection)
                return
        # The deadline already fired before this connection existed.
        _force_close_connection(connection)

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            connections = list(self._connections)
        for connection in connections:
            _force_close_connection(connection)

    @property
    def deadline_expired(self) -> bool:
        with self._lock:
            return self._expired

    # -- mutation byte emission ---------------------------------------------

    def note_bytes_emitted(self) -> None:
        with self._lock:
            self._bytes_emitted = True

    @property
    def bytes_emitted(self) -> bool:
        with self._lock:
            return self._bytes_emitted


_REQUEST_STATE = threading.local()


def _current_request_state() -> _RequestExecutionState | None:
    return getattr(_REQUEST_STATE, "state", None)


@functools.cache
def _watchdog_connection_class(base: type) -> type:
    """Subclass ``base`` so every connection reports to the request state.

    ``connect()`` registers the live socket with the watchdog *after* the
    connection succeeded (during DNS/connect the socket does not exist yet;
    that phase is bounded by the socket timeout handed to
    ``create_connection``).  ``send()`` marks byte emission only once a socket
    exists, so a connect-phase failure stays provably zero-bytes-emitted.
    """

    class _WatchdogConnection(base):  # type: ignore[misc, valid-type]
        def connect(self) -> None:
            super().connect()
            state = _current_request_state()
            if state is not None:
                state.attach(self)

        def send(self, data: Any) -> None:
            if self.sock is None and self.auto_open:
                # Mirrors HTTPConnection.send so the connect happens before the
                # byte-emission flag is set, never after.
                self.connect()
            if self.sock is not None:
                state = _current_request_state()
                if state is not None:
                    state.note_bytes_emitted()
            super().send(data)

    _WatchdogConnection.__name__ = f"_Watchdog{base.__name__}"
    _WatchdogConnection.__qualname__ = _WatchdogConnection.__name__
    return _WatchdogConnection


class _WatchdogHTTPHandler(urllib.request.HTTPHandler):
    def do_open(self, http_class: type, req: Any, **kwargs: Any) -> Any:
        return super().do_open(_watchdog_connection_class(http_class), req, **kwargs)


class _WatchdogHTTPSHandler(urllib.request.HTTPSHandler):
    def do_open(self, http_class: type, req: Any, **kwargs: Any) -> Any:
        return super().do_open(_watchdog_connection_class(http_class), req, **kwargs)


class _RedirectSafeHttpClient:
    """Stdlib HTTP client with redirects disabled; replaces the pyo3 client.

    Implements the same ``request(method, url, params, headers, body, keys,
    timeout_secs)`` interface as the pyo3 ``HttpClient`` so it can be swapped
    into ``BinanceHttpClient._client`` without touching the signing layer.

    The synchronous urllib call runs on a worker thread because the existing
    signing stack is async.  Thread/Future cancellation is not a safety proof:
    any ambiguous timeout is resolved by terminating and exactly reaping the
    entire credential process, followed by deterministic reconciliation.
    """

    def __init__(
        self,
        timeout_secs: float = _REQUEST_TIMEOUT_SECONDS,
        *,
        max_workers: int = _HTTP_WORKER_THREADS,
    ) -> None:
        self._timeout_secs = timeout_secs
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # never read env proxy variables
            _WatchdogHTTPHandler(),
            _WatchdogHTTPSHandler(),
            _NoRedirectHandler(),
        )
        self._deadline_lock = threading.Lock()
        self._absolute_deadline_ns: int | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="g1b-http"
        )

    def authorize_absolute_deadline(self, absolute_deadline_ns: int) -> None:
        """Stage one supervisor-derived deadline for the next signed request."""

        if type(absolute_deadline_ns) is not int or absolute_deadline_ns <= _time.monotonic_ns():
            raise CredentialHttpError("DEMO_HTTP_DEADLINE_EXHAUSTED")
        with self._deadline_lock:
            if self._absolute_deadline_ns is not None:
                raise CredentialHttpError("DEMO_HTTP_DEADLINE_ALREADY_STAGED")
            self._absolute_deadline_ns = absolute_deadline_ns

    def cancel_absolute_deadline(self, absolute_deadline_ns: int) -> None:
        """Clear an exact staged deadline if the signing stack never consumed it."""

        with self._deadline_lock:
            if self._absolute_deadline_ns == absolute_deadline_ns:
                self._absolute_deadline_ns = None

    def _consume_absolute_deadline(self) -> int:
        with self._deadline_lock:
            absolute_deadline_ns = self._absolute_deadline_ns
            self._absolute_deadline_ns = None
        if type(absolute_deadline_ns) is not int or absolute_deadline_ns <= _time.monotonic_ns():
            raise CredentialHttpError("DEMO_HTTP_DEADLINE_EXHAUSTED")
        return absolute_deadline_ns

    async def request(
        self,
        method: Any,
        url: str,
        params: Any = None,
        headers: Any = None,
        body: bytes | None = None,
        keys: Any = None,
        timeout_secs: float | None = None,
    ) -> _HttpResponse:
        absolute_deadline_ns = self._consume_absolute_deadline()
        future = self._executor.submit(
            self._request_sync,
            method,
            url,
            headers,
            body,
            timeout_secs,
            absolute_deadline_ns,
        )
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        """Shut the worker pool down; queued work items are cancelled."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _request_sync(
        self,
        method: Any,
        url: str,
        headers: Any,
        body: bytes | None,
        timeout_secs: float | None,
        absolute_deadline_ns: int,
    ) -> _HttpResponse:
        local_limit = self._timeout_secs if timeout_secs is None else float(timeout_secs)
        local_limit = min(self._timeout_secs, local_limit)
        remaining = (absolute_deadline_ns - _time.monotonic_ns()) / 1_000_000_000
        timeout = min(local_limit, remaining)
        if timeout <= 0:
            raise CredentialHttpError("DEMO_HTTP_DEADLINE_EXHAUSTED")
        state = _RequestExecutionState(timeout)
        _REQUEST_STATE.state = state
        state.start()
        try:
            return self._open_sync(method, url, headers, body, timeout)
        except (CredentialHttpError, TimeoutError):
            # Redirect fail-closed keeps its frozen reason; a timeout keeps its
            # own boundary (the caller drains and runs timeout containment).
            raise
        except Exception as exc:
            if state.bytes_emitted:
                # Request bytes reached the socket: it cannot be proven that
                # the venue never saw this mutation. Fail closed into the
                # UNKNOWN-mutation class so the runner runs the deterministic
                # ownership query instead of a plain STOP.
                raise CredentialHttpError(
                    f"DEMO_HTTP_UNKNOWN_MUTATION_STATE:{type(exc).__name__.upper()}"
                ) from exc
            # Connect/DNS phase: zero bytes emitted is mechanically provable,
            # so no order can have landed and a plain STOP is correct.
            raise
        finally:
            state.stop()
            _REQUEST_STATE.state = None

    def _open_sync(
        self,
        method: Any,
        url: str,
        headers: Any,
        body: bytes | None,
        timeout: float,
    ) -> _HttpResponse:
        method_name = str(method).split(".")[-1].upper()
        header_map = {str(k): str(v) for k, v in (headers or {}).items()}
        request = urllib.request.Request(url, data=body, headers=header_map, method=method_name)
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                # urllib surfaces some 3xx codes (300/304/305/306) as HTTPError
                # instead of routing them through redirect_request; fail closed.
                raise CredentialHttpError(f"DEMO_HTTP_REDIRECT_DETECTED:{exc.code}") from None
            # >=400 responses must flow back to BinanceHttpClient.send_request
            # unchanged so its own 4xx/5xx classification applies.
            return _HttpResponse(
                status=int(exc.code),
                headers={str(k): str(v) for k, v in (exc.headers or {}).items()},
                body=exc.read(),
            )
        status = int(getattr(response, "status", 0))
        if 300 <= status < 400:
            # Defensive: covers 300/304/305/306 not routed through the handler.
            raise CredentialHttpError(f"DEMO_HTTP_REDIRECT_DETECTED:{status}")
        return _HttpResponse(
            status=status,
            headers={str(k): str(v) for k, v in response.headers.items()},
            body=response.read(),
        )


def _install_redirect_safe_client(http_client: Any) -> None:
    """Replace ``http_client._client`` with ``_RedirectSafeHttpClient``.

    Idempotent: if the inner client is already a ``_RedirectSafeHttpClient``,
    no second replacement is applied.  Installed exactly once at transport
    construction time before any credential-bearing request is made.
    """
    inner = getattr(http_client, "_client", None)
    if inner is None:
        return
    if isinstance(inner, _RedirectSafeHttpClient):
        return  # already redirect-safe
    http_client._client = _RedirectSafeHttpClient()
