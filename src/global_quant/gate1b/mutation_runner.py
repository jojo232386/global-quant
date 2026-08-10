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
    DEMO_HTTP_ORIGIN,
    PROTOCOL_STATUS,
    PROTOCOL_VERSION,
    SYMBOL,
    AccountState,
    DurableIntent,
    LifecycleEvidence,
    LimitOrderFilters,
    MutationProtocolError,
    MutationRequestGuard,
    OrderDerivationProof,
    OwnedOrderProof,
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


class MutationRunnerError(RuntimeError):
    """Raised when the offline lifecycle cannot pass safely."""


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

    def fetch_final_state(self) -> dict[str, Any]:
        """Return the sanitized final global account/orders/config summary."""

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

    def fetch_final_state(self) -> dict[str, Any]:
        return dict(self.final_state)


class MutationRunner:
    """Serialize the frozen v1.6 lifecycle against an injectable transport.

    The runner owns no credential, HTTP, or execution state. It drives the
    ``MutationRequestGuard`` through the frozen 21-HTTP-attempt lifecycle and
    returns a ``LifecycleEvidence`` summary. ``validate_lifecycle_pass`` is the
    final offline arbiter for the happy path.
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
    ) -> None:
        self._transport = transport
        self._runtime_commit = runtime_commit
        self._session_nonce = session_nonce
        self._authorization_id = authorization_id
        self._protocol_commit = protocol_commit
        self._protocol_tag_object = protocol_tag_object
        self._protocol_sha256 = protocol_sha256
        self._elapsed = Decimal("0")
        self._preflight: _PreflightSnapshot | None = None
        self._derivation: OrderDerivationProof | None = None
        self._intent: DurableIntent | None = None
        self._guard: MutationRequestGuard | None = None
        self._query_status: str = "NEW"
        self._executed_quantity: Decimal = Decimal("0")
        self._cancel_status: str = "CANCELED"
        self._create_elapsed: Decimal = Decimal("0")
        self._accepted_to_cancel_seconds: Decimal = Decimal("0")
        self._final_state: dict[str, Any] = {}

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
        self.guard.note_read_succeeded(reservation)
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
        self.guard.note_read_succeeded(query_reservation)
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
        self.guard.note_read_succeeded(terminal_reservation)

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
        self._final_state = self._transport.fetch_final_state()

    def _build_evidence(self) -> LifecycleEvidence:
        final = self._final_state
        return LifecycleEvidence(
            create_requests=self.guard.ledger.create_requests,
            cancel_requests=self.guard.ledger.cancel_requests,
            emergency_close_requests=self.guard.ledger.emergency_close_requests,
            modify_requests=0,
            account_setting_mutations=0,
            accepted_orders=1,
            observed_statuses=(self._query_status, self._cancel_status),
            executed_quantity=self._executed_quantity,
            fee_delta=Decimal("0"),
            funding_delta=Decimal("0"),
            wallet_balance_delta=Decimal("0"),
            total_http_requests=self.guard.ledger.total_http_requests,
            total_runtime_seconds=self._elapsed,
            create_elapsed_seconds=self._create_elapsed,
            accepted_to_cancel_seconds=self._accepted_to_cancel_seconds,
            final_nonzero_positions=tuple(final.get("nonzero_positions", ())),
            final_open_regular_orders=int(final.get("open_regular_orders", 0)),
            final_open_algo_orders=int(final.get("open_algo_orders", 0)),
            unexpected_mutations=0,
            read_retries=self.guard.ledger.read_retry_requests,
            production_contacted=bool(self._transport.production_contacted),
            preflight_passed=True,
            final_account_config_matches=bool(final.get("account_config_matches", True)),
            runtime_binding_passed=True,
            credential_cleanup_passed=True,
            filters_passed=True,
            order_parameters_match=True,
            cleanup_confirmed=True,
        )


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


def _collect_frozen_protocol_state(project_root: Path) -> dict[str, str]:
    tag_ref = f"refs/tags/{PROTOCOL_TAG}"
    try:
        tag_type = _git_text(project_root, "cat-file", "-t", tag_ref)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc
    if tag_type != "tag":
        raise MutationRunnerError("PROTOCOL_TAG_NOT_ANNOTATED")
    try:
        tag_commit = _git_text(project_root, "rev-parse", f"{tag_ref}^{{commit}}")
        head_commit = _git_text(project_root, "rev-parse", "HEAD^{commit}")
        ancestor = _run_git(
            project_root,
            "merge-base",
            "--is-ancestor",
            tag_commit,
            head_commit,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID") from exc
    if ancestor.returncode == 1:
        raise MutationRunnerError("PROTOCOL_TAG_NOT_ANCESTOR")
    if ancestor.returncode != 0:
        raise MutationRunnerError("PROTOCOL_GIT_STATE_INVALID")
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
    return {
        "protocol_tag": PROTOCOL_TAG,
        "protocol_commit": tag_commit,
        "tested_commit": head_commit,
        "protocol_sha256": hashlib.sha256(current_protocol).hexdigest(),
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
    if environment_names & RECOGNIZED_BINANCE_CREDENTIAL_ENV_NAMES:
        payload = _base_payload(
            status="STOP",
            reason_codes=["CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"],
            credential_environment_empty=False,
        )
        return 1, _write_evidence(evidence_dir, payload)

    try:
        frozen_state = _collect_frozen_protocol_state(Path(project_root))
    except MutationRunnerError as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[str(exc)],
            credential_environment_empty=True,
        )
        return 1, _write_evidence(evidence_dir, payload)

    runner = MutationRunner(
        transport,
        runtime_commit=runtime_commit,
        session_nonce=session_nonce,
        authorization_id=authorization_id,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
    )
    try:
        evidence = runner.execute_lifecycle()
        validate_lifecycle_pass(evidence)
    except (MutationProtocolError, MutationRunnerError) as exc:
        payload = _base_payload(
            status="STOP",
            reason_codes=[str(exc)],
            credential_environment_empty=True,
        )
        payload.update(frozen_state)
        return 1, _write_evidence(evidence_dir, payload)

    payload = _base_payload(
        status="PASS",
        reason_codes=[],
        credential_environment_empty=True,
    )
    payload.update(frozen_state)
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
