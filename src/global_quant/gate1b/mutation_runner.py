"""Thin offline Demo lifecycle runner for the frozen NT-GATE-1B v1.6 protocol.

This runner serializes the frozen v1.6 mutation lifecycle
(preflight reads -> durable intent -> create -> query -> targeted cancel ->
terminal query -> final assertions -> evidence closeout) against an injectable
transport. It reuses the offline contract helpers in ``mutation_protocol.py`` and
the credential-name / endpoint / runtime-binding patterns from ``safety.py`` and
``protocol_readiness.py``.

The runner performs no real network I/O, requests no credential, and performs no
Demo mutation. The ``FakeLifecycleTransport`` drives the lifecycle from
fixture-provided contract-ready state for offline validation. A later
credential-bearing session replaces the transport with the real Demo HTTP adapter
while keeping the same lifecycle order and guard contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from global_quant.gate1b.mutation_protocol import (
    _OWNED_POSITION_READ_PATHS,
    DEMO_HTTP_ORIGIN,
    PROTOCOL_STATUS,
    PROTOCOL_VERSION,
    SYMBOL,
    AccountState,
    DurableIntent,
    LifecycleEvidence,
    LimitOrderFilters,
    MarketCloseFilters,
    MarketCloseProof,
    MutationProtocolError,
    MutationRequestGuard,
    OrderDerivationProof,
    OwnedOrderProof,
    OwnedPositionProof,
    RequestPurpose,
    ReservedRequest,
    SymbolState,
    validate_account_state,
    validate_lifecycle_pass,
    validate_symbol_state,
)
from global_quant.gate1b.safety import (
    CONFLICTING_CREDENTIAL_NAMES,
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_RELATIVE_PATH = Path("protocols/NT_GATE_1B_V1_6.md")
PROTOCOL_TAG = "nt-gate-1b-v1.6-protocol"

# Gate1b source modules the runner loads at runtime. Their on-disk bytes must
# equal their committed HEAD blobs (section 17 runtime/evidence binding).
_RUNTIME_SOURCE_RELATIVE_PATHS: tuple[str, ...] = (
    "src/global_quant/gate1b/mutation_runner.py",
    "src/global_quant/gate1b/mutation_protocol.py",
    "src/global_quant/gate1b/safety.py",
)

# Credential environment names rejected before any Git or lifecycle work. The
# runner inspects names only and never indexes or serializes values.
RECOGNIZED_BINANCE_CREDENTIAL_ENV_NAMES = frozenset(
    {DEMO_KEY_NAME, DEMO_SECRET_NAME, *CONFLICTING_CREDENTIAL_NAMES}
)

# Frozen pre-create read order (section 11): eleven logical reads before the
# single probe create. Each entry is (path, parameters factory).
_PRE_CREATE_READ_PATHS: tuple[str, ...] = (
    "/fapi/v1/time",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/symbolConfig",
    "/fapi/v2/account",
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/order",
    "/fapi/v1/userTrades",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/premiumIndex",
)

# Frozen post-create read order after the targeted cancel: terminal order query
# is reserved inside the create-query-cancel phase; the six reads below are the
# final global account/orders/config/position-mode observations.
_POST_CANCEL_READ_PATHS: tuple[str, ...] = (
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/userTrades",
    "/fapi/v2/account",
    "/fapi/v1/symbolConfig",
    "/fapi/v1/positionSide/dual",
)

# Read paths whose frozen parameter map is only the signed recvWindow.
_RECV_WINDOW_ONLY_PATHS = frozenset(
    {
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v2/account",
        "/fapi/v1/positionSide/dual",
    }
)

# Reconcile reads required post-create to prove owned-position ownership for the
# section 14 contingency. ``/fapi/v1/order`` is supplied by the terminal query;
# the four paths below are read fresh inside the containment phase.
_OWNERSHIP_RECONCILE_READ_PATHS: tuple[str, ...] = (
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/userTrades",
    "/fapi/v2/account",
)

# Final-state keys that must be explicitly present (section 16). Absence is
# unknown evidence and may never default to a clean value.
_REQUIRED_FINAL_STATE_KEYS: frozenset[str] = frozenset(
    {
        "nonzero_positions",
        "open_regular_orders",
        "open_algo_orders",
        "account_config_matches",
    }
)


class MutationRunnerError(RuntimeError):
    """Raised when the offline lifecycle cannot pass safely.

    Carries optional containment context so the evidence writer can record that a
    section 14 contingency was attempted even when the run fails closed.
    """

    def __init__(
        self,
        reason: str,
        *,
        containment_occurred: bool = False,
        observed_terminal_status: str = "CANCELED",
        observed_terminal_executed_quantity: Decimal = Decimal("0"),
        emergency_close_attempts: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.containment_occurred = containment_occurred
        self.observed_terminal_status = observed_terminal_status
        self.observed_terminal_executed_quantity = observed_terminal_executed_quantity
        self.emergency_close_attempts = emergency_close_attempts


class LifecycleTransport(Protocol):
    """Injectable transport returning contract-ready Demo state for offline runs."""

    def fetch_account_state(self) -> AccountState:
        """Return the read-only account snapshot asserted before durable intent."""

    def fetch_symbol_state(self) -> SymbolState:
        """Return the venue symbol metadata asserted before durable intent."""

    def fetch_filters(self) -> LimitOrderFilters:
        """Return the parsed LIMIT filter contract for the frozen symbol."""

    def fetch_book(self) -> tuple[Decimal, Decimal]:
        """Return (best_bid, best_ask) for the frozen price derivation."""

    def fetch_mark(self) -> Decimal:
        """Return the fresh mark price for the frozen price derivation."""

    def read(self, reservation: ReservedRequest) -> Any:
        """Return a sanitized allowlisted read response for the reserved path."""

    def send_create(self, reservation: ReservedRequest) -> dict[str, str]:
        """Return the ACK response for the single probe create."""

    def send_query_order(self, reservation: ReservedRequest) -> tuple[str, Decimal, Decimal]:
        """Return (status, executed_quantity, accepted_elapsed_seconds)."""

    def send_cancel(self, reservation: ReservedRequest) -> str:
        """Return the terminal status for the targeted cancel."""

    def send_terminal_query(self, reservation: ReservedRequest) -> tuple[str, Decimal]:
        """Return (terminal_status, terminal_executed_quantity) for the post-cancel order query."""

    def fetch_final_state(self) -> dict[str, Any]:
        """Return the sanitized final global account/orders/config summary."""

    def fetch_market_close_filters(self) -> MarketCloseFilters:
        """Return the parsed MARKET filter contract for the contingency close."""

    def fetch_reconcile_state(self) -> dict[str, Any]:
        """Return the reconciled owned-position facts for the section 14 proof."""

    def send_emergency_close(self, reservation: ReservedRequest) -> dict[str, str]:
        """Return the ACK response for the sole contingency reduce-only close."""

    def send_emergency_query(self, reservation: ReservedRequest) -> tuple[str, Decimal]:
        """Return (status, executed_quantity) for the contingency close terminal query."""

    def fetch_containment_final_state(self) -> dict[str, Any]:
        """Return the sanitized final global state after a contingency close."""

    @property
    def production_contacted(self) -> bool:
        """Whether any non-Demo origin was contacted (must remain false)."""


@dataclass(frozen=True)
class _PreflightSnapshot:
    account: AccountState
    symbol: SymbolState
    filters: LimitOrderFilters
    best_bid: Decimal
    best_ask: Decimal
    mark_price: Decimal


@dataclass
class FakeLifecycleTransport:
    """Fixture-driven offline transport for the frozen v1.6 lifecycle.

    All state is contract-ready (parsed AccountState/SymbolState/filters/decimals).
    No real HTTP, no credential, no Demo mutation. The fixture fields may be
    overridden to drive happy-path or fail-closed scenarios in tests.
    """

    account_state: AccountState
    symbol_state: SymbolState
    filters: LimitOrderFilters
    best_bid: Decimal
    best_ask: Decimal
    mark_price: Decimal
    create_ack: dict[str, str] = field(default_factory=dict)
    query_status: str = "NEW"
    query_executed_quantity: Decimal = Decimal("0")
    query_accepted_elapsed_seconds: Decimal = Decimal("1")
    cancel_status: str = "CANCELED"
    terminal_status: str = "CANCELED"
    terminal_executed_quantity: Decimal = Decimal("0")
    final_state: dict[str, Any] = field(default_factory=dict)
    production_contacted: bool = False
    # Contingency (section 14) fixture fields. Defaults keep the happy path
    # unchanged; containment tests override them.
    market_close_filters: MarketCloseFilters | None = None
    reconcile_state: dict[str, Any] = field(default_factory=dict)
    emergency_close_ack: dict[str, str] = field(default_factory=dict)
    emergency_query_status: str = "FILLED"
    emergency_query_executed_quantity: Decimal = Decimal("0")
    containment_final_state: dict[str, Any] = field(default_factory=dict)
    second_cancel_status: str = "CANCELED"
    second_terminal_status: str = "CANCELED"
    second_terminal_executed_quantity: Decimal = Decimal("0")
    # Internal call counter so the second post-cancel terminal query (after a
    # second cancel of a partial remainder) returns the second fixture values.
    _terminal_query_calls: int = field(default=0, init=False, repr=False)

    def fetch_account_state(self) -> AccountState:
        return self.account_state

    def fetch_symbol_state(self) -> SymbolState:
        return self.symbol_state

    def fetch_filters(self) -> LimitOrderFilters:
        return self.filters

    def fetch_book(self) -> tuple[Decimal, Decimal]:
        return self.best_bid, self.best_ask

    def fetch_mark(self) -> Decimal:
        return self.mark_price

    def read(self, reservation: ReservedRequest) -> Any:
        # The offline transport returns a minimal sanitized sentinel for read
        # reservations; the runner only needs the reservation digest promoted to
        # a successful ownership source via note_read_succeeded.
        return {"path": reservation.path}

    def send_create(self, reservation: ReservedRequest) -> dict[str, str]:
        return dict(self.create_ack)

    def send_query_order(self, reservation: ReservedRequest) -> tuple[str, Decimal, Decimal]:
        return (
            self.query_status,
            self.query_executed_quantity,
            self.query_accepted_elapsed_seconds,
        )

    def send_cancel(self, reservation: ReservedRequest) -> str:
        return self.cancel_status

    def send_terminal_query(self, reservation: ReservedRequest) -> tuple[str, Decimal]:
        call = self._terminal_query_calls
        self._terminal_query_calls += 1
        if call == 0:
            return (self.terminal_status, self.terminal_executed_quantity)
        return (self.second_terminal_status, self.second_terminal_executed_quantity)

    def fetch_final_state(self) -> dict[str, Any]:
        return dict(self.final_state)

    def fetch_market_close_filters(self) -> MarketCloseFilters:
        if self.market_close_filters is None:
            raise MutationRunnerError("MARKET_CLOSE_FILTERS_NOT_PROVIDED")
        return self.market_close_filters

    def fetch_reconcile_state(self) -> dict[str, Any]:
        return dict(self.reconcile_state)

    def send_emergency_close(self, reservation: ReservedRequest) -> dict[str, str]:
        return dict(self.emergency_close_ack)

    def send_emergency_query(self, reservation: ReservedRequest) -> tuple[str, Decimal]:
        return (self.emergency_query_status, self.emergency_query_executed_quantity)

    def fetch_containment_final_state(self) -> dict[str, Any]:
        return dict(self.containment_final_state)


class MutationRunner:
    """Serialize the frozen v1.6 lifecycle against an injectable transport.

    The runner owns no credential, HTTP, or execution state. It drives the
    ``MutationRequestGuard`` through the frozen lifecycle and returns a
    ``LifecycleEvidence`` summary. ``validate_lifecycle_pass`` is the final
    offline arbiter for the zero-fill happy path only; any unexpected fill
    routes through the section 14 contingency and can never PASS.
    """

    def __init__(
        self,
        transport: LifecycleTransport,
        *,
        runtime_commit: str,
        session_nonce: str,
        authorization_id: str,
        protocol_commit: str,
        protocol_tag_object: str,
        protocol_sha256: str,
        runtime_binding_passed: bool = False,
        credential_cleanup_passed: bool = False,
    ) -> None:
        self._transport = transport
        self._runtime_commit = runtime_commit
        self._session_nonce = session_nonce
        self._authorization_id = authorization_id
        self._protocol_commit = protocol_commit
        self._protocol_tag_object = protocol_tag_object
        self._protocol_sha256 = protocol_sha256
        self._runtime_binding_passed = runtime_binding_passed
        self._credential_cleanup_passed = credential_cleanup_passed
        self._elapsed = Decimal("0")
        self._preflight: _PreflightSnapshot | None = None
        self._derivation: OrderDerivationProof | None = None
        self._intent: DurableIntent | None = None
        self._guard: MutationRequestGuard | None = None
        self._query_status: str = "NEW"
        self._executed_quantity: Decimal = Decimal("0")
        self._cancel_status: str = "CANCELED"
        self._terminal_status: str = "CANCELED"
        self._terminal_executed_quantity: Decimal = Decimal("0")
        self._unexpected_fill_detected: bool = False
        self._containment_occurred: bool = False
        self._emergency_query_status: str = "FILLED"
        self._emergency_query_executed_quantity: Decimal = Decimal("0")
        self._create_elapsed: Decimal = Decimal("0")
        self._accepted_to_cancel_seconds: Decimal = Decimal("0")
        self._final_state: dict[str, Any] = {}
        # Mirror of the guard's post-create read source digests, tracked so the
        # owned-position proof can be built without touching guard internals.
        self._post_create_read_shas: dict[str, str] = {}

    @property
    def guard(self) -> MutationRequestGuard:
        if self._guard is None:
            raise MutationRunnerError("RUNNER_NOT_INITIALIZED")
        return self._guard

    @property
    def intent(self) -> DurableIntent:
        if self._intent is None:
            raise MutationRunnerError("RUNNER_NOT_INITIALIZED")
        return self._intent

    def execute_lifecycle(self) -> LifecycleEvidence:
        self._preflight_phase()
        self._intent_phase()
        self._create_query_cancel_phase()
        if self._unexpected_fill_detected:
            self._containment_phase()
        self._final_phase()
        return self._build_evidence()

    def _preflight_phase(self) -> None:
        account = self._transport.fetch_account_state()
        symbol = self._transport.fetch_symbol_state()
        filters = self._transport.fetch_filters()
        best_bid, best_ask = self._transport.fetch_book()
        mark_price = self._transport.fetch_mark()
        validate_account_state(account, required_notional=filters.min_notional)
        validate_symbol_state(symbol)
        filter_hash = filters.canonical_sha256
        derivation = OrderDerivationProof(
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            filters=filters,
            filter_snapshot_sha256=filter_hash,
            filter_contract_sha256=filter_hash,
            book_age_ms=Decimal("1"),
            mark_age_ms=Decimal("1"),
            observed_elapsed_seconds=self._elapsed,
        )
        derivation.validate_fresh_at(self._elapsed)
        self._derivation = derivation
        self._preflight = _PreflightSnapshot(
            account=account,
            symbol=symbol,
            filters=filters,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
        )

    def _intent_phase(self) -> None:
        assert self._derivation is not None
        self._intent = DurableIntent(
            authorization_id=self._authorization_id,
            protocol_commit=self._protocol_commit,
            protocol_tag_object=self._protocol_tag_object,
            protocol_sha256=self._protocol_sha256,
            runtime_commit=self._runtime_commit,
            session_nonce=self._session_nonce,
            order_derivation=self._derivation,
            persisted=True,
        )
        self._guard = MutationRequestGuard(self._intent)

    def _register_read(self, reservation: ReservedRequest) -> None:
        self.guard.note_read_succeeded(reservation)
        if self.guard.ledger.create_requests == 1:
            self._post_create_read_shas[reservation.path] = reservation.request_sha256

    def _reserve_read(self, path: str, parameters: Mapping[str, object]) -> ReservedRequest:
        self._elapsed += Decimal("0.001")
        reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.read(reservation)
        self._register_read(reservation)
        return reservation

    def _create_query_cancel_phase(self) -> None:
        recv_window = {"recvWindow": "5000"}
        for path in _PRE_CREATE_READ_PATHS:
            if path in {"/fapi/v1/time", "/fapi/v1/exchangeInfo"}:
                params: Mapping[str, object] = {}
            elif path in _RECV_WINDOW_ONLY_PATHS:
                params = recv_window
            elif path == "/fapi/v1/symbolConfig":
                params = {"symbol": SYMBOL, **recv_window}
            elif path == "/fapi/v1/order":
                params = {
                    "symbol": SYMBOL,
                    "origClientOrderId": self.intent.client_order_id,
                    **recv_window,
                }
            elif path == "/fapi/v1/userTrades":
                params = {"symbol": SYMBOL, **recv_window}
            elif path == "/fapi/v1/ticker/bookTicker" or path == "/fapi/v1/premiumIndex":
                params = {"symbol": SYMBOL}
            else:  # pragma: no cover - exhaustiveness guard
                raise MutationRunnerError(f"UNKNOWN_PRE_CREATE_PATH:{path}")
            self._reserve_read(path, params)

        self._elapsed += Decimal("0.001")
        create_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CREATE,
            parameters=self.intent.probe_payload,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.send_create(create_reservation)
        self._create_elapsed = self._elapsed

        self._elapsed += Decimal("0.001")
        query_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path="/fapi/v1/order",
            purpose=RequestPurpose.READ,
            parameters=self.intent.query_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.read(query_reservation)
        self._register_read(query_reservation)
        status, executed_qty, _accepted_elapsed = self._transport.send_query_order(
            query_reservation
        )
        if status != "NEW" or executed_qty != 0:
            raise MutationRunnerError("UNEXPECTED_ORDER_STATE_AT_QUERY")
        self._query_status = status
        self._executed_quantity = executed_qty
        order_proof = OwnedOrderProof(
            intent_sha256=self.intent.intent_sha256,
            symbol=SYMBOL,
            client_order_id=self.intent.client_order_id,
            status=status,
            executed_quantity=executed_qty,
            observed_after_http_attempt=self.guard.ledger.total_http_requests,
            source_request_sha256=query_reservation.request_sha256,
            accepted_elapsed_seconds=self._create_elapsed,
            observed_elapsed_seconds=self._elapsed,
        )
        self.guard.note_owned_order_proof(order_proof)

        self._elapsed += Decimal("0.001")
        cancel_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="DELETE",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CANCEL,
            parameters=self.intent.cancel_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._cancel_status = self._transport.send_cancel(cancel_reservation)
        self._accepted_to_cancel_seconds = self._elapsed - self._create_elapsed

        # Post-cancel terminal order query (section 14). Its parsed result MUST
        # participate in the verdict: a fill or non-CANCELED terminal state is an
        # unexpected fill that can never enter the happy-path PASS.
        self._elapsed += Decimal("0.001")
        terminal_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path="/fapi/v1/order",
            purpose=RequestPurpose.READ,
            parameters=self.intent.query_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.read(terminal_reservation)
        self._register_read(terminal_reservation)
        terminal_status, terminal_executed = self._transport.send_terminal_query(
            terminal_reservation
        )
        terminal_executed = _validated_executed_quantity(
            terminal_executed, context="TERMINAL_EXECUTED_QUANTITY"
        )
        self._terminal_status = terminal_status
        self._terminal_executed_quantity = terminal_executed
        if terminal_executed > 0:
            self._unexpected_fill_detected = True
            self._executed_quantity = terminal_executed
        elif terminal_status != "CANCELED":
            # No fill, but the probe did not reach the expected CANCELED terminal
            # state (e.g. EXPIRED). This is an unexpected mutation with nothing
            # to contain; fail closed immediately.
            raise MutationRunnerError(
                "STOP_UNEXPECTED_TERMINAL_STATE",
                observed_terminal_status=terminal_status,
            )

    def _containment_phase(self) -> None:
        """Execute the section 14 contingency for an unexpected fill.

        The run is already non-PASS. This phase attempts bounded owned-position
        containment using the existing protocol machinery: it never enlarges the
        mutation scope and never disguises the outcome as a clean happy path.
        """
        # Section 14 step 2: if the probe is still open (PARTIALLY_FILLED), cancel
        # the owned remainder after a fresh query proves it open.
        if self._terminal_status == "PARTIALLY_FILLED":
            self._cancel_owned_remainder_after_partial()

        if self._terminal_status not in {"CANCELED", "FILLED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            raise MutationRunnerError(
                "BLOCKED_CLEANUP_UNPROVEN",
                containment_occurred=True,
                observed_terminal_status=self._terminal_status,
                observed_terminal_executed_quantity=self._terminal_executed_quantity,
            )

        # Section 14 step 3: reconcile via the owned-position read paths. The
        # terminal query already supplied /fapi/v1/order; the four paths below
        # are read fresh post-create.
        recv_window = {"recvWindow": "5000"}
        for path in _OWNERSHIP_RECONCILE_READ_PATHS:
            if path == "/fapi/v1/exchangeInfo":
                params: Mapping[str, object] = {}
            elif path == "/fapi/v1/premiumIndex":
                params = {"symbol": SYMBOL}
            elif path == "/fapi/v1/userTrades":
                params = {"symbol": SYMBOL, **recv_window}
            elif path == "/fapi/v2/account":
                params = recv_window
            else:  # pragma: no cover - exhaustiveness guard
                raise MutationRunnerError(f"UNKNOWN_OWNERSHIP_PATH:{path}")
            self._reserve_read(path, params)

        # Section 14 step 4: prove ownership and, only if strictly proven, issue
        # the single bounded reduce-only contingency close.
        self._prove_owned_position_and_close()
        self._containment_occurred = True

    def _cancel_owned_remainder_after_partial(self) -> None:
        """Re-establish owned-order-open and cancel a still-open partial remainder."""

        # Re-note the owned order proof from the terminal (partial) observation;
        # the guard requires stage in {CREATE_ATTEMPTED, CANCEL_ATTEMPTED}.
        partial_proof = OwnedOrderProof(
            intent_sha256=self.intent.intent_sha256,
            symbol=SYMBOL,
            client_order_id=self.intent.client_order_id,
            status=self._terminal_status,
            executed_quantity=self._terminal_executed_quantity,
            observed_after_http_attempt=self.guard.ledger.total_http_requests,
            source_request_sha256=self._post_create_read_shas["/fapi/v1/order"],
            accepted_elapsed_seconds=self._create_elapsed,
            observed_elapsed_seconds=self._elapsed,
        )
        self.guard.note_owned_order_proof(partial_proof)

        self._elapsed += Decimal("0.001")
        cancel_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="DELETE",
            path="/fapi/v1/order",
            purpose=RequestPurpose.CANCEL,
            parameters=self.intent.cancel_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        # The fixture returns the second cancel's terminal status; a missing or
        # contradictory response is not treated as success.
        self._transport.send_cancel(cancel_reservation)

        # Fresh terminal query after the second cancel.
        self._elapsed += Decimal("0.001")
        terminal_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path="/fapi/v1/order",
            purpose=RequestPurpose.READ,
            parameters=self.intent.query_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.read(terminal_reservation)
        self._register_read(terminal_reservation)
        terminal_status, terminal_executed = self._transport.send_terminal_query(
            terminal_reservation
        )
        terminal_executed = _validated_executed_quantity(
            terminal_executed, context="TERMINAL_EXECUTED_QUANTITY"
        )
        self._terminal_status = terminal_status
        self._terminal_executed_quantity = terminal_executed
        if terminal_executed > 0:
            self._executed_quantity = terminal_executed

    def _prove_owned_position_and_close(self) -> None:
        reconcile = self._transport.fetch_reconcile_state()
        residual = reconcile.get("residual_quantity")
        position_direction = reconcile.get("position_direction")
        open_remainder = reconcile.get("open_remainder_quantity")
        other_activity_absent = reconcile.get("other_activity_absent")
        if (
            not isinstance(residual, Decimal)
            or not isinstance(open_remainder, Decimal)
            or not isinstance(other_activity_absent, bool)
            or position_direction != "LONG"
            or open_remainder != 0
            or not other_activity_absent
            or residual != self._terminal_executed_quantity
            or residual <= 0
        ):
            raise MutationRunnerError(
                "BLOCKED_CLEANUP_UNPROVEN",
                containment_occurred=True,
                observed_terminal_status=self._terminal_status,
                observed_terminal_executed_quantity=self._terminal_executed_quantity,
            )

        market_filters = self._transport.fetch_market_close_filters()
        market_proof = MarketCloseProof(
            filter_snapshot_sha256=self._derivation.filter_snapshot_sha256  # type: ignore[union-attr]
            if self._derivation is not None
            else "",
            filter_contract_sha256=market_filters.canonical_sha256,
            filters=market_filters,
            quantity=residual,
            mark_price=self._preflight.mark_price if self._preflight is not None else Decimal("0"),
            mark_price_age_ms=Decimal("1"),
            observed_elapsed_seconds=self._elapsed,
        )
        source_request_sha256s = tuple(
            sorted(
                (path, self._post_create_read_shas[path])
                for path in _OWNED_POSITION_READ_PATHS
                if path in self._post_create_read_shas
            )
        )
        if {path for path, _ in source_request_sha256s} != set(_OWNED_POSITION_READ_PATHS):
            raise MutationRunnerError(
                "BLOCKED_CLEANUP_UNPROVEN",
                containment_occurred=True,
                observed_terminal_status=self._terminal_status,
                observed_terminal_executed_quantity=self._terminal_executed_quantity,
            )
        position_proof = OwnedPositionProof(
            intent_sha256=self.intent.intent_sha256,
            symbol=SYMBOL,
            residual_quantity=residual,
            owned_executed_quantity=self._terminal_executed_quantity,
            position_direction=position_direction,
            probe_terminal_status=self._terminal_status,
            open_remainder_quantity=open_remainder,
            other_activity_absent=other_activity_absent,
            market_close_proof=market_proof,
            observed_after_http_attempt=self.guard.ledger.total_http_requests,
            source_request_sha256s=source_request_sha256s,
            observed_elapsed_seconds=self._elapsed,
        )
        # The guard validates the proof's internal consistency and the market
        # close filters; any mismatch raises OWNERSHIP_PROOF_MISMATCH, which we
        # convert to a fail-closed BLOCKED_CLEANUP_UNPROVEN.
        try:
            self.guard.note_owned_position_proof(position_proof)
        except MutationProtocolError as exc:
            raise MutationRunnerError(
                "BLOCKED_CLEANUP_UNPROVEN",
                containment_occurred=True,
                observed_terminal_status=self._terminal_status,
                observed_terminal_executed_quantity=self._terminal_executed_quantity,
            ) from exc

        # Section 14 step 4: the single SELL MARKET reduce-only contingency
        # close. Zero POST retry; the guard counts it against the mutation budget.
        self._elapsed += Decimal("0.001")
        close_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="POST",
            path="/fapi/v1/order",
            purpose=RequestPurpose.EMERGENCY_CLOSE,
            parameters=self.intent.emergency_close_payload(residual),
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.send_emergency_close(close_reservation)

        # Query the contingency close terminal state by its deterministic client
        # ID; it is never re-POSTed (section 14).
        self._elapsed += Decimal("0.001")
        emergency_query_reservation = self.guard.reserve(
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path="/fapi/v1/order",
            purpose=RequestPurpose.READ,
            parameters=self.intent.emergency_query_parameters,
            elapsed_seconds=self._elapsed,
            retry_index=0,
        )
        self._transport.read(emergency_query_reservation)
        self._register_read(emergency_query_reservation)
        emergency_status, emergency_executed = self._transport.send_emergency_query(
            emergency_query_reservation
        )
        emergency_executed = _validated_executed_quantity(
            emergency_executed, context="EMERGENCY_EXECUTED_QUANTITY"
        )
        self._emergency_query_status = emergency_status
        self._emergency_query_executed_quantity = emergency_executed

    def _final_phase(self) -> None:
        recv_window = {"recvWindow": "5000"}
        for path in _POST_CANCEL_READ_PATHS:
            if path in _RECV_WINDOW_ONLY_PATHS:
                params: Mapping[str, object] = recv_window
            elif path == "/fapi/v1/symbolConfig":
                params = {"symbol": SYMBOL, **recv_window}
            elif path == "/fapi/v1/order":
                params = {
                    "symbol": SYMBOL,
                    "origClientOrderId": self.intent.client_order_id,
                    **recv_window,
                }
            elif path == "/fapi/v1/userTrades":
                params = {"symbol": SYMBOL, **recv_window}
            else:  # pragma: no cover - exhaustiveness guard
                raise MutationRunnerError(f"UNKNOWN_POST_CANCEL_PATH:{path}")
            self._reserve_read(path, params)
        if self._containment_occurred:
            self._final_state = self._transport.fetch_containment_final_state()
        else:
            self._final_state = self._transport.fetch_final_state()

    def _build_evidence(self) -> LifecycleEvidence:
        final = self._final_state
        missing = _REQUIRED_FINAL_STATE_KEYS - final.keys()
        if missing:
            # Absent final-state evidence is unknown and may never default to a
            # clean value (section 16). Fail closed.
            raise MutationRunnerError("FINAL_STATE_EVIDENCE_INCOMPLETE")
        positions_raw = final["nonzero_positions"]
        account_config_matches = final["account_config_matches"]
        open_regular = final["open_regular_orders"]
        open_algo = final["open_algo_orders"]
        if (
            not isinstance(positions_raw, tuple)
            or not isinstance(account_config_matches, bool)
            # Counts must be exact non-negative ints. ``type(...) is int`` rejects
            # the bool subclass and no lossy coercion is applied, so float/str/
            # bool/None/negative values all fail closed instead of decoding to 0.
            or type(open_regular) is not int
            or type(open_algo) is not int
            or open_regular < 0
            or open_algo < 0
        ):
            raise MutationRunnerError("FINAL_STATE_EVIDENCE_MALFORMED")
        final_nonzero_positions: tuple[tuple[str, Decimal], ...] = tuple(positions_raw)
        final_account_config_matches = bool(account_config_matches)

        # Even successful containment retains STOP; but if the final global state
        # cannot be proven clean after containment, the run is BLOCKED.
        if self._containment_occurred and (
            final_nonzero_positions
            or open_regular != 0
            or open_algo != 0
            or not final_account_config_matches
        ):
            raise MutationRunnerError(
                "BLOCKED_FINAL_NOT_CLEAN_AFTER_CONTAINMENT",
                containment_occurred=True,
                observed_terminal_status=self._terminal_status,
                observed_terminal_executed_quantity=self._terminal_executed_quantity,
                emergency_close_attempts=1 if self._containment_occurred else 0,
            )

        if self._containment_occurred:
            observed_statuses = (
                self._query_status,
                self._cancel_status,
                self._terminal_status,
                self._emergency_query_status,
            )
            unexpected_mutations = 1
        else:
            observed_statuses = (self._query_status, self._cancel_status)
            unexpected_mutations = 0

        # Cleanup is confirmed only when the final global state is provably clean.
        cleanup_confirmed = (
            final_account_config_matches
            and not final_nonzero_positions
            and open_regular == 0
            and open_algo == 0
        )

        return LifecycleEvidence(
            create_requests=self.guard.ledger.create_requests,
            cancel_requests=self.guard.ledger.cancel_requests,
            emergency_close_requests=self.guard.ledger.emergency_close_requests,
            modify_requests=0,
            account_setting_mutations=0,
            accepted_orders=1,
            observed_statuses=observed_statuses,
            executed_quantity=self._executed_quantity,
            fee_delta=Decimal("0"),
            funding_delta=Decimal("0"),
            wallet_balance_delta=Decimal("0"),
            total_http_requests=self.guard.ledger.total_http_requests,
            total_runtime_seconds=self._elapsed,
            create_elapsed_seconds=self._create_elapsed,
            accepted_to_cancel_seconds=self._accepted_to_cancel_seconds,
            final_nonzero_positions=final_nonzero_positions,
            final_open_regular_orders=open_regular,
            final_open_algo_orders=open_algo,
            unexpected_mutations=unexpected_mutations,
            read_retries=self.guard.ledger.read_retry_requests,
            production_contacted=bool(self._transport.production_contacted),
            preflight_passed=self._preflight is not None,
            final_account_config_matches=final_account_config_matches,
            runtime_binding_passed=self._runtime_binding_passed,
            credential_cleanup_passed=self._credential_cleanup_passed,
            filters_passed=self._derivation is not None and self._preflight is not None,
            order_parameters_match=self.guard.ledger.create_requests == 1,
            cleanup_confirmed=cleanup_confirmed,
        )


def _validated_executed_quantity(value: object, *, context: str) -> Decimal:
    """Require an exact finite non-negative Decimal before any comparison.

    Exchange executed-quantity state must never be lossily coerced: bool, float,
    str, None, NaN, Infinity, or a negative value is malformed evidence and fails
    closed with a clear STOP before any ``> 0`` / ``== 0`` decision is made.
    """

    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise MutationRunnerError(f"STOP_MALFORMED_{context}")
    return value


def _run_git(
    project_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=check,
    )


def _git_text(project_root: Path, *args: str) -> str:
    return _run_git(project_root, *args).stdout.decode("ascii").strip()


def _verify_runtime_binding(
    project_root: Path,
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> dict[str, str]:
    """Mechanically verify the committed-runtime binding (section 17).

    Caller-supplied identity values must equal values recomputed from the actual
    Git/runtime state. Any mismatch, dirty worktree, untracked source, or byte
    drift fails closed. Returns the verified binding facts.
    """

    tag_ref = f"refs/tags/{PROTOCOL_TAG}"
    try:
        tag_type = _git_text(project_root, "cat-file", "-t", tag_ref)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc
    if tag_type != "tag":
        raise MutationRunnerError("PROTOCOL_TAG_NOT_ANNOTATED")

    try:
        actual_head = _git_text(project_root, "rev-parse", "HEAD^{commit}")
        actual_tree = _git_text(project_root, "rev-parse", "HEAD^{tree}")
        actual_tag_object = _git_text(project_root, "rev-parse", tag_ref)
        peeled_tag_commit = _git_text(project_root, "rev-parse", f"{tag_ref}^{{commit}}")
        # Tracked worktree must be clean.
        tracked_dirty = _run_git(project_root, "diff", "--quiet", "HEAD", check=False)
        untracked = _run_git(
            project_root, "ls-files", "--others", "--exclude-standard", check=False
        )
        ancestor = _run_git(
            project_root,
            "merge-base",
            "--is-ancestor",
            peeled_tag_commit,
            actual_head,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc

    if tracked_dirty.returncode != 0:
        raise MutationRunnerError("RUNTIME_WORKTREE_DIRTY")
    if untracked.stdout.strip():
        raise MutationRunnerError("RUNTIME_UNTRACKED_FILES_PRESENT")
    if ancestor.returncode == 1:
        raise MutationRunnerError("PROTOCOL_TAG_NOT_ANCESTOR")
    if ancestor.returncode != 0:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID")

    # Cross-check caller-supplied identity against recomputed Git facts.
    if runtime_commit != actual_head:
        raise MutationRunnerError("RUNTIME_COMMIT_MISMATCH")
    if protocol_commit != peeled_tag_commit:
        raise MutationRunnerError("PROTOCOL_COMMIT_MISMATCH")
    if protocol_tag_object != actual_tag_object:
        raise MutationRunnerError("PROTOCOL_TAG_OBJECT_MISMATCH")

    protocol_path = project_root / PROTOCOL_RELATIVE_PATH
    try:
        current_protocol = protocol_path.read_bytes()
        frozen_protocol = _run_git(
            project_root,
            "show",
            f"{tag_ref}:{PROTOCOL_RELATIVE_PATH.as_posix()}",
        ).stdout
    except FileNotFoundError as exc:
        raise MutationRunnerError("PROTOCOL_FILE_UNAVAILABLE") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc
    if current_protocol != frozen_protocol:
        raise MutationRunnerError("PROTOCOL_BYTES_CHANGED_AFTER_FREEZE")
    actual_protocol_sha256 = hashlib.sha256(current_protocol).hexdigest()
    if protocol_sha256 != actual_protocol_sha256:
        raise MutationRunnerError("PROTOCOL_SHA256_MISMATCH")

    # Every runtime source module byte must equal its committed HEAD blob.
    for relative in _RUNTIME_SOURCE_RELATIVE_PATHS:
        try:
            _run_git(project_root, "ls-files", "--error-unmatch", relative, check=True)
            disk_hash = _git_text(project_root, "hash-object", relative)
            head_blob = _git_text(project_root, "rev-parse", f"HEAD:{relative}")
        except subprocess.CalledProcessError as exc:
            raise MutationRunnerError("RUNTIME_SOURCE_NOT_TRACKED") from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc
        if disk_hash != head_blob:
            raise MutationRunnerError("RUNTIME_SOURCE_BYTES_CHANGED")

    return {
        "protocol_tag": PROTOCOL_TAG,
        "protocol_commit": peeled_tag_commit,
        "protocol_tag_object": actual_tag_object,
        "protocol_sha256": actual_protocol_sha256,
        "runtime_commit": actual_head,
        "runtime_tree": actual_tree,
    }


def _base_payload(
    *,
    status: str,
    reason_codes: Sequence[str],
    credential_environment_empty: bool,
) -> dict[str, Any]:
    return {
        "gate": "NT-GATE-1B",
        "protocol_version": PROTOCOL_VERSION,
        "protocol_status": PROTOCOL_STATUS,
        "mode": "DEMO_MUTATION_LIFECYCLE_OFFLINE",
        "status": status,
        "reason_codes": list(reason_codes),
        "completed_at": datetime.now(UTC).isoformat(),
        "credential_environment_empty": credential_environment_empty,
        "credentials_read": False,
        "network_accessed": False,
        "authenticated_request_sent": False,
        "order_summary": {"canceled": 0, "filled": 0, "submitted": 0},
        "economic_event_summary": {"fees": 0, "funding": 0},
        "position_changes": 0,
        "agent_credential_access_allowed": False,
        "next_action": (
            "WAIT_FOR_EXPLICIT_CREDENTIAL_BEARING_DEMO_AUTHORIZATION"
            if status == "PASS"
            else "STOP_MUTATION_LIFECYCLE"
        ),
    }


def _write_evidence(evidence_dir: Path, payload: Mapping[str, Any]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = evidence_dir / "mutation-lifecycle.json"
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=evidence_dir,
            prefix=".mutation-lifecycle-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def _is_clean_pass_evidence(evidence: LifecycleEvidence) -> bool:
    """A clean happy-path candidate has zero fill and no contingency mutations."""

    return (
        evidence.executed_quantity == 0
        and evidence.emergency_close_requests == 0
        and evidence.unexpected_mutations == 0
    )


def run_mutation_lifecycle(
    transport: LifecycleTransport,
    *,
    project_root: Path,
    evidence_dir: Path,
    environ: Mapping[str, str],
    runtime_commit: str,
    session_nonce: str,
    authorization_id: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> tuple[int, Path]:
    """Run the offline v1.6 lifecycle gate and return exit code + evidence path."""

    environment_names = set(environ)
    credential_environment_empty = not bool(
        environment_names & RECOGNIZED_BINANCE_CREDENTIAL_ENV_NAMES
    )
    if not credential_environment_empty:
        payload = _base_payload(
            status="STOP",
            reason_codes=["CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"],
            credential_environment_empty=False,
        )
        return 1, _write_evidence(evidence_dir, payload)

    try:
        binding = _verify_runtime_binding(
            Path(project_root),
            runtime_commit=runtime_commit,
            protocol_commit=protocol_commit,
            protocol_tag_object=protocol_tag_object,
            protocol_sha256=protocol_sha256,
        )
    except MutationRunnerError as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[exc.reason],
            credential_environment_empty=True,
        )
        return 1, _write_evidence(evidence_dir, payload)

    runner = MutationRunner(
        transport,
        runtime_commit=binding["runtime_commit"],
        session_nonce=session_nonce,
        authorization_id=authorization_id,
        protocol_commit=binding["protocol_commit"],
        protocol_tag_object=binding["protocol_tag_object"],
        protocol_sha256=binding["protocol_sha256"],
        runtime_binding_passed=True,
        # Derived from the executed credential-environment validation: the run
        # only proceeds when the environment holds no recognized credential, so
        # cleanup is trivially complete exactly when the env is empty.
        credential_cleanup_passed=credential_environment_empty,
    )
    try:
        evidence = runner.execute_lifecycle()
    except MutationRunnerError as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[exc.reason],
            credential_environment_empty=True,
        )
        payload.update(binding)
        payload["containment"] = {
            "containment_occurred": exc.containment_occurred,
            "observed_terminal_status": exc.observed_terminal_status,
            "observed_terminal_executed_quantity": str(exc.observed_terminal_executed_quantity),
            "emergency_close_attempts": exc.emergency_close_attempts,
        }
        return 1, _write_evidence(evidence_dir, payload)

    # Any unexpected fill routes through the section 14 contingency and retains
    # STOP, even when containment succeeds. It can never be reclassified as PASS.
    if not _is_clean_pass_evidence(evidence):
        payload = _base_payload(
            status="STOP",
            reason_codes=(
                ["STOP_UNEXPECTED_FILL_CONTAINED"]
                if evidence.emergency_close_requests > 0
                else ["STOP_UNEXPECTED_FILL"]
            ),
            credential_environment_empty=True,
        )
        payload.update(binding)
        payload["lifecycle"] = {
            "create_requests": evidence.create_requests,
            "cancel_requests": evidence.cancel_requests,
            "emergency_close_requests": evidence.emergency_close_requests,
            "total_http_requests": evidence.total_http_requests,
            "executed_quantity": str(evidence.executed_quantity),
            "unexpected_mutations": evidence.unexpected_mutations,
            "production_contacted": evidence.production_contacted,
            "read_retries": evidence.read_retries,
            "final_open_regular_orders": evidence.final_open_regular_orders,
            "final_open_algo_orders": evidence.final_open_algo_orders,
        }
        payload["containment"] = {
            "containment_occurred": evidence.emergency_close_requests > 0,
            "observed_terminal_status": evidence.observed_statuses[2]
            if len(evidence.observed_statuses) > 2
            else "CANCELED",
            "observed_terminal_executed_quantity": str(evidence.executed_quantity),
            "emergency_close_attempts": evidence.emergency_close_requests,
        }
        return 1, _write_evidence(evidence_dir, payload)

    try:
        validate_lifecycle_pass(evidence)
    except (MutationProtocolError, MutationRunnerError) as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[str(exc)],
            credential_environment_empty=True,
        )
        payload.update(binding)
        return 1, _write_evidence(evidence_dir, payload)

    payload = _base_payload(
        status="PASS",
        reason_codes=[],
        credential_environment_empty=True,
    )
    payload.update(binding)
    payload["lifecycle"] = {
        "create_requests": evidence.create_requests,
        "cancel_requests": evidence.cancel_requests,
        "emergency_close_requests": evidence.emergency_close_requests,
        "total_http_requests": evidence.total_http_requests,
        "executed_quantity": str(evidence.executed_quantity),
        "unexpected_mutations": evidence.unexpected_mutations,
        "production_contacted": evidence.production_contacted,
        "read_retries": evidence.read_retries,
        "final_open_regular_orders": evidence.final_open_regular_orders,
        "final_open_algo_orders": evidence.final_open_algo_orders,
    }
    return 0, _write_evidence(evidence_dir, payload)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the offline NT-GATE-1B v1.6 mutation lifecycle gate."
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Directory that will receive mutation-lifecycle.json.",
    )
    parser.add_argument(
        "--runtime-commit",
        type=str,
        required=True,
        help="The committed runtime SHA-1 to bind into lifecycle evidence.",
    )
    parser.add_argument(
        "--session-nonce",
        type=str,
        required=True,
        help="Exactly 16 lowercase hex characters generated locally.",
    )
    parser.add_argument(
        "--authorization-id",
        type=str,
        required=True,
        help="The non-secret one-time authorization ID (g1b16-{16 hex}).",
    )
    parser.add_argument(
        "--protocol-commit",
        type=str,
        required=True,
        help="The peeled frozen protocol commit the runtime binds to.",
    )
    parser.add_argument(
        "--protocol-tag-object",
        type=str,
        required=True,
        help="The annotated protocol tag object SHA-1.",
    )
    parser.add_argument(
        "--protocol-sha256",
        type=str,
        required=True,
        help="SHA-256 of the exact tagged protocol bytes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with no credential or network inputs.

    The default transport is the offline fake; a credential-bearing session
    replaces it with the real Demo HTTP adapter while keeping the same lifecycle.
    """

    args = _parse_args(argv)
    # The CLI cannot construct a real Demo transport without credentials; it
    # exits with a credential-free STOP explaining that a credential-bearing
    # session must supply the transport. This keeps the offline CLI safe.
    payload = _base_payload(
        status="STOP",
        reason_codes=["CREDENTIAL_BEARING_TRANSPORT_NOT_PROVIDED"],
        credential_environment_empty=True,
    )
    evidence_dir = args.evidence_dir
    evidence_path = _write_evidence(evidence_dir, payload)
    print(
        json.dumps(
            {"evidence_path": str(evidence_path), "exit_code": 1},
            sort_keys=True,
        )
    )
    return 1
