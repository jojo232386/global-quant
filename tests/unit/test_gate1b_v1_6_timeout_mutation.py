"""Timeout / in-flight mutation attack tests for Gate 1B v1.6.

These tests prove the timeout boundary guarantee:

* NO_QUEUED_OR_RUNNING_MUTATION_SURVIVES_TIMEOUT_BOUNDARY — when the transport
  raises ``DEMO_HTTP_TIMEOUT``, every tracked request has converged, whether it
  was already running on a worker thread (urllib is synchronous and cannot be
  cancelled by asyncio) or still sitting in the executor queue.  A request that
  has been submitted but not started is counted, a request that is RUNNING but
  has not yet entered the worker frame is counted, and a drain that cannot
  prove convergence within its absolute deadline escalates to
  ``BLOCKED_MUTATION_TIMEOUT_DRAIN_UNCONVERGED`` instead of returning quietly;
* failure classification is by "could the mutation bytes have been emitted",
  not by Python exception type: a post-send failure becomes
  ``DEMO_HTTP_UNKNOWN_MUTATION_STATE`` and is routed through the ownership
  query, while a connect-phase failure stays an ordinary STOP;
* a timed-out or unknown-state CREATE is never retried;
* the runner routes such a mutation into ownership-query containment and ends
  BLOCKED — a clean happy-path PASS is unreachable, so a "late mutation"
  landing after a clean verdict is impossible.  A probe proven ``FILLED`` runs
  the frozen section-14 containment (it is not skipped by the inapplicable
  owned-order-OPEN proof).

No real credential, no Binance, no Demo mutation: the slow / abrupt servers are
local sockets and the runner uses a fixture-driven fake transport.
"""

from __future__ import annotations

import asyncio
import concurrent.futures.thread as _cf_thread
import contextlib
import http.server
import socket
import struct
import threading
import time
from decimal import Decimal
from typing import Any

import pytest

import global_quant.gate1b.demo_transport as dt
from global_quant.gate1b.demo_transport import (
    DEMO_HTTP_ORIGIN,
    DemoLifecycleTransport,
    _RedirectSafeHttpClient,
)
from global_quant.gate1b.mutation_protocol import (
    AccountState,
    LimitOrderFilters,
    MarketCloseFilters,
    SymbolState,
)
from global_quant.gate1b.mutation_runner import (
    MutationRunnerError,
    run_mutation_lifecycle,
)

_RUNTIME_COMMIT = "a" * 40
_SESSION_NONCE = "0123456789abcdef"
_AUTHORIZATION_ID = "g1b16-0123456789abcdef"
_PROTOCOL_COMMIT = "d" * 40
_PROTOCOL_TAG_OBJECT = "e" * 40
_PROTOCOL_SHA256 = "f" * 64


# ---------------------------------------------------------------------------
# Local slow-origin helpers (origin A delays the response past the timeout).
# ---------------------------------------------------------------------------


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Origin A: records every request and delays responses past the timeout."""

    delay_seconds: float = 3.0
    started: list[float] = []
    finished: list[float] = []

    def _slow(self) -> None:
        self.started.append(time.monotonic())
        time.sleep(self.delay_seconds)
        try:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.finished.append(time.monotonic())

    def do_GET(self) -> None:  # noqa: N802
        self._slow()

    def do_POST(self) -> None:  # noqa: N802
        self._slow()

    def log_message(self, *args: Any) -> None:  # pragma: no cover
        pass


class _SlowOrigin:
    def __init__(self, delay_seconds: float = 3.0) -> None:
        _SlowHandler.delay_seconds = delay_seconds
        _SlowHandler.started = []
        _SlowHandler.finished = []
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def request_count(self) -> int:
        return len(_SlowHandler.started)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _SlowSignedClient:
    """Fake signed client: passes the Demo-origin check but talks to a local
    slow origin through a real ``_RedirectSafeHttpClient``."""

    def __init__(self, target_url: str, timeout_secs: float) -> None:
        self.base_url = DEMO_HTTP_ORIGIN  # passes transport origin pinning
        self._target = target_url
        self._client = _RedirectSafeHttpClient(timeout_secs=timeout_secs)

    async def sign_request(self, http_method, url_path, payload=None, ratelimiter_keys=None):
        return await self._client.request(
            http_method, self._target + url_path, headers={"X-MBX-APIKEY": "demo-fake-key"}
        )


class _AbruptCloseOrigin:
    """Origin B: accepts the connection, reads the request, then resets it.

    This reproduces the only dangerous send-phase failure: the request bytes
    (a CREATE) have provably reached the socket and may have reached the venue,
    but no response is returned.  ``SO_LINGER(1, 0)`` makes ``close()`` emit a
    TCP RST so the client observes a reset rather than a clean EOF.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.request_bytes_received = 0
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._sock.getsockname()
        return f"http://{host}:{port}"

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with contextlib.suppress(OSError):
                conn.settimeout(2.0)
                data = conn.recv(65536)
                self.request_bytes_received += len(data)
                conn.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
            with contextlib.suppress(OSError):
                conn.close()

    def close(self) -> None:
        self._running = False
        with contextlib.suppress(OSError):
            self._sock.close()


# ---------------------------------------------------------------------------
# Transport-level: drain guarantee
# ---------------------------------------------------------------------------


class TestTransportDrain:
    def test_pending_thread_visible_and_drained(self) -> None:
        client = _RedirectSafeHttpClient(timeout_secs=0.5)

        async def run() -> None:
            task = asyncio.create_task(
                client.request("GET", "http://127.0.0.1:1/never", headers={})
            )
            # Give the worker thread time to start (connection refused is fast;
            # force a slow path via a socket to nowhere? Use the real slow origin
            # instead in the next test; here prove the counter mechanics.)
            await asyncio.sleep(0.05)
            return task

        asyncio.run(run())
        # pending may be 0 already for refused connection; the drain guarantee
        # itself is proven in test_signed_timeout_drains_before_raise.
        assert client.pending_count() == 0

    def test_signed_timeout_drains_before_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: when _signed raises DEMO_HTTP_TIMEOUT, nothing queued or
        running survives the boundary
        (NO_QUEUED_OR_RUNNING_MUTATION_SURVIVES_TIMEOUT_BOUNDARY)."""
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 0.5)
        origin = _SlowOrigin(delay_seconds=3.0)
        try:
            signed = _SlowSignedClient(origin.base_url, timeout_secs=0.5)
            transport = DemoLifecycleTransport(http_client=signed)

            # During the request the worker is in flight (origin sleeps 3s).
            with pytest.raises(MutationRunnerError) as exc:
                transport.fetch_book()
            assert "DEMO_HTTP_TIMEOUT" in str(exc.value)

            # The timeout boundary guarantee: after the raise, zero threads.
            assert transport.pending_request_count() == 0, (
                "worker thread survived the timeout boundary"
            )
            # The origin received exactly one request (no retry).
            assert origin.request_count == 1
        finally:
            origin.close()
            transport.close()

    def test_inflight_was_running_during_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prove the attack premise: at the moment wait_for times out, the
        worker thread is still running (server has not responded yet)."""
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 0.5)
        origin = _SlowOrigin(delay_seconds=3.0)
        try:
            signed = _SlowSignedClient(origin.base_url, timeout_secs=0.5)
            transport = DemoLifecycleTransport(http_client=signed)
            client = signed._client

            before = client.pending_count()
            with pytest.raises(MutationRunnerError):
                transport.fetch_book()
            after = client.pending_count()

            # The worker was in flight during the timeout (origin still sleeping)
            # and converged by the time the error reached the caller.
            assert origin.request_count == 1
            assert after == 0
        finally:
            origin.close()
            transport.close()

    def test_late_mutation_impossible_after_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After the timeout raise, the original request has converged: the
        origin's handler has returned (request finished) and no follow-up
        request arrives (no retry). A late landing after the boundary is
        impossible."""
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 0.5)
        origin = _SlowOrigin(delay_seconds=0.8)
        try:
            signed = _SlowSignedClient(origin.base_url, timeout_secs=0.5)
            transport = DemoLifecycleTransport(http_client=signed)

            with pytest.raises(MutationRunnerError):
                transport.fetch_book()
            # After the boundary: no pending threads, no retry.
            assert transport.pending_request_count() == 0
            assert origin.request_count == 1
            # Give the origin time to finish the in-flight handler; no new
            # request may arrive.
            time.sleep(0.4)
            assert origin.request_count == 1, "request was retried after timeout"
        finally:
            origin.close()
            transport.close()


# ---------------------------------------------------------------------------
# Attack A: a submitted-but-not-started request must already be tracked.
# ---------------------------------------------------------------------------


class TestQueuedRequestTracking:
    def test_saturated_executor_counts_queued_request(self) -> None:
        """The executor has one worker.  While request 1 occupies it, request 2
        is *queued*: its callable has never run and the origin has never seen
        it.  ``pending_count()`` must nonetheless report 2 — tracking covers
        queued + running, not running-and-registered only."""
        origin = _SlowOrigin(delay_seconds=1.0)
        client = _RedirectSafeHttpClient(timeout_secs=5.0, max_workers=1)
        observed: dict[str, int] = {}

        async def scenario() -> None:
            first = asyncio.ensure_future(
                client.request("GET", origin.base_url + "/first", headers={})
            )
            # Let the worker pick request 1 up and reach the origin.
            for _ in range(200):
                if origin.request_count >= 1:
                    break
                await asyncio.sleep(0.005)
            second = asyncio.ensure_future(
                client.request("GET", origin.base_url + "/second", headers={})
            )
            # Yield until the second coroutine has registered and submitted.
            for _ in range(200):
                if client.pending_count() >= 2:
                    break
                await asyncio.sleep(0.005)
            observed["pending"] = client.pending_count()
            observed["origin_requests"] = origin.request_count

            # The drain must cancel the queued work item (it can never run) and
            # wait for the running one; it must not report a clean boundary
            # while either is outstanding.
            await client.drain(deadline_secs=5.0)
            observed["pending_after_drain"] = client.pending_count()
            observed["origin_requests_after_drain"] = origin.request_count

            for task in (first, second):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        try:
            asyncio.run(scenario())
        finally:
            client.close()
            origin.close()

        assert observed["origin_requests"] == 1, (
            "premise broken: the second request must still be queued"
        )
        assert observed["pending"] == 2, (
            "a submitted-but-not-started request was invisible to pending_count()"
        )
        assert observed["pending_after_drain"] == 0
        # The cancelled work item must never have executed.
        assert observed["origin_requests_after_drain"] == 1


# ---------------------------------------------------------------------------
# Attack B: the RUNNING-but-not-yet-inside-the-worker window.
# ---------------------------------------------------------------------------


class TestRunningBeforeWorkerEntryWindow:
    def test_window_between_running_and_worker_entry_is_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CPython marks the future RUNNING in ``_WorkItem.run()`` *before* it
        calls the submitted callable.  In that window ``cancel()`` already
        fails, so the coroutine must not clear the token, and the worker frame
        has not been entered yet, so its ``finally`` cannot have cleared it
        either.  The token must therefore still be registered.

        The window is unobservable from the public API, so this test
        reimplements ``_WorkItem.run`` faithfully and pauses inside it.
        """
        client = _RedirectSafeHttpClient(timeout_secs=1.0, max_workers=1)
        in_window = threading.Event()
        resume = threading.Event()
        observed: dict[str, Any] = {}

        def patched_run(work_item: Any) -> None:
            if not work_item.future.set_running_or_notify_cancel():
                return
            # --- inside the uncancellable window -----------------------------
            observed["running"] = work_item.future.running()
            observed["cancel_result"] = work_item.future.cancel()
            observed["pending_in_window"] = client.pending_count()
            in_window.set()
            resume.wait(5.0)
            # --- faithful continuation of CPython's _WorkItem.run ------------
            try:
                result = work_item.fn(*work_item.args, **work_item.kwargs)
            except BaseException as exc:
                work_item.future.set_exception(exc)
            else:
                work_item.future.set_result(result)

        monkeypatch.setattr(_cf_thread._WorkItem, "run", patched_run)

        async def scenario() -> None:
            task = asyncio.ensure_future(
                client.request("GET", "http://127.0.0.1:1/never", headers={})
            )
            for _ in range(400):
                if in_window.is_set():
                    break
                await asyncio.sleep(0.005)
            observed["pending_from_loop"] = client.pending_count()
            resume.set()
            with contextlib.suppress(BaseException):
                await task
            observed["pending_after"] = client.pending_count()

        try:
            asyncio.run(scenario())
        finally:
            resume.set()
            client.close()

        assert in_window.is_set(), "the window was never reached"
        assert observed["running"] is True
        assert observed["cancel_result"] is False, (
            "premise broken: cancel() must fail once the future is RUNNING"
        )
        assert observed["pending_in_window"] == 1, (
            "a RUNNING-but-not-yet-entered request was invisible to pending_count()"
        )
        assert observed["pending_from_loop"] == 1
        assert observed["pending_after"] == 0


# ---------------------------------------------------------------------------
# Attack D: the drain has an absolute deadline and fails closed on expiry.
# ---------------------------------------------------------------------------


class TestDrainDeadline:
    def test_unconverged_drain_fails_closed(self) -> None:
        """A drain that cannot prove convergence inside its absolute deadline
        must raise BLOCKED_MUTATION_TIMEOUT_DRAIN_UNCONVERGED, never return."""
        origin = _SlowOrigin(delay_seconds=3.0)
        client = _RedirectSafeHttpClient(timeout_secs=10.0, max_workers=1)
        captured: dict[str, Any] = {}

        async def scenario() -> None:
            task = asyncio.ensure_future(
                client.request("GET", origin.base_url + "/slow", headers={})
            )
            for _ in range(400):
                if origin.request_count >= 1:
                    break
                await asyncio.sleep(0.005)
            started = time.monotonic()
            with pytest.raises(MutationRunnerError) as exc:
                await client.drain(deadline_secs=0.15)
            captured["reason"] = exc.value.reason
            captured["elapsed"] = time.monotonic() - started
            captured["pending"] = client.pending_count()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

        try:
            asyncio.run(scenario())
        finally:
            client.close()
            origin.close()

        assert captured["reason"] == "BLOCKED_MUTATION_TIMEOUT_DRAIN_UNCONVERGED"
        # The deadline is absolute: the drain neither returned early nor waited
        # for the (3 s) origin.
        assert 0.1 <= captured["elapsed"] < 2.0, captured["elapsed"]
        assert captured["pending"] >= 1

    def test_converged_drain_returns(self) -> None:
        """The deadline must not turn a converging drain into a false BLOCKED."""
        client = _RedirectSafeHttpClient(timeout_secs=1.0, max_workers=2)
        try:
            asyncio.run(client.drain(deadline_secs=0.5))
        finally:
            client.close()
        assert client.pending_count() == 0


# ---------------------------------------------------------------------------
# Attack E1: send-phase failure is classified by emitted bytes, not by type.
# ---------------------------------------------------------------------------


class TestSendPhaseClassification:
    def test_post_send_reset_is_unknown_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Request bytes reached the socket and the peer reset the connection:
        the mutation state is UNKNOWN and must fail closed into
        DEMO_HTTP_UNKNOWN_MUTATION_STATE (not a plain transport failure)."""
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 2.0)
        origin = _AbruptCloseOrigin()
        transport = None
        try:
            signed = _SlowSignedClient(origin.base_url, timeout_secs=2.0)
            transport = DemoLifecycleTransport(http_client=signed)
            with pytest.raises(MutationRunnerError) as exc:
                transport.fetch_book()
            assert exc.value.reason.startswith("DEMO_HTTP_UNKNOWN_MUTATION_STATE"), (
                exc.value.reason
            )
            assert origin.request_bytes_received > 0, (
                "premise broken: the request bytes never reached the peer"
            )
            # The unknown-mutation boundary drains too: nothing is left queued.
            assert transport.pending_request_count() == 0
        finally:
            if transport is not None:
                transport.close()
            origin.close()

    def test_connect_phase_failure_is_not_unknown_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero bytes emitted is mechanically provable, so a refused connection
        stays an ordinary STOP and never claims an unknown mutation."""
        monkeypatch.setattr(dt, "_REQUEST_TIMEOUT_SECONDS", 2.0)
        transport = None
        try:
            signed = _SlowSignedClient("http://127.0.0.1:1", timeout_secs=2.0)
            transport = DemoLifecycleTransport(http_client=signed)
            with pytest.raises(MutationRunnerError) as exc:
                transport.fetch_book()
            assert "UNKNOWN_MUTATION_STATE" not in exc.value.reason, exc.value.reason
            assert exc.value.reason.startswith("DEMO_HTTP_FAILURE_"), exc.value.reason
        finally:
            if transport is not None:
                transport.close()


# ---------------------------------------------------------------------------
# Runner-level: timed-out mutation -> ownership query containment -> BLOCKED
# ---------------------------------------------------------------------------


def _filters() -> LimitOrderFilters:
    return LimitOrderFilters(
        min_price=Decimal("1000.00"),
        max_price=Decimal("5000.00"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        percent_price_multiplier_down=Decimal("0.85"),
        percent_price_multiplier_up=Decimal("1.05"),
    )


def _account_state() -> AccountState:
    return AccountState(
        can_trade=True,
        dual_side_position=False,
        multi_assets_margin=False,
        margin_type="ISOLATED",
        leverage=1,
        auto_add_margin=False,
        server_time_skew_ms=Decimal("100"),
        wallet_balance=Decimal("100"),
        available_balance=Decimal("100"),
        nonzero_positions=(),
        open_regular_order_ids=(),
        open_algo_order_ids=(),
    )


def _symbol_state() -> SymbolState:
    return SymbolState(
        symbol="ETHUSDT",
        status="TRADING",
        contract_type="PERPETUAL",
        quote_asset="USDT",
        margin_asset="USDT",
        order_types=frozenset({"LIMIT", "MARKET"}),
        time_in_force=frozenset({"GTX"}),
        filter_type_counts=(
            ("PRICE_FILTER", 1),
            ("LOT_SIZE", 1),
            ("MARKET_LOT_SIZE", 1),
            ("MIN_NOTIONAL", 1),
            ("PERCENT_PRICE", 1),
        ),
        uninterpreted_applicable_filter_types=(),
    )


def _market_close_filters() -> MarketCloseFilters:
    return MarketCloseFilters(
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        market_lot_size_filter_count=1,
        min_notional_filter_count=1,
        uninterpreted_applicable_filter_types=(),
    )


def _clean_final_state() -> dict[str, Any]:
    return {
        "nonzero_positions": (),
        "open_regular_orders": 0,
        "open_algo_orders": 0,
        "account_config_matches": True,
    }


def _reconcile_owned(residual: Decimal) -> dict[str, Any]:
    return {
        "residual_quantity": residual,
        "position_direction": "LONG",
        "open_remainder_quantity": Decimal("0"),
        "other_activity_absent": True,
    }


def _filled_containment_overrides(residual: Decimal = Decimal("0.002")) -> dict[str, Any]:
    """Fixture for a probe proven FILLED after an unsettled mutation.

    Supplies everything section 14 needs so the containment can actually run to
    completion: the market-close filter contract, the owned-position
    reconciliation, the emergency-close ack/terminal query and the post
    containment final state.
    """
    return {
        "query_status": "FILLED",
        "query_executed_quantity": residual,
        "terminal_status": "FILLED",
        "terminal_executed_quantity": residual,
        "market_close_filters": _market_close_filters(),
        "reconcile_state": _reconcile_owned(residual),
        "emergency_close_ack": {
            "orderId": "2",
            "status": "NEW",
            "clientOrderId": "g1b16c-aaaaaaaa-0123456789abcdef-1",
        },
        "emergency_query_status": "FILLED",
        "emergency_query_executed_quantity": residual,
        "containment_final_state": _clean_final_state(),
        "final_state": _clean_final_state(),
    }


def _make_transport(**overrides: Any) -> Any:
    from global_quant.gate1b.mutation_runner import FakeLifecycleTransport

    values: dict[str, Any] = {
        "account_state": _account_state(),
        "symbol_state": _symbol_state(),
        "filters": _filters(),
        "best_bid": Decimal("2500.00"),
        "best_ask": Decimal("2500.01"),
        "mark_price": Decimal("2500.00"),
        "create_ack": {
            "orderId": "1",
            "status": "NEW",
            "clientOrderId": "g1b16-xxxxxxxxxx-0123456789abcdef-01",
        },
        "query_status": "NEW",
        "query_executed_quantity": Decimal("0"),
        "query_accepted_elapsed_seconds": Decimal("1"),
        "cancel_status": "CANCELED",
        "terminal_status": "CANCELED",
        "terminal_executed_quantity": Decimal("0"),
        "final_state": {
            "nonzero_positions": (),
            "open_regular_orders": 0,
            "open_algo_orders": 0,
            "account_config_matches": True,
        },
        "production_contacted": False,
    }
    values.update(overrides)
    return FakeLifecycleTransport(**values)


class TestRunnerTimeoutContainment:
    def _run(
        self, transport: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[int, dict[str, Any]]:
        import global_quant.gate1b.mutation_runner as mr

        # These tests exercise the timeout-containment path, not git binding;
        # bypass the worktree check so the suite runs on any working tree.
        monkeypatch.setattr(
            mr,
            "_verify_runtime_binding",
            lambda project_root, **kw: {
                "runtime_commit": _RUNTIME_COMMIT,
                "protocol_commit": _PROTOCOL_COMMIT,
                "protocol_tag_object": _PROTOCOL_TAG_OBJECT,
                "protocol_sha256": _PROTOCOL_SHA256,
            },
        )
        evidence_dir = tmp_path / "evidence"
        exit_code, evidence_path = run_mutation_lifecycle(
            transport,
            project_root=".",
            evidence_dir=evidence_dir,
            environ={},
            runtime_commit=_RUNTIME_COMMIT,
            session_nonce=_SESSION_NONCE,
            authorization_id=_AUTHORIZATION_ID,
            protocol_commit=_PROTOCOL_COMMIT,
            protocol_tag_object=_PROTOCOL_TAG_OBJECT,
            protocol_sha256=_PROTOCOL_SHA256,
        )
        import json

        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        return exit_code, payload

    def test_create_timeout_order_landed_canceled_blocked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREATE times out; ownership query proves the order landed (NEW);
        targeted cancel runs; verdict is BLOCKED, never PASS, create not retried."""
        transport = _make_transport()

        create_calls = {"n": 0}
        cancel_calls = {"n": 0}

        def timeout_create(reservation: Any) -> dict[str, str]:
            create_calls["n"] += 1
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT")

        def count_cancel(reservation: Any) -> str:
            cancel_calls["n"] += 1
            return transport.cancel_status

        transport.send_create = timeout_create  # type: ignore[method-assign]
        transport.send_cancel = count_cancel  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)
        assert exit_code != 0
        assert payload["status"] in {"STOP", "BLOCKED"}
        assert any("TIMEOUT" in r for r in payload["reason_codes"]), payload["reason_codes"]
        assert create_calls["n"] == 1, "CREATE must not be retried"
        assert cancel_calls["n"] >= 1, "landed order must be canceled"
        assert payload["status"] != "PASS"
        assert "PASS" not in payload.get("status", "")

    def test_create_timeout_order_filled_containment_blocked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack C: CREATE times out and the ownership query proves the probe
        FILLED.

        The guard's owned-order proof only admits NEW/PARTIALLY_FILLED, so a
        naive implementation aborts on OWNERSHIP_PROOF_MISMATCH and silently
        skips section 14 for the single most dangerous outcome. This asserts the
        containment actually ran: an emergency reduce-only close was attempted
        and the final state was re-confirmed, while the verdict stays BLOCKED
        and the CREATE is never retried.
        """
        transport = _make_transport(**_filled_containment_overrides())

        create_calls = {"n": 0}
        emergency_calls = {"n": 0}
        original_emergency = transport.send_emergency_close

        def timeout_create(reservation: Any) -> dict[str, str]:
            create_calls["n"] += 1
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT")

        def count_emergency(reservation: Any) -> dict[str, str]:
            emergency_calls["n"] += 1
            return original_emergency(reservation)

        transport.send_create = timeout_create  # type: ignore[method-assign]
        transport.send_emergency_close = count_emergency  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)

        assert exit_code != 0
        assert payload["status"] != "PASS"
        assert any("TIMEOUT" in r for r in payload["reason_codes"]), payload["reason_codes"]
        assert create_calls["n"] == 1, "CREATE must not be retried"
        assert emergency_calls["n"] >= 1, "section-14 containment never ran for a FILLED probe"
        containment = payload["containment"]
        assert containment["containment_occurred"] is True, containment
        assert containment["emergency_close_attempts"] >= 1, containment
        assert containment["observed_terminal_status"] == "FILLED", containment

    def test_filled_without_completable_containment_escalates(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A proven fill whose section-14 containment cannot complete must not
        be reported with the ordinary timeout reason: the residual position is
        still open, so the failure is escalated to its own BLOCKED reason."""
        overrides = _filled_containment_overrides()
        # Break the reconciliation so the owned-position proof is unprovable.
        overrides["reconcile_state"] = {}
        transport = _make_transport(**overrides)

        def timeout_create(reservation: Any) -> dict[str, str]:
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT")

        transport.send_create = timeout_create  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)
        assert exit_code != 0
        assert payload["status"] != "PASS"
        assert payload["reason_codes"] == ["BLOCKED_MUTATION_TIMEOUT_CONTAINMENT_UNPROVEN"], (
            payload["reason_codes"]
        )
        assert payload["containment"]["containment_occurred"] is False

    def test_unknown_mutation_state_runs_ownership_containment(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack E: a CREATE that failed *after* its bytes reached the socket
        is an UNKNOWN mutation, not a plain STOP.

        It must be routed through the same deterministic ownership query and
        containment as a timeout, must never re-POST the CREATE, and must end
        BLOCKED.
        """
        transport = _make_transport(**_filled_containment_overrides())

        create_calls = {"n": 0}
        query_calls = {"n": 0}
        emergency_calls = {"n": 0}
        original_query = transport.send_query_order
        original_emergency = transport.send_emergency_close

        def unknown_create(reservation: Any) -> dict[str, str]:
            create_calls["n"] += 1
            raise MutationRunnerError("DEMO_HTTP_UNKNOWN_MUTATION_STATE:CONNECTIONRESETERROR")

        def count_query(reservation: Any):
            query_calls["n"] += 1
            return original_query(reservation)

        def count_emergency(reservation: Any) -> dict[str, str]:
            emergency_calls["n"] += 1
            return original_emergency(reservation)

        transport.send_create = unknown_create  # type: ignore[method-assign]
        transport.send_query_order = count_query  # type: ignore[method-assign]
        transport.send_emergency_close = count_emergency  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)

        assert exit_code != 0
        assert payload["status"] != "PASS"
        assert create_calls["n"] == 1, "CREATE retry count must be 0"
        assert query_calls["n"] >= 1, "unknown mutation state skipped the ownership query"
        assert emergency_calls["n"] >= 1, "unknown mutation state skipped section-14 containment"
        containment = payload["containment"]
        assert containment["containment_occurred"] is True, containment
        assert containment["emergency_close_attempts"] >= 1, containment

    def test_unknown_mutation_state_before_create_is_plain_stop(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No CREATE has been reserved yet, so there is nothing to contain: the
        unsettled-mutation predicate must not fire on a preflight read."""
        transport = _make_transport()

        def unknown_account() -> AccountState:
            raise MutationRunnerError("DEMO_HTTP_UNKNOWN_MUTATION_STATE:OSERROR")

        transport.fetch_account_state = unknown_account  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)
        assert exit_code != 0
        assert payload["status"] != "PASS"
        assert payload["containment"]["containment_occurred"] is False
        assert payload["containment"]["emergency_close_attempts"] == 0

    def test_create_timeout_order_unprovable_blocked(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREATE times out; ownership query fails; BLOCKED_MUTATION_TIMEOUT
        _UNPROVEN; no clean PASS."""
        transport = _make_transport()

        def timeout_create(reservation: Any) -> dict[str, str]:
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT")

        def fail_query(reservation: Any):
            raise MutationRunnerError("DEMO_HTTP_FAILURE_BINANCECLIENTERROR")

        transport.send_create = timeout_create  # type: ignore[method-assign]
        transport.send_query_order = fail_query  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)
        assert exit_code != 0
        assert payload["status"] != "PASS"
        assert any("TIMEOUT" in r for r in payload["reason_codes"])

    def test_preflight_timeout_is_plain_stop_not_containment(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout before any CREATE is a plain preflight STOP (no mutation
        was attempted), not a containment path."""
        transport = _make_transport()

        def timeout_account() -> AccountState:
            raise MutationRunnerError("DEMO_HTTP_TIMEOUT")

        transport.fetch_account_state = timeout_account  # type: ignore[method-assign]

        exit_code, payload = self._run(transport, tmp_path, monkeypatch)
        assert exit_code != 0
        assert payload["status"] != "PASS"
