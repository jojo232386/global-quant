"""Credential-free final-evidence and verdict primitives for Gate 1B v1.6.

This module deliberately does not perform venue reads and cannot declare a Gate
PASS.  It verifies sanitized, typed results produced by fresh read reservations,
requires a hash-bound exact-reap attestation from the process controller, and
can emit only ``READY_FOR_INDEPENDENT_REVIEW`` or ``BLOCKED``.

Raw credentials must never be supplied to this module.  ``canary_tokens`` are
synthetic, non-secret leak sentinels used to prove that retained artifacts do
not contain credential-like material.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Any

from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.durable_intent import (
    DurableIntentError,
    PersistedIntent,
    load_persisted_intent,
)
from global_quant.gate1b.execution_journal import (
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    IntentChainBinding,
    JournalRecord,
    MutationAttempt,
    MutationReservationProof,
    PreIntentReadReservation,
    PreIntentReadResult,
    ReadKind,
    ReadOutcome,
    ReadPurpose,
    ReadReservationProof,
    ReadResultProof,
    SessionAuthority,
    reserved_request_ledger_sha256,
    reserved_request_parameters_sha256,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    AccountState,
    DurableIntent,
    LimitOrderFilters,
    MutationProtocolError,
    OrderDerivationProof,
    RequestPurpose,
    ReservedRequest,
    SymbolState,
    validate_account_state,
    validate_symbol_state,
)
from global_quant.gate1b.process_boundary import (
    ProcessBoundaryError,
    ProcessLifecycleJournal,
    ReapAttestation,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CLIENT_ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,36}")
_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,24}")

_CREDENTIAL_ENVIRONMENT_NAMES = frozenset(
    {
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_DEMO_API_KEY",
        "BINANCE_DEMO_API_SECRET",
        "BINANCE_FUTURES_TESTNET_API_KEY",
        "BINANCE_FUTURES_TESTNET_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    },
)

_FORBIDDEN_RETAINED_FIELDS = frozenset(
    {
        "api_key",
        "api_secret",
        "apikey",
        "apisecret",
        "authorization",
        "authorization_header",
        "credential_derived_material",
        "credential_hash",
        "headers",
        "pem",
        "private_key",
        "raw_exception",
        "raw_response",
        "request_headers",
        "secret",
        "signature",
        "signed_headers",
        "signed_query",
        "signed_query_string",
        "signed_url",
        "x_mbx_apikey",
    },
)

_FORBIDDEN_RAW_MARKERS = (
    b"-----begin private key-----",
    b"-----begin rsa private key-----",
    b"x-mbx-apikey",
)

_GENERATED_ARTIFACTS = frozenset(
    {
        "final-account.json",
        "final-open-algo-orders.json",
        "final-open-orders.json",
        "final-order.json",
        "final-position.json",
        "final-position-mode.json",
        "final-state.json",
        "final-symbol-config.json",
        "final-trade.json",
        "manifest.json",
        "manifest.json.sha256",
        "preflight.json",
        "process-exit.json",
        "verdict.json",
        "verdict.json.sha256",
    },
)

# The allowlist is intentionally flat and explicit.  New retained material must
# be reviewed and added rather than silently entering a recursive manifest.
ARTIFACT_ALLOWLIST = frozenset(
    {
        "authorization.json",
        "child-pre-exit.json",
        "intent.json",
        "lifecycle.jsonl",
        "lifecycle.jsonl.head",
        "preflight.json",
        "request-ledger.json",
        "request-ledger.json.head",
        "requests.jsonl",
        "requests.jsonl.head",
        *_GENERATED_ARTIFACTS,
    },
)


class FinalEvidenceError(RuntimeError):
    """Raised when final evidence cannot be safely retained or finalized."""


class EvidenceKind(Enum):
    ORDER = "ORDER"
    OPEN_REGULAR_ORDERS = "OPEN_REGULAR_ORDERS"
    OPEN_ALGO_ORDERS = "OPEN_ALGO_ORDERS"
    TRADE = "TRADE"
    ACCOUNT = "ACCOUNT"
    SYMBOL_CONFIG = "SYMBOL_CONFIG"
    POSITION_MODE = "POSITION_MODE"


class PreflightKind(Enum):
    SERVER_TIME = "SERVER_TIME"
    POSITION_MODE = "POSITION_MODE"
    SYMBOL_CONFIG = "SYMBOL_CONFIG"
    ACCOUNT = "ACCOUNT"
    OPEN_REGULAR_ORDERS = "OPEN_REGULAR_ORDERS"
    OPEN_ALGO_ORDERS = "OPEN_ALGO_ORDERS"
    EXCHANGE_INFO = "EXCHANGE_INFO"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    TRADE = "TRADE"
    BOOK_TICKER = "BOOK_TICKER"
    MARK_PRICE = "MARK_PRICE"


class BlockedFinalizationCause(Enum):
    """Typed fail-closed causes for a final read schedule that never completed."""

    CREDENTIAL_CHILD_CRASH = "FINALIZATION_BLOCKED_CREDENTIAL_CHILD_CRASH"
    FINAL_READ_SCHEDULE_INCOMPLETE = "FINALIZATION_BLOCKED_FINAL_READ_SCHEDULE_INCOMPLETE"
    FINAL_READ_IPC_FAILURE = "FINALIZATION_BLOCKED_FINAL_READ_IPC_FAILURE"
    FINAL_READ_RESULT_DURABILITY_FAILURE = (
        "FINALIZATION_BLOCKED_FINAL_READ_RESULT_DURABILITY_FAILURE"
    )
    RUNTIME_BINDING_FAILED = "FINALIZATION_BLOCKED_RUNTIME_BINDING_FAILED"


_FROZEN_FINAL_SCHEDULE = (
    EvidenceKind.ORDER,
    EvidenceKind.OPEN_REGULAR_ORDERS,
    EvidenceKind.OPEN_ALGO_ORDERS,
    EvidenceKind.TRADE,
    EvidenceKind.ACCOUNT,
    EvidenceKind.SYMBOL_CONFIG,
    EvidenceKind.POSITION_MODE,
)

_FROZEN_PREFLIGHT_SCHEDULE = (
    PreflightKind.SERVER_TIME,
    PreflightKind.POSITION_MODE,
    PreflightKind.SYMBOL_CONFIG,
    PreflightKind.ACCOUNT,
    PreflightKind.OPEN_REGULAR_ORDERS,
    PreflightKind.OPEN_ALGO_ORDERS,
    PreflightKind.EXCHANGE_INFO,
    PreflightKind.DUPLICATE_ORDER,
    PreflightKind.TRADE,
    PreflightKind.BOOK_TICKER,
    PreflightKind.MARK_PRICE,
)

_PREFLIGHT_RESPONSE_KIND = {
    PreflightKind.SERVER_TIME: ResponseKind.SERVER_TIME,
    PreflightKind.POSITION_MODE: ResponseKind.POSITION_MODE,
    PreflightKind.SYMBOL_CONFIG: ResponseKind.SYMBOL_CONFIG,
    PreflightKind.ACCOUNT: ResponseKind.ACCOUNT,
    PreflightKind.OPEN_REGULAR_ORDERS: ResponseKind.OPEN_ORDERS,
    PreflightKind.OPEN_ALGO_ORDERS: ResponseKind.OPEN_ALGO_ORDERS,
    PreflightKind.EXCHANGE_INFO: ResponseKind.EXCHANGE_INFO,
    PreflightKind.DUPLICATE_ORDER: ResponseKind.ORDER_NOT_FOUND,
    PreflightKind.TRADE: ResponseKind.USER_TRADES,
    PreflightKind.BOOK_TICKER: ResponseKind.BOOK_TICKER,
    PreflightKind.MARK_PRICE: ResponseKind.MARK_PRICE,
}

_FINAL_READ_CONTRACT: dict[
    EvidenceKind,
    tuple[str, tuple[tuple[str, str], ...] | None, ReadKind],
] = {
    EvidenceKind.ORDER: ("/fapi/v1/order", None, ReadKind.ORDER),
    EvidenceKind.OPEN_REGULAR_ORDERS: (
        "/fapi/v1/openOrders",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
        ReadKind.GENERAL,
    ),
    EvidenceKind.OPEN_ALGO_ORDERS: (
        "/fapi/v1/openAlgoOrders",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
        ReadKind.GENERAL,
    ),
    EvidenceKind.TRADE: (
        "/fapi/v1/userTrades",
        (("recvWindow", str(RECEIVE_WINDOW_MS)), ("symbol", SYMBOL)),
        ReadKind.TRADE,
    ),
    EvidenceKind.ACCOUNT: (
        "/fapi/v2/account",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
        ReadKind.ACCOUNT,
    ),
    EvidenceKind.SYMBOL_CONFIG: (
        "/fapi/v1/symbolConfig",
        (("recvWindow", str(RECEIVE_WINDOW_MS)), ("symbol", SYMBOL)),
        ReadKind.GENERAL,
    ),
    EvidenceKind.POSITION_MODE: (
        "/fapi/v1/positionSide/dual",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
        ReadKind.GENERAL,
    ),
}

_PREFLIGHT_READ_CONTRACT: dict[
    PreflightKind,
    tuple[str, tuple[tuple[str, str], ...] | None],
] = {
    PreflightKind.SERVER_TIME: ("/fapi/v1/time", ()),
    PreflightKind.POSITION_MODE: (
        "/fapi/v1/positionSide/dual",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
    ),
    PreflightKind.SYMBOL_CONFIG: (
        "/fapi/v1/symbolConfig",
        (("recvWindow", str(RECEIVE_WINDOW_MS)), ("symbol", SYMBOL)),
    ),
    PreflightKind.ACCOUNT: (
        "/fapi/v2/account",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
    ),
    PreflightKind.OPEN_REGULAR_ORDERS: (
        "/fapi/v1/openOrders",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
    ),
    PreflightKind.OPEN_ALGO_ORDERS: (
        "/fapi/v1/openAlgoOrders",
        (("recvWindow", str(RECEIVE_WINDOW_MS)),),
    ),
    PreflightKind.EXCHANGE_INFO: ("/fapi/v1/exchangeInfo", ()),
    PreflightKind.DUPLICATE_ORDER: ("/fapi/v1/order", None),
    PreflightKind.TRADE: (
        "/fapi/v1/userTrades",
        (("recvWindow", str(RECEIVE_WINDOW_MS)), ("symbol", SYMBOL)),
    ),
    PreflightKind.BOOK_TICKER: (
        "/fapi/v1/ticker/bookTicker",
        (("symbol", SYMBOL),),
    ),
    PreflightKind.MARK_PRICE: (
        "/fapi/v1/premiumIndex",
        (("symbol", SYMBOL),),
    ),
}

_FINAL_RESPONSE_KIND = {
    EvidenceKind.ORDER: ResponseKind.ORDER_OBSERVATION,
    EvidenceKind.OPEN_REGULAR_ORDERS: ResponseKind.OPEN_ORDERS,
    EvidenceKind.OPEN_ALGO_ORDERS: ResponseKind.OPEN_ALGO_ORDERS,
    EvidenceKind.TRADE: ResponseKind.USER_TRADES,
    EvidenceKind.ACCOUNT: ResponseKind.ACCOUNT,
    EvidenceKind.SYMBOL_CONFIG: ResponseKind.SYMBOL_CONFIG,
    EvidenceKind.POSITION_MODE: ResponseKind.POSITION_MODE,
}


class OrderFinalStatus(Enum):
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    FILLED = "FILLED"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


_TRANSPORT_ORDER_STATUSES = frozenset(
    status for status in OrderFinalStatus if status is not OrderFinalStatus.UNKNOWN
)


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _is_finite_decimal(value: object) -> bool:
    return type(value) is Decimal and value.is_finite()


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _expected_final_read_kind(kind: EvidenceKind) -> ReadKind:
    return _FINAL_READ_CONTRACT[kind][2]


def _validate_final_read_endpoint(kind: EvidenceKind, reserved: ReservedRequest) -> None:
    expected_path, expected_parameters, _read_kind = _FINAL_READ_CONTRACT[kind]
    actual_parameters = reserved.parameters
    if kind is EvidenceKind.ORDER:
        parameters = dict(actual_parameters)
        valid_parameters = (
            set(parameters) == {"origClientOrderId", "recvWindow", "symbol"}
            and parameters.get("recvWindow") == str(RECEIVE_WINDOW_MS)
            and parameters.get("symbol") == SYMBOL
            and type(parameters.get("origClientOrderId")) is str
            and _CLIENT_ORDER_ID_RE.fullmatch(parameters["origClientOrderId"]) is not None
        )
    else:
        valid_parameters = actual_parameters == expected_parameters
    if (
        reserved.origin != DEMO_HTTP_ORIGIN
        or reserved.method != "GET"
        or reserved.path != expected_path
        or reserved.retry_index != 0
        or not valid_parameters
    ):
        raise FinalEvidenceError("FINAL_READ_ENDPOINT_MISMATCH")


def _validate_preflight_read_endpoint(
    kind: PreflightKind,
    reservation: PreIntentReadReservation,
) -> None:
    expected_path, expected_parameters = _PREFLIGHT_READ_CONTRACT[kind]
    if kind is PreflightKind.DUPLICATE_ORDER:
        parameters = dict(reservation.parameters)
        valid_parameters = (
            set(parameters) == {"origClientOrderId", "recvWindow", "symbol"}
            and parameters.get("recvWindow") == str(RECEIVE_WINDOW_MS)
            and parameters.get("symbol") == SYMBOL
            and type(parameters.get("origClientOrderId")) is str
            and _CLIENT_ORDER_ID_RE.fullmatch(parameters["origClientOrderId"]) is not None
        )
    else:
        valid_parameters = reservation.parameters == expected_parameters
    if (
        reservation.origin != DEMO_HTTP_ORIGIN
        or reservation.method != "GET"
        or reservation.path != expected_path
        or reservation.retry_index != 0
        or not valid_parameters
    ):
        raise FinalEvidenceError("PREFLIGHT_READ_ENDPOINT_MISMATCH")


@dataclass(frozen=True, slots=True)
class PreIntentReadProvenance:
    """One exact child result anchored to the durable pre-intent journal chain."""

    kind: PreflightKind
    reservation: PreIntentReadReservation
    prepared_record: JournalRecord
    result_record: JournalRecord
    transport_result: TransportResult

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not PreflightKind
            or type(self.reservation) is not PreIntentReadReservation
            or type(self.prepared_record) is not JournalRecord
            or type(self.result_record) is not JournalRecord
            or type(self.transport_result) is not TransportResult
        ):
            raise FinalEvidenceError("PREFLIGHT_READ_AUTHORITY_REQUIRED")
        prepared = getattr(self.prepared_record.event, "reservation", None)
        result = getattr(self.result_record.event, "result", None)
        if (
            type(prepared) is not PreIntentReadReservation
            or prepared != self.reservation
            or type(result) is not PreIntentReadResult
            or result.reservation_sha256 != self.reservation.reservation_sha256
            or result.prepared_record_sequence != self.prepared_record.sequence
            or result.prepared_record_digest != self.prepared_record.digest
            or self.result_record.sequence <= self.prepared_record.sequence
            or self.transport_result.request_sha256 != self.reservation.reservation_sha256
            or self.transport_result.logical_request_sha256
            != self.reservation.logical_request_sha256
            or self.transport_result.result_sha256 != result.result_sha256
            or self.transport_result.kind is not _PREFLIGHT_RESPONSE_KIND[self.kind]
        ):
            raise FinalEvidenceError("PREFLIGHT_READ_AUTHORITY_MISMATCH")
        _validate_preflight_read_endpoint(self.kind, self.reservation)

    @property
    def observed_at_ns(self) -> int:
        result = getattr(self.result_record.event, "result", None)
        if type(result) is not PreIntentReadResult:  # pragma: no cover - constructor bound
            raise FinalEvidenceError("PREFLIGHT_READ_AUTHORITY_MISMATCH")
        return result.observed_at_ns


@dataclass(frozen=True, slots=True)
class PreflightEvidenceBundle:
    """All eleven exact pre-intent results plus their one-way intent binding."""

    authority: SessionAuthority
    authority_record: JournalRecord
    provenances: tuple[PreIntentReadProvenance, ...]
    persisted_intent: PersistedIntent
    intent_binding_record: JournalRecord

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not SessionAuthority
            or type(self.authority_record) is not JournalRecord
            or type(self.provenances) is not tuple
            or any(type(item) is not PreIntentReadProvenance for item in self.provenances)
            or type(self.persisted_intent) is not PersistedIntent
            or type(self.intent_binding_record) is not JournalRecord
        ):
            raise FinalEvidenceError("PREFLIGHT_EVIDENCE_AUTHORITY_REQUIRED")
        recorded_authority = getattr(self.authority_record.event, "authority", None)
        binding = getattr(self.intent_binding_record.event, "binding", None)
        try:
            replayed_intent = load_persisted_intent(self.persisted_intent.path)
        except DurableIntentError as exc:
            raise FinalEvidenceError("PREFLIGHT_INTENT_REPLAY_FAILED") from exc
        if (
            recorded_authority != self.authority
            or tuple(item.kind for item in self.provenances) != _FROZEN_PREFLIGHT_SCHEDULE
            or len(self.provenances) != 11
            or replayed_intent != self.persisted_intent
            or type(binding) is not IntentChainBinding
            or binding.session_authority_sha256 != self.authority.authority_sha256
            or binding.intent_sha256 != self.persisted_intent.intent.intent_sha256
            or binding.intent_file_sha256 != self.persisted_intent.file_sha256
            or self.authority_record.sequence >= self.provenances[0].prepared_record.sequence
            or self.intent_binding_record.sequence
            <= max(item.result_record.sequence for item in self.provenances)
            or any(
                item.reservation.session_authority_sha256 != self.authority.authority_sha256
                or item.reservation.generation != self.authority.generation
                for item in self.provenances
            )
        ):
            raise FinalEvidenceError("PREFLIGHT_EVIDENCE_AUTHORITY_MISMATCH")
        duplicate = self.by_kind(PreflightKind.DUPLICATE_ORDER)
        if (
            dict(duplicate.reservation.parameters).get("origClientOrderId")
            != self.authority.client_id
        ):
            raise FinalEvidenceError("PREFLIGHT_DUPLICATE_KEY_MISMATCH")

    def by_kind(self, kind: PreflightKind) -> PreIntentReadProvenance:
        matches = tuple(item for item in self.provenances if item.kind is kind)
        if len(matches) != 1:
            raise FinalEvidenceError("PREFLIGHT_READ_SCHEDULE_MISMATCH")
        return matches[0]


@dataclass(frozen=True, slots=True)
class FinalReadProvenance:
    """One final observation bound to replayable READ records and reservation."""

    kind: EvidenceKind
    reserved_request: ReservedRequest
    prepared_record: JournalRecord
    result_record: JournalRecord
    transport_result: TransportResult

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not EvidenceKind
            or type(self.reserved_request) is not ReservedRequest
            or type(self.prepared_record) is not JournalRecord
            or type(self.result_record) is not JournalRecord
            or type(self.transport_result) is not TransportResult
        ):
            raise FinalEvidenceError("FINAL_READ_AUTHORITY_REQUIRED")
        prepared = self.reservation_proof
        result = self.result_proof
        reserved = self.reserved_request
        _validate_final_read_endpoint(self.kind, reserved)
        expected_read_kind = _expected_final_read_kind(self.kind)
        if (
            reserved.purpose is not RequestPurpose.READ
            or prepared.purpose is not ReadPurpose.EVIDENCE
            or prepared.read_kind is not expected_read_kind
            or prepared.request_sha256 != reserved.request_sha256
            or prepared.logical_request_sha256 != reserved.logical_request_sha256
            or prepared.method != reserved.method
            or prepared.path != reserved.path
            or prepared.retry_index != reserved.retry_index
            or prepared.monotonic_sequence != reserved.ledger.total_http_requests
            or prepared.parameters_sha256 != reserved_request_parameters_sha256(reserved)
            or prepared.ledger_sha256 != reserved_request_ledger_sha256(reserved)
            or prepared.intent_sha256 != reserved.intent_sha256
            or result.request_sha256 != reserved.request_sha256
            or result.prepared_record_sequence != self.prepared_record.sequence
            or result.prepared_record_digest != self.prepared_record.digest
            or result.generation != prepared.generation
            or result.monotonic_sequence != prepared.monotonic_sequence
            or result.read_kind is not prepared.read_kind
            or self.result_record.sequence <= self.prepared_record.sequence
            or self.transport_result.request_sha256 != reserved.request_sha256
            or self.transport_result.logical_request_sha256 != reserved.logical_request_sha256
            or self.transport_result.result_sha256 != result.result_sha256
            or self.transport_result.kind is not _FINAL_RESPONSE_KIND[self.kind]
        ):
            raise FinalEvidenceError("FINAL_READ_AUTHORITY_MISMATCH")

    @property
    def reservation_proof(self) -> ReadReservationProof:
        proof = getattr(self.prepared_record.event, "proof", None)
        if type(proof) is not ReadReservationProof:
            raise FinalEvidenceError("READ_PREPARED_RECORD_REQUIRED")
        return proof

    @property
    def result_proof(self) -> ReadResultProof:
        proof = getattr(self.result_record.event, "proof", None)
        if type(proof) is not ReadResultProof:
            raise FinalEvidenceError("READ_RESULT_RECORD_REQUIRED")
        return proof

    @property
    def reservation_sha256(self) -> str:
        return self.reserved_request.request_sha256

    @property
    def reservation_sequence(self) -> int:
        return self.prepared_record.sequence

    @property
    def reservation_monotonic_sequence(self) -> int:
        return self.reservation_proof.monotonic_sequence

    @property
    def observed_sequence(self) -> int:
        return self.result_record.sequence

    @property
    def observed_monotonic_sequence(self) -> int:
        return self.result_proof.monotonic_sequence

    @property
    def observed_at_ns(self) -> int:
        return self.result_proof.observed_at_ns

    @property
    def result_sha256(self) -> str:
        return self.transport_result.result_sha256


@dataclass(frozen=True, slots=True)
class MutationBarrier:
    """Exact last mutation request and replayable dispatch-frontier record."""

    last_request: ReservedRequest
    last_mutation_record: JournalRecord

    def __post_init__(self) -> None:
        if (
            type(self.last_request) is not ReservedRequest
            or self.last_request.purpose is RequestPurpose.READ
            or type(self.last_mutation_record) is not JournalRecord
        ):
            raise FinalEvidenceError("INVALID_MUTATION_BARRIER")


@dataclass(frozen=True, slots=True)
class PositionEntry:
    """One retained non-zero global position; an empty tuple proves flatness."""

    symbol: str
    position_side: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.symbol) is not str
            or _SYMBOL_RE.fullmatch(self.symbol) is None
            or self.position_side not in {"BOTH", "LONG", "SHORT"}
            or not _is_finite_decimal(self.quantity)
            or self.quantity == 0
        ):
            raise FinalEvidenceError("INVALID_POSITION_ENTRY")


@dataclass(frozen=True, slots=True)
class TradeEntry:
    """Sanitized relevant trade; venue identity is retained only as SHA-256."""

    trade_id_sha256: str
    order_id_sha256: str
    quantity: Decimal
    fee: Decimal
    realized_pnl: Decimal

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.trade_id_sha256)
            or not _is_sha256(self.order_id_sha256)
            or not _is_finite_decimal(self.quantity)
            or self.quantity <= 0
            or not _is_finite_decimal(self.fee)
            or not _is_finite_decimal(self.realized_pnl)
        ):
            raise FinalEvidenceError("INVALID_TRADE_ENTRY")


@dataclass(frozen=True, slots=True)
class PreflightProjection:
    """Mechanical projection of the eleven durable pre-intent child results."""

    authority: SessionAuthority
    authority_record: JournalRecord
    provenances: tuple[PreIntentReadProvenance, ...]
    account_state: AccountState
    symbol_state: SymbolState
    filters: LimitOrderFilters
    order_derivation: OrderDerivationProof
    intent: DurableIntent
    baseline_trades: tuple[TradeEntry, ...]

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not SessionAuthority
            or type(self.authority_record) is not JournalRecord
            or type(self.provenances) is not tuple
            or tuple(item.kind for item in self.provenances) != _FROZEN_PREFLIGHT_SCHEDULE
            or type(self.account_state) is not AccountState
            or type(self.symbol_state) is not SymbolState
            or type(self.filters) is not LimitOrderFilters
            or type(self.order_derivation) is not OrderDerivationProof
            or type(self.intent) is not DurableIntent
            or self.intent.persisted is not False
            or self.intent.order_derivation != self.order_derivation
            or self.order_derivation.filters != self.filters
            or type(self.baseline_trades) is not tuple
            or any(type(item) is not TradeEntry for item in self.baseline_trades)
        ):
            raise FinalEvidenceError("INVALID_PREFLIGHT_PROJECTION")

    @property
    def artifact_payload(self) -> dict[str, object]:
        return _preflight_projection_artifact_payload(self)

    @property
    def artifact_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.artifact_payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class PersistedPreflightProjection:
    """Create-only durable preflight projection written before intent persistence."""

    path: Path
    file_sha256: str
    projection_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not _is_sha256(self.file_sha256)
            or not _is_sha256(self.projection_sha256)
        ):
            raise FinalEvidenceError("INVALID_PERSISTED_PREFLIGHT_PROJECTION")


@dataclass(frozen=True, slots=True)
class ReplayedPreflightEvidence:
    """Strict reconstruction of preflight projection and bound evidence bundle."""

    artifact: PersistedPreflightProjection
    projection: PreflightProjection
    bundle: PreflightEvidenceBundle

    def __post_init__(self) -> None:
        if (
            type(self.artifact) is not PersistedPreflightProjection
            or type(self.projection) is not PreflightProjection
            or type(self.bundle) is not PreflightEvidenceBundle
            or self.bundle.authority != self.projection.authority
            or self.bundle.provenances != self.projection.provenances
            or self.bundle.persisted_intent.intent.intent_sha256
            != self.projection.intent.intent_sha256
            or self.artifact.projection_sha256 != self.projection.artifact_sha256
        ):
            raise FinalEvidenceError("INVALID_REPLAYED_PREFLIGHT_EVIDENCE")


def _validate_evidence_header(
    provenance: FinalReadProvenance,
    expected: EvidenceKind,
) -> None:
    if type(provenance) is not FinalReadProvenance or provenance.kind is not expected:
        raise FinalEvidenceError("FINAL_EVIDENCE_KIND_MISMATCH")


@dataclass(frozen=True, slots=True)
class FinalOrderEvidence:
    provenance: FinalReadProvenance
    client_order_id: str
    status: OrderFinalStatus
    executed_quantity: Decimal

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.ORDER)
        if (
            type(self.client_order_id) is not str
            or _CLIENT_ORDER_ID_RE.fullmatch(self.client_order_id) is None
            or type(self.status) is not OrderFinalStatus
            or not _is_finite_decimal(self.executed_quantity)
            or self.executed_quantity < 0
        ):
            raise FinalEvidenceError("INVALID_FINAL_ORDER_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalOpenOrdersEvidence:
    provenance: FinalReadProvenance
    open_order_id_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.OPEN_REGULAR_ORDERS)
        if type(self.open_order_id_sha256) is not tuple or any(
            not _is_sha256(value) for value in self.open_order_id_sha256
        ):
            raise FinalEvidenceError("INVALID_FINAL_OPEN_ORDERS_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalOpenAlgoOrdersEvidence:
    provenance: FinalReadProvenance
    open_algo_order_id_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.OPEN_ALGO_ORDERS)
        if type(self.open_algo_order_id_sha256) is not tuple or any(
            not _is_sha256(value) for value in self.open_algo_order_id_sha256
        ):
            raise FinalEvidenceError("INVALID_FINAL_OPEN_ALGO_ORDERS_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalPositionEvidence:
    provenance: FinalReadProvenance
    nonzero_positions: tuple[PositionEntry, ...]

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.ACCOUNT)
        if type(self.nonzero_positions) is not tuple or any(
            type(entry) is not PositionEntry for entry in self.nonzero_positions
        ):
            raise FinalEvidenceError("INVALID_FINAL_POSITION_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalTradeEvidence:
    provenance: FinalReadProvenance
    relevant_trades: tuple[TradeEntry, ...]
    fee_delta: Decimal

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.TRADE)
        if (
            type(self.relevant_trades) is not tuple
            or any(type(entry) is not TradeEntry for entry in self.relevant_trades)
            or not _is_finite_decimal(self.fee_delta)
        ):
            raise FinalEvidenceError("INVALID_FINAL_TRADE_EVIDENCE")


@dataclass(frozen=True, slots=True)
class AccountFinalEvidence:
    provenance: FinalReadProvenance
    can_trade: bool
    single_asset_mode: bool
    wallet_delta: Decimal

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.ACCOUNT)
        if (
            type(self.can_trade) is not bool
            or type(self.single_asset_mode) is not bool
            or not _is_finite_decimal(self.wallet_delta)
        ):
            raise FinalEvidenceError("INVALID_FINAL_ACCOUNT_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalSymbolConfigEvidence:
    provenance: FinalReadProvenance
    symbol_config_matches: bool

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.SYMBOL_CONFIG)
        if type(self.symbol_config_matches) is not bool:
            raise FinalEvidenceError("INVALID_FINAL_SYMBOL_CONFIG_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalPositionModeEvidence:
    provenance: FinalReadProvenance
    position_mode_one_way: bool

    def __post_init__(self) -> None:
        _validate_evidence_header(self.provenance, EvidenceKind.POSITION_MODE)
        if type(self.position_mode_one_way) is not bool:
            raise FinalEvidenceError("INVALID_FINAL_POSITION_MODE_EVIDENCE")


@dataclass(frozen=True, slots=True)
class FinalEvidenceBundle:
    preflight: PreflightEvidenceBundle
    barrier: MutationBarrier
    order: FinalOrderEvidence
    open_orders: FinalOpenOrdersEvidence
    open_algo_orders: FinalOpenAlgoOrdersEvidence
    position: FinalPositionEvidence
    trade: FinalTradeEvidence
    account: AccountFinalEvidence
    symbol_config: FinalSymbolConfigEvidence
    position_mode: FinalPositionModeEvidence

    def __post_init__(self) -> None:
        if (
            type(self.preflight) is not PreflightEvidenceBundle
            or type(self.barrier) is not MutationBarrier
            or type(self.order) is not FinalOrderEvidence
            or type(self.open_orders) is not FinalOpenOrdersEvidence
            or type(self.open_algo_orders) is not FinalOpenAlgoOrdersEvidence
            or type(self.position) is not FinalPositionEvidence
            or type(self.trade) is not FinalTradeEvidence
            or type(self.account) is not AccountFinalEvidence
            or type(self.symbol_config) is not FinalSymbolConfigEvidence
            or type(self.position_mode) is not FinalPositionModeEvidence
        ):
            raise FinalEvidenceError("INVALID_FINAL_EVIDENCE_BUNDLE")
        if (
            self.barrier.last_request.intent_sha256
            != self.preflight.persisted_intent.intent.intent_sha256
        ):
            raise FinalEvidenceError("PREFLIGHT_FINAL_INTENT_MISMATCH")
        if self.position.provenance != self.account.provenance:
            raise FinalEvidenceError("ACCOUNT_RESPONSE_PROVENANCE_MISMATCH")
        _validate_direct_transport_projections(self)

    @property
    def provenances(self) -> tuple[FinalReadProvenance, ...]:
        return (
            self.order.provenance,
            self.open_orders.provenance,
            self.open_algo_orders.provenance,
            self.trade.provenance,
            self.account.provenance,
            self.symbol_config.provenance,
            self.position_mode.provenance,
        )


@dataclass(frozen=True, slots=True)
class FinalVerification:
    review_eligible: bool
    reason_codes: tuple[str, ...]
    mutation_states: tuple[str, ...] = ()
    gate_pass_declared: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.review_eligible) is not bool
            or type(self.reason_codes) is not tuple
            or any(type(reason) is not str or not reason for reason in self.reason_codes)
            or type(self.mutation_states) is not tuple
            or any(type(state) is not str or not state for state in self.mutation_states)
            or self.gate_pass_declared is not False
        ):
            raise FinalEvidenceError("INVALID_FINAL_VERIFICATION")


def _order_result_payload(evidence: FinalOrderEvidence) -> dict[str, object]:
    return {
        "client_order_id": evidence.client_order_id,
        "executed_quantity": _canonical_decimal(evidence.executed_quantity),
        "status": evidence.status.value,
    }


def _open_orders_result_payload(evidence: FinalOpenOrdersEvidence) -> dict[str, object]:
    return {"open_order_id_sha256": list(evidence.open_order_id_sha256)}


def _open_algo_orders_result_payload(
    evidence: FinalOpenAlgoOrdersEvidence,
) -> dict[str, object]:
    return {"open_algo_order_id_sha256": list(evidence.open_algo_order_id_sha256)}


def _position_result_payload(evidence: FinalPositionEvidence) -> dict[str, object]:
    return {
        "nonzero_positions": [
            {
                "position_side": entry.position_side,
                "quantity": _canonical_decimal(entry.quantity),
                "symbol": entry.symbol,
            }
            for entry in evidence.nonzero_positions
        ],
    }


def _trade_result_payload(evidence: FinalTradeEvidence) -> dict[str, object]:
    return {
        "fee_delta": _canonical_decimal(evidence.fee_delta),
        "relevant_trades": [
            {
                "fee": _canonical_decimal(entry.fee),
                "order_id_sha256": entry.order_id_sha256,
                "quantity": _canonical_decimal(entry.quantity),
                "realized_pnl": _canonical_decimal(entry.realized_pnl),
                "trade_id_sha256": entry.trade_id_sha256,
            }
            for entry in evidence.relevant_trades
        ],
    }


def _account_result_payload(evidence: AccountFinalEvidence) -> dict[str, object]:
    return {
        "can_trade": evidence.can_trade,
        "single_asset_mode": evidence.single_asset_mode,
        "wallet_delta": _canonical_decimal(evidence.wallet_delta),
    }


def _account_response_result_payload(bundle: FinalEvidenceBundle) -> dict[str, object]:
    return {
        "account": _account_result_payload(bundle.account),
        "position": _position_result_payload(bundle.position),
    }


def _symbol_config_result_payload(
    evidence: FinalSymbolConfigEvidence,
) -> dict[str, object]:
    return {"symbol_config_matches": evidence.symbol_config_matches}


def _position_mode_result_payload(evidence: FinalPositionModeEvidence) -> dict[str, object]:
    return {"position_mode_one_way": evidence.position_mode_one_way}


def _exact_transport_fields(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
    expected: frozenset[str],
) -> dict[str, object]:
    fields = dict(provenance.transport_result.fields)
    if frozenset(fields) != expected:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return fields


def _transport_decimal(value: object, *, positive: bool = False) -> Decimal:
    if type(value) is not str:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH") from exc
    if not parsed.is_finite() or _canonical_decimal(parsed) != value or (positive and parsed <= 0):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return parsed


def _transport_list(value: object) -> list[object]:
    if type(value) is not list:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return value


def _transport_mapping(value: object, expected: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != expected:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return value


def _project_order(
    provenance: FinalReadProvenance,
) -> tuple[str, OrderFinalStatus, Decimal, str]:
    fields = _exact_transport_fields(
        provenance,
        frozenset(
            {
                "clientOrderId",
                "executedQty",
                "orderIdSha256",
                "origQty",
                "positionSide",
                "price",
                "reduceOnly",
                "side",
                "status",
                "symbol",
                "timeInForce",
                "type",
            }
        ),
    )
    client_id = fields["clientOrderId"]
    order_id_sha256 = fields["orderIdSha256"]
    status = fields["status"]
    if (
        type(client_id) is not str
        or _CLIENT_ORDER_ID_RE.fullmatch(client_id) is None
        or not _is_sha256(order_id_sha256)
        or type(status) is not str
        or fields["symbol"] != SYMBOL
        or fields["positionSide"] != "BOTH"
        or fields["side"] != "BUY"
        or fields["type"] != "LIMIT"
        or fields["timeInForce"] != "GTX"
        or fields["reduceOnly"] is not False
    ):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    try:
        typed_status = OrderFinalStatus(status)
    except ValueError as exc:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH") from exc
    if typed_status not in _TRANSPORT_ORDER_STATUSES:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    _transport_decimal(fields["origQty"], positive=True)
    _transport_decimal(fields["price"])
    return (
        client_id,
        typed_status,
        _transport_decimal(fields["executedQty"]),
        order_id_sha256,
    )


def _project_open_orders(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> tuple[str, ...]:
    fields = _exact_transport_fields(provenance, frozenset({"count", "orders"}))
    orders = _transport_list(fields["orders"])
    if type(fields["count"]) is not int or fields["count"] != len(orders):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    identities: list[str] = []
    expected = frozenset(
        {
            "clientOrderIdSha256",
            "executedQty",
            "orderIdSha256",
            "origQty",
            "positionSide",
            "reduceOnly",
            "side",
            "status",
            "symbol",
            "type",
        }
    )
    for raw in orders:
        item = _transport_mapping(raw, expected)
        identity = item["orderIdSha256"]
        if (
            not _is_sha256(identity)
            or not _is_sha256(item["clientOrderIdSha256"])
            or item["symbol"] != SYMBOL
        ):
            raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
        _transport_decimal(item["executedQty"])
        _transport_decimal(item["origQty"], positive=True)
        identities.append(identity)
    if identities != sorted(identities) or len(set(identities)) != len(identities):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return tuple(identities)


def _project_open_algo_orders(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> tuple[str, ...]:
    fields = _exact_transport_fields(provenance, frozenset({"count", "orders"}))
    orders = _transport_list(fields["orders"])
    if type(fields["count"]) is not int or fields["count"] != len(orders):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    identities: list[str] = []
    expected = frozenset(
        {
            "algoIdSha256",
            "clientAlgoIdSha256",
            "positionSide",
            "quantity",
            "reduceOnly",
            "side",
            "status",
            "symbol",
            "type",
        }
    )
    for raw in orders:
        item = _transport_mapping(raw, expected)
        identity = item["algoIdSha256"]
        if (
            not _is_sha256(identity)
            or not _is_sha256(item["clientAlgoIdSha256"])
            or item["symbol"] != SYMBOL
        ):
            raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
        _transport_decimal(item["quantity"], positive=True)
        identities.append(identity)
    if identities != sorted(identities) or len(set(identities)) != len(identities):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return tuple(identities)


def _project_trades(provenance: FinalReadProvenance) -> tuple[TradeEntry, ...]:
    fields = _exact_transport_fields(provenance, frozenset({"count", "trades"}))
    trades = _transport_list(fields["trades"])
    if type(fields["count"]) is not int or fields["count"] != len(trades):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    projected: list[TradeEntry] = []
    expected = frozenset(
        {
            "commission",
            "orderIdSha256",
            "quantity",
            "realizedPnl",
            "tradeIdSha256",
        }
    )
    for raw in trades:
        item = _transport_mapping(raw, expected)
        projected.append(
            TradeEntry(
                trade_id_sha256=item["tradeIdSha256"],  # type: ignore[arg-type]
                order_id_sha256=item["orderIdSha256"],  # type: ignore[arg-type]
                quantity=_transport_decimal(item["quantity"], positive=True),
                fee=_transport_decimal(item["commission"]),
                realized_pnl=_transport_decimal(item["realizedPnl"]),
            )
        )
    if tuple(entry.trade_id_sha256 for entry in projected) != tuple(
        sorted(entry.trade_id_sha256 for entry in projected)
    ) or len({entry.trade_id_sha256 for entry in projected}) != len(projected):
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return tuple(projected)


def _project_preflight_trades(
    provenance: PreIntentReadProvenance,
) -> tuple[TradeEntry, ...]:
    fields = _exact_transport_fields(provenance, frozenset({"count", "trades"}))
    trades = _transport_list(fields["trades"])
    if type(fields["count"]) is not int or fields["count"] != len(trades):
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")
    projected: list[TradeEntry] = []
    expected = frozenset(
        {
            "commission",
            "orderIdSha256",
            "quantity",
            "realizedPnl",
            "tradeIdSha256",
        }
    )
    for raw in trades:
        item = _transport_mapping(raw, expected)
        projected.append(
            TradeEntry(
                trade_id_sha256=item["tradeIdSha256"],  # type: ignore[arg-type]
                order_id_sha256=item["orderIdSha256"],  # type: ignore[arg-type]
                quantity=_transport_decimal(item["quantity"], positive=True),
                fee=_transport_decimal(item["commission"]),
                realized_pnl=_transport_decimal(item["realizedPnl"]),
            )
        )
    identities = tuple(item.trade_id_sha256 for item in projected)
    if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")
    return tuple(projected)


def _timed_transport_fields(
    provenance: PreIntentReadProvenance,
    endpoint_fields: frozenset[str],
) -> tuple[dict[str, object], Decimal, int, int]:
    timing = frozenset(
        {
            "localMonotonicAfterNs",
            "localMonotonicBeforeNs",
            "localWallAfterMs",
            "localWallBeforeMs",
        }
    )
    fields = _exact_transport_fields(provenance, endpoint_fields | timing)
    values = tuple(fields[name] for name in timing)
    if any(type(value) is not int or value < 0 for value in values):
        raise FinalEvidenceError("PREFLIGHT_TIMING_FIELDS_MISMATCH")
    wall_before = fields["localWallBeforeMs"]
    wall_after = fields["localWallAfterMs"]
    monotonic_before = fields["localMonotonicBeforeNs"]
    monotonic_after = fields["localMonotonicAfterNs"]
    if wall_after < wall_before or monotonic_after < monotonic_before:
        raise FinalEvidenceError("PREFLIGHT_TIMING_FIELDS_MISMATCH")
    if monotonic_after > provenance.observed_at_ns:
        raise FinalEvidenceError("PREFLIGHT_TIMING_RECORD_MISMATCH")
    midpoint = (Decimal(wall_before) + Decimal(wall_after)) / Decimal(2)
    return fields, midpoint, monotonic_before, monotonic_after


def _validate_preflight_timed_schedule(
    provenances: tuple[PreIntentReadProvenance, ...],
) -> None:
    contracts = (
        (PreflightKind.SERVER_TIME, frozenset({"serverTime"})),
        (
            PreflightKind.BOOK_TICKER,
            frozenset(
                {
                    "askPrice",
                    "askQty",
                    "bidPrice",
                    "bidQty",
                    "lastUpdateId",
                    "symbol",
                    "time",
                }
            ),
        ),
        (PreflightKind.MARK_PRICE, frozenset({"markPrice", "symbol", "time"})),
    )
    brackets = tuple(
        _timed_transport_fields(
            _preflight_provenance_by_kind(provenances, kind),
            endpoint_fields,
        )[2:]
        for kind, endpoint_fields in contracts
    )
    if any(
        earlier_after > later_before for (_, earlier_after), (later_before, _) in pairwise(brackets)
    ):
        raise FinalEvidenceError("PREFLIGHT_TIMED_SCHEDULE_CONTRADICTION")


def _validate_preflight_evidence(
    preflight: PreflightEvidenceBundle,
    execution_journal: ExecutionJournal,
    records: tuple[JournalRecord, ...],
) -> None:
    try:
        replayed_intent = load_persisted_intent(preflight.persisted_intent.path)
    except (OSError, DurableIntentError) as exc:
        raise FinalEvidenceError("PREFLIGHT_INTENT_REPLAY_FAILED") from exc
    if replayed_intent != preflight.persisted_intent:
        raise FinalEvidenceError("PREFLIGHT_INTENT_REPLAY_FAILED")
    _assert_record_replayed(preflight.authority_record, records)
    _assert_record_replayed(preflight.intent_binding_record, records)
    intent = preflight.persisted_intent.intent
    projection = project_preflight_evidence(
        authority=preflight.authority,
        authority_record=preflight.authority_record,
        provenances=preflight.provenances,
        execution_journal=execution_journal,
        protocol_commit=intent.protocol_commit,
        protocol_tag_object=intent.protocol_tag_object,
        protocol_sha256=intent.protocol_sha256,
    )
    if (
        projection.intent.intent_sha256 != intent.intent_sha256
        or projection.intent.order_derivation != intent.order_derivation
        or projection.intent.client_order_id != intent.client_order_id
    ):
        raise FinalEvidenceError("PREFLIGHT_INTENT_PROJECTION_MISMATCH")


def _project_account_positions(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> tuple[PositionEntry, ...]:
    fields = _exact_transport_fields(
        provenance,
        frozenset({"balances", "canTrade", "multiAssetsMargin", "nonzeroPositions"}),
    )
    positions = _transport_list(fields["nonzeroPositions"])
    projected: list[PositionEntry] = []
    for raw in positions:
        item = _transport_mapping(
            raw,
            frozenset({"positionAmt", "positionSide", "symbol"}),
        )
        projected.append(
            PositionEntry(
                symbol=item["symbol"],  # type: ignore[arg-type]
                position_side=item["positionSide"],  # type: ignore[arg-type]
                quantity=_transport_decimal(item["positionAmt"]),
            )
        )
    return tuple(projected)


def _project_account_balances(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> dict[str, tuple[Decimal, Decimal]]:
    fields = dict(provenance.transport_result.fields)
    if frozenset(fields) != frozenset(
        {"balances", "canTrade", "multiAssetsMargin", "nonzeroPositions"}
    ):
        raise FinalEvidenceError("ACCOUNT_TRANSPORT_FIELDS_MISMATCH")
    balances = _transport_list(fields["balances"])
    projected: dict[str, tuple[Decimal, Decimal]] = {}
    ordered_assets: list[str] = []
    for raw in balances:
        item = _transport_mapping(
            raw,
            frozenset({"asset", "availableBalance", "walletBalance"}),
        )
        asset = item["asset"]
        if type(asset) is not str or _SYMBOL_RE.fullmatch(asset) is None or asset in projected:
            raise FinalEvidenceError("ACCOUNT_TRANSPORT_FIELDS_MISMATCH")
        ordered_assets.append(asset)
        projected[asset] = (
            _transport_decimal(item["availableBalance"]),
            _transport_decimal(item["walletBalance"]),
        )
    if ordered_assets != sorted(ordered_assets) or "USDT" not in projected:
        raise FinalEvidenceError("ACCOUNT_TRANSPORT_FIELDS_MISMATCH")
    return projected


def _project_account_flags(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> tuple[bool, bool]:
    fields = _exact_transport_fields(
        provenance,
        frozenset({"balances", "canTrade", "multiAssetsMargin", "nonzeroPositions"}),
    )
    if type(fields["canTrade"]) is not bool or type(fields["multiAssetsMargin"]) is not bool:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return fields["canTrade"], not fields["multiAssetsMargin"]


def _project_symbol_config(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> bool:
    fields = _exact_transport_fields(
        provenance,
        frozenset({"isAutoAddMargin", "leverage", "marginType", "symbol"}),
    )
    if type(fields["isAutoAddMargin"]) is not bool or type(fields["leverage"]) is not int:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return (
        fields["symbol"] == SYMBOL
        and fields["marginType"] == "ISOLATED"
        and fields["leverage"] == 1
        and fields["isAutoAddMargin"] is False
    )


def _project_position_mode(
    provenance: FinalReadProvenance | PreIntentReadProvenance,
) -> bool:
    fields = _exact_transport_fields(provenance, frozenset({"dualSidePosition"}))
    if type(fields["dualSidePosition"]) is not bool:
        raise FinalEvidenceError("FINAL_TRANSPORT_FIELDS_MISMATCH")
    return not fields["dualSidePosition"]


def _preflight_provenance_by_kind(
    provenances: tuple[PreIntentReadProvenance, ...],
    kind: PreflightKind,
) -> PreIntentReadProvenance:
    matches = tuple(item for item in provenances if item.kind is kind)
    if len(matches) != 1:
        raise FinalEvidenceError("PREFLIGHT_READ_SCHEDULE_MISMATCH")
    return matches[0]


def _validate_preflight_projection_replay(
    *,
    authority: SessionAuthority,
    authority_record: JournalRecord,
    provenances: tuple[PreIntentReadProvenance, ...],
    records: tuple[JournalRecord, ...],
) -> None:
    if (
        type(authority) is not SessionAuthority
        or type(authority_record) is not JournalRecord
        or type(provenances) is not tuple
        or tuple(item.kind for item in provenances) != _FROZEN_PREFLIGHT_SCHEDULE
        or len(provenances) != 11
        or any(type(item) is not PreIntentReadProvenance for item in provenances)
        or getattr(authority_record.event, "authority", None) != authority
    ):
        raise FinalEvidenceError("PREFLIGHT_EVIDENCE_AUTHORITY_MISMATCH")
    _assert_record_replayed(authority_record, records)
    for provenance in provenances:
        _assert_record_replayed(provenance.prepared_record, records)
        _assert_record_replayed(provenance.result_record, records)

    durable_prepared = tuple(
        reservation.reservation_sha256
        for record in records
        if type(reservation := getattr(record.event, "reservation", None))
        is PreIntentReadReservation
        and reservation.session_authority_sha256 == authority.authority_sha256
    )
    durable_prepared_set = frozenset(durable_prepared)
    durable_results = tuple(
        result.reservation_sha256
        for record in records
        if type(result := getattr(record.event, "result", None)) is PreIntentReadResult
        and result.reservation_sha256 in durable_prepared_set
    )
    supplied = tuple(item.reservation.reservation_sha256 for item in provenances)
    if supplied != durable_prepared or supplied != durable_results:
        raise FinalEvidenceError("PREFLIGHT_EVIDENCE_SET_NOT_EXACT")
    if tuple(item.reservation.ledger.total_http_requests for item in provenances) != tuple(
        range(1, 12)
    ):
        raise FinalEvidenceError("PREFLIGHT_READ_MONOTONIC_CHAIN_BROKEN")
    if any(
        item.reservation.session_authority_sha256 != authority.authority_sha256
        or item.reservation.generation != authority.generation
        or item.reservation.retry_index != 0
        for item in provenances
    ):
        raise FinalEvidenceError("PREFLIGHT_EVIDENCE_AUTHORITY_MISMATCH")
    if any(
        later.reservation.elapsed_seconds < earlier.reservation.elapsed_seconds
        or later.prepared_record.sequence <= earlier.result_record.sequence
        or later.observed_at_ns <= earlier.observed_at_ns
        for earlier, later in pairwise(provenances)
    ):
        raise FinalEvidenceError("PREFLIGHT_CLOCK_SEQUENCE_CONTRADICTION")


def _project_exchange_info(
    provenance: PreIntentReadProvenance,
) -> tuple[SymbolState, LimitOrderFilters]:
    fields = _exact_transport_fields(
        provenance,
        frozenset(
            {
                "contractType",
                "filterTypeCounts",
                "limitLotSize",
                "marginAsset",
                "marketLotSize",
                "minNotional",
                "orderTypes",
                "percentPrice",
                "priceFilter",
                "quoteAsset",
                "status",
                "symbol",
                "timeInForce",
                "uninterpretedFilterTypes",
            }
        ),
    )
    price = _transport_mapping(
        fields["priceFilter"],
        frozenset({"maxPrice", "minPrice", "tickSize"}),
    )
    lot = _transport_mapping(
        fields["limitLotSize"],
        frozenset({"maxQuantity", "minQuantity", "stepSize"}),
    )
    market_lot = _transport_mapping(
        fields["marketLotSize"],
        frozenset({"maxQuantity", "minQuantity", "stepSize"}),
    )
    percent = _transport_mapping(
        fields["percentPrice"],
        frozenset({"multiplierDown", "multiplierUp"}),
    )
    counts = fields["filterTypeCounts"]
    order_types = fields["orderTypes"]
    time_in_force = fields["timeInForce"]
    uninterpreted = fields["uninterpretedFilterTypes"]
    if (
        type(counts) is not dict
        or any(type(name) is not str or type(count) is not int for name, count in counts.items())
        or type(order_types) is not list
        or any(type(value) is not str for value in order_types)
        or order_types != sorted(set(order_types))
        or type(time_in_force) is not list
        or any(type(value) is not str for value in time_in_force)
        or time_in_force != sorted(set(time_in_force))
        or type(uninterpreted) is not list
        or any(type(value) is not str for value in uninterpreted)
        or uninterpreted != sorted(set(uninterpreted))
    ):
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")
    for name in ("minQuantity", "maxQuantity", "stepSize"):
        _transport_decimal(market_lot[name], positive=True)
    symbol_state = SymbolState(
        symbol=fields["symbol"],  # type: ignore[arg-type]
        status=fields["status"],  # type: ignore[arg-type]
        contract_type=fields["contractType"],  # type: ignore[arg-type]
        quote_asset=fields["quoteAsset"],  # type: ignore[arg-type]
        margin_asset=fields["marginAsset"],  # type: ignore[arg-type]
        order_types=frozenset(order_types),
        time_in_force=frozenset(time_in_force),
        filter_type_counts=tuple(sorted(counts.items())),
        uninterpreted_applicable_filter_types=tuple(uninterpreted),
    )
    filters = LimitOrderFilters(
        min_price=_transport_decimal(price["minPrice"], positive=True),
        max_price=_transport_decimal(price["maxPrice"], positive=True),
        tick_size=_transport_decimal(price["tickSize"], positive=True),
        min_quantity=_transport_decimal(lot["minQuantity"], positive=True),
        max_quantity=_transport_decimal(lot["maxQuantity"], positive=True),
        step_size=_transport_decimal(lot["stepSize"], positive=True),
        min_notional=_transport_decimal(fields["minNotional"], positive=True),
        percent_price_multiplier_down=_transport_decimal(
            percent["multiplierDown"],
            positive=True,
        ),
        percent_price_multiplier_up=_transport_decimal(
            percent["multiplierUp"],
            positive=True,
        ),
        price_filter_count=counts.get("PRICE_FILTER", 0),
        lot_size_filter_count=counts.get("LOT_SIZE", 0),
        min_notional_filter_count=counts.get("MIN_NOTIONAL", 0),
        percent_price_filter_count=counts.get("PERCENT_PRICE", 0),
        uninterpreted_applicable_filter_types=tuple(uninterpreted),
    )
    return symbol_state, filters


def _project_preflight_order_derivation(
    provenances: tuple[PreIntentReadProvenance, ...],
    filters: LimitOrderFilters,
) -> OrderDerivationProof:
    book_provenance = _preflight_provenance_by_kind(
        provenances,
        PreflightKind.BOOK_TICKER,
    )
    mark_provenance = _preflight_provenance_by_kind(
        provenances,
        PreflightKind.MARK_PRICE,
    )
    book, book_wall_midpoint, book_before, book_after = _timed_transport_fields(
        book_provenance,
        frozenset({"askPrice", "askQty", "bidPrice", "bidQty", "lastUpdateId", "symbol", "time"}),
    )
    mark, mark_wall_midpoint, mark_before, mark_after = _timed_transport_fields(
        mark_provenance,
        frozenset({"markPrice", "symbol", "time"}),
    )
    if (
        book["symbol"] != SYMBOL
        or mark["symbol"] != SYMBOL
        or type(book["time"]) is not int
        or type(mark["time"]) is not int
        or type(book["lastUpdateId"]) is not int
        or book["lastUpdateId"] < 0
    ):
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")
    _transport_decimal(book["bidQty"], positive=True)
    _transport_decimal(book["askQty"], positive=True)
    exchange = _preflight_provenance_by_kind(provenances, PreflightKind.EXCHANGE_INFO)
    book_monotonic_midpoint = (Decimal(book_before) + Decimal(book_after)) / Decimal(2)
    mark_monotonic_midpoint = (Decimal(mark_before) + Decimal(mark_after)) / Decimal(2)
    derivation_monotonic = max(book_monotonic_midpoint, mark_monotonic_midpoint)
    return OrderDerivationProof(
        best_bid=_transport_decimal(book["bidPrice"], positive=True),
        best_ask=_transport_decimal(book["askPrice"], positive=True),
        mark_price=_transport_decimal(mark["markPrice"], positive=True),
        filters=filters,
        filter_snapshot_sha256=exchange.transport_result.result_sha256,
        filter_contract_sha256=filters.canonical_sha256,
        book_age_ms=(
            book_wall_midpoint
            - Decimal(book["time"])
            + (derivation_monotonic - book_monotonic_midpoint) / Decimal(1_000_000)
        ),
        mark_age_ms=(
            mark_wall_midpoint
            - Decimal(mark["time"])
            + (derivation_monotonic - mark_monotonic_midpoint) / Decimal(1_000_000)
        ),
        observed_elapsed_seconds=max(
            book_provenance.reservation.elapsed_seconds,
            mark_provenance.reservation.elapsed_seconds,
        ),
    )


def project_preflight_evidence(
    *,
    authority: SessionAuthority,
    authority_record: JournalRecord,
    provenances: tuple[PreIntentReadProvenance, ...],
    execution_journal: ExecutionJournal,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> PreflightProjection:
    """Project the sole intent candidate from all exact durable pre-intent reads."""

    records = _replay_execution_journal(execution_journal)
    _validate_preflight_projection_replay(
        authority=authority,
        authority_record=authority_record,
        provenances=provenances,
        records=records,
    )
    _validate_preflight_timed_schedule(provenances)

    def by_kind(kind: PreflightKind) -> PreIntentReadProvenance:
        return _preflight_provenance_by_kind(provenances, kind)

    duplicate = _exact_transport_fields(
        by_kind(PreflightKind.DUPLICATE_ORDER),
        frozenset({"clientOrderId", "outcome", "venueCode"}),
    )
    if (
        duplicate["clientOrderId"] != authority.client_id
        or duplicate["outcome"] != "CONFIRMED_NOT_FOUND"
        or duplicate["venueCode"] != -2013
    ):
        raise FinalEvidenceError("PREFLIGHT_DUPLICATE_ID_NOT_ABSENT")
    symbol_config = _exact_transport_fields(
        by_kind(PreflightKind.SYMBOL_CONFIG),
        frozenset({"isAutoAddMargin", "leverage", "marginType", "symbol"}),
    )
    position_mode = _exact_transport_fields(
        by_kind(PreflightKind.POSITION_MODE),
        frozenset({"dualSidePosition"}),
    )
    if (
        type(symbol_config["isAutoAddMargin"]) is not bool
        or type(symbol_config["leverage"]) is not int
        or type(position_mode["dualSidePosition"]) is not bool
        or symbol_config["symbol"] != SYMBOL
    ):
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")
    server, server_wall_midpoint, _server_before, _server_after = _timed_transport_fields(
        by_kind(PreflightKind.SERVER_TIME),
        frozenset({"serverTime"}),
    )
    if type(server["serverTime"]) is not int or server["serverTime"] <= 0:
        raise FinalEvidenceError("PREFLIGHT_TRANSPORT_FIELDS_MISMATCH")

    try:
        symbol_state, filters = _project_exchange_info(by_kind(PreflightKind.EXCHANGE_INFO))
        validate_symbol_state(symbol_state)
        order_derivation = _project_preflight_order_derivation(provenances, filters)
        intent = DurableIntent(
            authorization_id=authority.authorization_id,
            protocol_commit=protocol_commit,
            protocol_tag_object=protocol_tag_object,
            protocol_sha256=protocol_sha256,
            runtime_commit=authority.runtime_commit,
            session_nonce=authority.session_nonce,
            order_derivation=order_derivation,
            persisted=False,
        )
        balances = _project_account_balances(by_kind(PreflightKind.ACCOUNT))
        can_trade, _single_asset = _project_account_flags(by_kind(PreflightKind.ACCOUNT))
        positions = _project_account_positions(by_kind(PreflightKind.ACCOUNT))
        account_state = AccountState(
            can_trade=can_trade,
            dual_side_position=position_mode["dualSidePosition"],  # type: ignore[arg-type]
            multi_assets_margin=not _single_asset,
            margin_type=symbol_config["marginType"],  # type: ignore[arg-type]
            leverage=symbol_config["leverage"],  # type: ignore[arg-type]
            auto_add_margin=symbol_config["isAutoAddMargin"],  # type: ignore[arg-type]
            server_time_skew_ms=Decimal(server["serverTime"]) - server_wall_midpoint,
            wallet_balance=balances["USDT"][1],
            available_balance=balances["USDT"][0],
            nonzero_positions=tuple((item.symbol, item.quantity) for item in positions),
            open_regular_order_ids=_project_open_orders(by_kind(PreflightKind.OPEN_REGULAR_ORDERS)),
            open_algo_order_ids=_project_open_algo_orders(by_kind(PreflightKind.OPEN_ALGO_ORDERS)),
        )
        validate_account_state(account_state, required_notional=intent.probe_order.notional)
    except MutationProtocolError as exc:
        raise FinalEvidenceError(f"PREFLIGHT_PROTOCOL_PROJECTION_FAILED:{exc}") from exc
    baseline_trades = _project_preflight_trades(by_kind(PreflightKind.TRADE))
    return PreflightProjection(
        authority=authority,
        authority_record=authority_record,
        provenances=provenances,
        account_state=account_state,
        symbol_state=symbol_state,
        filters=filters,
        order_derivation=order_derivation,
        intent=intent,
        baseline_trades=baseline_trades,
    )


def persist_preflight_projection(
    path: Path,
    projection: PreflightProjection,
) -> PersistedPreflightProjection:
    """Create and fsync the canonical sanitized projection before intent.json."""

    if not isinstance(path, Path) or type(projection) is not PreflightProjection:
        raise FinalEvidenceError("PREFLIGHT_PROJECTION_PERSIST_INPUT_INVALID")
    payload = _pretty_json_bytes(projection.artifact_payload)
    _atomic_write_owner_only(path, payload)
    replayed_payload, replayed_bytes = _read_preflight_projection_artifact(path)
    if replayed_payload != projection.artifact_payload or replayed_bytes != payload:
        raise FinalEvidenceError("PREFLIGHT_PROJECTION_DURABILITY_MISMATCH")
    return PersistedPreflightProjection(
        path=path,
        file_sha256=hashlib.sha256(replayed_bytes).hexdigest(),
        projection_sha256=projection.artifact_sha256,
    )


def load_preflight_evidence(
    path: Path,
    *,
    execution_journal: ExecutionJournal,
    persisted_intent: PersistedIntent,
) -> ReplayedPreflightEvidence:
    """Replay preflight.json only through its exact live journal anchors."""

    if (
        not isinstance(path, Path)
        or type(execution_journal) is not ExecutionJournal
        or type(persisted_intent) is not PersistedIntent
        or load_persisted_intent(persisted_intent.path) != persisted_intent
    ):
        raise FinalEvidenceError("PREFLIGHT_REPLAY_INPUT_INVALID")
    payload, artifact_bytes = _read_preflight_projection_artifact(path)
    records = _replay_execution_journal(execution_journal)
    intent = persisted_intent.intent
    authority_records = tuple(
        record
        for record in records
        if type(authority := getattr(record.event, "authority", None)) is SessionAuthority
        and authority.authorization_id == intent.authorization_id
        and authority.runtime_commit == intent.runtime_commit
        and authority.session_nonce == intent.session_nonce
    )
    if len(authority_records) != 1:
        raise FinalEvidenceError("PREFLIGHT_AUTHORITY_REPLAY_MISMATCH")
    authority_record = authority_records[0]
    authority = authority_record.event.authority
    binding_records = tuple(
        record
        for record in records
        if type(binding := getattr(record.event, "binding", None)) is IntentChainBinding
        and binding.session_authority_sha256 == authority.authority_sha256
        and binding.intent_sha256 == intent.intent_sha256
        and binding.intent_file_sha256 == persisted_intent.file_sha256
    )
    if len(binding_records) != 1:
        raise FinalEvidenceError("PREFLIGHT_BINDING_REPLAY_MISMATCH")
    reads = payload.get("reads") if type(payload) is dict else None
    if type(reads) is not list or len(reads) != len(_FROZEN_PREFLIGHT_SCHEDULE):
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_READ_SCHEDULE_MISMATCH")
    provenances: list[PreIntentReadProvenance] = []
    try:
        for expected_kind, raw in zip(_FROZEN_PREFLIGHT_SCHEDULE, reads, strict=True):
            if type(raw) is not dict or raw.get("kind") != expected_kind.value:
                raise FinalEvidenceError("PREFLIGHT_ARTIFACT_READ_SCHEDULE_MISMATCH")
            prepared_anchor = raw.get("prepared_record")
            result_anchor = raw.get("result_record")
            transport_payload = raw.get("transport_result")
            if (
                type(prepared_anchor) is not dict
                or type(result_anchor) is not dict
                or type(transport_payload) is not dict
                or set(transport_payload)
                != {
                    "fields",
                    "kind",
                    "logical_request_sha256",
                    "request_sha256",
                    "result_sha256",
                }
            ):
                raise FinalEvidenceError("PREFLIGHT_ARTIFACT_STRUCTURE_INVALID")
            prepared_record = _record_from_artifact_anchor(records, prepared_anchor)
            result_record = _record_from_artifact_anchor(records, result_anchor)
            reservation = getattr(prepared_record.event, "reservation", None)
            durable_result = getattr(result_record.event, "result", None)
            fields = transport_payload["fields"]
            if (
                type(reservation) is not PreIntentReadReservation
                or type(durable_result) is not PreIntentReadResult
                or type(fields) is not dict
            ):
                raise FinalEvidenceError("PREFLIGHT_ARTIFACT_JOURNAL_MISMATCH")
            transport_result = TransportResult.build(
                request_sha256=transport_payload["request_sha256"],
                logical_request_sha256=transport_payload["logical_request_sha256"],
                kind=ResponseKind(transport_payload["kind"]),
                fields=tuple(fields.items()),
            )
            if transport_result.result_sha256 != transport_payload["result_sha256"]:
                raise FinalEvidenceError("PREFLIGHT_ARTIFACT_TRANSPORT_DIGEST_MISMATCH")
            provenances.append(
                PreIntentReadProvenance(
                    kind=expected_kind,
                    reservation=reservation,
                    prepared_record=prepared_record,
                    result_record=result_record,
                    transport_result=transport_result,
                )
            )
    except FinalEvidenceError:
        raise
    except BaseException:
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_STRUCTURE_INVALID") from None
    projection = project_preflight_evidence(
        authority=authority,
        authority_record=authority_record,
        provenances=tuple(provenances),
        execution_journal=execution_journal,
        protocol_commit=intent.protocol_commit,
        protocol_tag_object=intent.protocol_tag_object,
        protocol_sha256=intent.protocol_sha256,
    )
    if (
        projection.intent.intent_sha256 != intent.intent_sha256
        or payload != projection.artifact_payload
        or artifact_bytes != _pretty_json_bytes(projection.artifact_payload)
    ):
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_RECONSTRUCTION_MISMATCH")
    bundle = PreflightEvidenceBundle(
        authority=authority,
        authority_record=authority_record,
        provenances=tuple(provenances),
        persisted_intent=persisted_intent,
        intent_binding_record=binding_records[0],
    )
    artifact = PersistedPreflightProjection(
        path=path,
        file_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        projection_sha256=projection.artifact_sha256,
    )
    return ReplayedPreflightEvidence(
        artifact=artifact,
        projection=projection,
        bundle=bundle,
    )


def project_final_evidence(
    *,
    preflight: PreflightEvidenceBundle,
    barrier: MutationBarrier,
    provenances: tuple[FinalReadProvenance, ...],
) -> FinalEvidenceBundle:
    """Project the sole final bundle from the exact seven durable reads."""

    if (
        type(preflight) is not PreflightEvidenceBundle
        or type(barrier) is not MutationBarrier
        or type(provenances) is not tuple
        or tuple(item.kind for item in provenances if type(item) is FinalReadProvenance)
        != _FROZEN_FINAL_SCHEDULE
        or len(provenances) != len(_FROZEN_FINAL_SCHEDULE)
    ):
        raise FinalEvidenceError("FINAL_READ_SCHEDULE_MISMATCH")

    by_kind = {item.kind: item for item in provenances}
    if len(by_kind) != len(_FROZEN_FINAL_SCHEDULE):
        raise FinalEvidenceError("FINAL_READ_SCHEDULE_MISMATCH")
    order = by_kind[EvidenceKind.ORDER]
    open_orders = by_kind[EvidenceKind.OPEN_REGULAR_ORDERS]
    open_algo_orders = by_kind[EvidenceKind.OPEN_ALGO_ORDERS]
    trade = by_kind[EvidenceKind.TRADE]
    account = by_kind[EvidenceKind.ACCOUNT]
    symbol_config = by_kind[EvidenceKind.SYMBOL_CONFIG]
    position_mode = by_kind[EvidenceKind.POSITION_MODE]

    client_id, status, executed_quantity, order_id_sha256 = _project_order(order)
    baseline_trades = _project_preflight_trades(preflight.by_kind(PreflightKind.TRADE))
    all_trades = _project_trades(trade)
    baseline_by_id = {item.trade_id_sha256: item for item in baseline_trades}
    final_by_id = {item.trade_id_sha256: item for item in all_trades}
    if any(final_by_id.get(identity) != item for identity, item in baseline_by_id.items()):
        raise FinalEvidenceError("FINAL_TRADE_BASELINE_MISMATCH")
    relevant_trades = tuple(
        item
        for item in all_trades
        if item.trade_id_sha256 not in baseline_by_id and item.order_id_sha256 == order_id_sha256
    )
    initial_balances = _project_account_balances(preflight.by_kind(PreflightKind.ACCOUNT))
    final_balances = _project_account_balances(account)
    if set(initial_balances) != set(final_balances):
        raise FinalEvidenceError("FINAL_ACCOUNT_BASELINE_MISMATCH")
    can_trade, single_asset_mode = _project_account_flags(account)

    return FinalEvidenceBundle(
        preflight=preflight,
        barrier=barrier,
        order=FinalOrderEvidence(
            provenance=order,
            client_order_id=client_id,
            status=status,
            executed_quantity=executed_quantity,
        ),
        open_orders=FinalOpenOrdersEvidence(
            provenance=open_orders,
            open_order_id_sha256=_project_open_orders(open_orders),
        ),
        open_algo_orders=FinalOpenAlgoOrdersEvidence(
            provenance=open_algo_orders,
            open_algo_order_id_sha256=_project_open_algo_orders(open_algo_orders),
        ),
        position=FinalPositionEvidence(
            provenance=account,
            nonzero_positions=_project_account_positions(account),
        ),
        trade=FinalTradeEvidence(
            provenance=trade,
            relevant_trades=relevant_trades,
            fee_delta=sum((item.fee for item in relevant_trades), start=Decimal(0)),
        ),
        account=AccountFinalEvidence(
            provenance=account,
            can_trade=can_trade,
            single_asset_mode=single_asset_mode,
            wallet_delta=final_balances["USDT"][1] - initial_balances["USDT"][1],
        ),
        symbol_config=FinalSymbolConfigEvidence(
            provenance=symbol_config,
            symbol_config_matches=_project_symbol_config(symbol_config),
        ),
        position_mode=FinalPositionModeEvidence(
            provenance=position_mode,
            position_mode_one_way=_project_position_mode(position_mode),
        ),
    )


def _validate_direct_transport_projections(bundle: FinalEvidenceBundle) -> None:
    client_id, status, executed_quantity, order_id_sha256 = _project_order(bundle.order.provenance)
    baseline_trades = _project_preflight_trades(bundle.preflight.by_kind(PreflightKind.TRADE))
    all_trades = _project_trades(bundle.trade.provenance)
    baseline_by_id = {trade.trade_id_sha256: trade for trade in baseline_trades}
    final_by_id = {trade.trade_id_sha256: trade for trade in all_trades}
    if any(final_by_id.get(identity) != trade for identity, trade in baseline_by_id.items()):
        raise FinalEvidenceError("FINAL_TRADE_BASELINE_MISMATCH")
    new_trades = tuple(trade for trade in all_trades if trade.trade_id_sha256 not in baseline_by_id)
    relevant_trades = tuple(
        trade for trade in new_trades if trade.order_id_sha256 == order_id_sha256
    )
    fee_delta = sum((trade.fee for trade in relevant_trades), start=Decimal(0))
    can_trade, single_asset_mode = _project_account_flags(bundle.account.provenance)
    initial_balances = _project_account_balances(bundle.preflight.by_kind(PreflightKind.ACCOUNT))
    final_balances = _project_account_balances(bundle.account.provenance)
    if set(initial_balances) != set(final_balances):
        raise FinalEvidenceError("FINAL_ACCOUNT_BASELINE_MISMATCH")
    wallet_delta = final_balances["USDT"][1] - initial_balances["USDT"][1]
    if (
        (bundle.order.client_order_id, bundle.order.status, bundle.order.executed_quantity)
        != (client_id, status, executed_quantity)
        or bundle.open_orders.open_order_id_sha256
        != _project_open_orders(bundle.open_orders.provenance)
        or bundle.open_algo_orders.open_algo_order_id_sha256
        != _project_open_algo_orders(bundle.open_algo_orders.provenance)
        or bundle.position.nonzero_positions
        != _project_account_positions(bundle.position.provenance)
        or bundle.trade.relevant_trades != relevant_trades
        or bundle.trade.fee_delta != fee_delta
        or (bundle.account.can_trade, bundle.account.single_asset_mode)
        != (can_trade, single_asset_mode)
        or bundle.account.wallet_delta != wallet_delta
        or bundle.symbol_config.symbol_config_matches
        is not _project_symbol_config(bundle.symbol_config.provenance)
        or bundle.position_mode.position_mode_one_way
        is not _project_position_mode(bundle.position_mode.provenance)
    ):
        raise FinalEvidenceError("FINAL_RESULT_PROJECTION_MISMATCH")


def _replay_execution_journal(journal: ExecutionJournal) -> tuple[JournalRecord, ...]:
    if type(journal) is not ExecutionJournal:
        raise FinalEvidenceError("EXECUTION_JOURNAL_REQUIRED")
    try:
        return journal.records()
    except ExecutionJournalError as exc:
        raise FinalEvidenceError("EXECUTION_JOURNAL_REPLAY_FAILED") from exc


def _assert_record_replayed(
    record: JournalRecord,
    records: tuple[JournalRecord, ...],
) -> None:
    if (
        type(record) is not JournalRecord
        or record.sequence <= 0
        or record.sequence > len(records)
        or records[record.sequence - 1] != record
    ):
        raise FinalEvidenceError("JOURNAL_RECORD_NOT_REPLAYED")


def _proof_from_record(record: JournalRecord, proof_type: type[Any]) -> Any:
    proof = getattr(record.event, "proof", None)
    if type(proof) is not proof_type:
        raise FinalEvidenceError("JOURNAL_PROOF_TYPE_MISMATCH")
    return proof


def _attempt_for_request(
    records: tuple[JournalRecord, ...],
    request_sha256: str,
) -> MutationAttempt:
    matches = tuple(
        attempt
        for record in records
        if type(attempt := getattr(record.event, "attempt", None)) is MutationAttempt
        and attempt.reservation_sha256 == request_sha256
    )
    if len(matches) != 1:
        raise FinalEvidenceError("MUTATION_ATTEMPT_LINEAGE_MISMATCH")
    return matches[0]


def _record_relates_to_attempt(
    record: JournalRecord,
    attempt: MutationAttempt,
) -> bool:
    event = record.event
    if getattr(event, "attempt", None) == attempt:
        return True
    if getattr(event, "attempt_id", None) == attempt.attempt_id:
        return True
    proof = getattr(event, "proof", None)
    return (
        type(proof) is MutationReservationProof
        and proof.request_sha256 == attempt.reservation_sha256
    )


def _validate_mutation_barrier(
    bundle: FinalEvidenceBundle,
    journal: ExecutionJournal,
    records: tuple[JournalRecord, ...],
    reasons: set[str],
) -> tuple[str, ...]:
    barrier = bundle.barrier
    _assert_record_replayed(barrier.last_mutation_record, records)
    reservation_records = tuple(
        record
        for record in records
        if type(getattr(record.event, "proof", None)) is MutationReservationProof
        and record.event.proof.request_sha256 == barrier.last_request.request_sha256
    )
    if len(reservation_records) != 1:
        raise FinalEvidenceError("MUTATION_RESERVATION_NOT_REPLAYED")
    proof = _proof_from_record(reservation_records[0], MutationReservationProof)
    reserved = barrier.last_request
    if (
        proof.request_sha256 != reserved.request_sha256
        or proof.logical_request_sha256 != reserved.logical_request_sha256
        or proof.method != reserved.method
        or proof.path != reserved.path
        or proof.retry_index != reserved.retry_index
        or proof.monotonic_sequence != reserved.ledger.total_http_requests
        or proof.parameters_sha256 != reserved_request_parameters_sha256(reserved)
        or proof.ledger_sha256 != reserved_request_ledger_sha256(reserved)
        or proof.intent_sha256 != reserved.intent_sha256
    ):
        raise FinalEvidenceError("MUTATION_RESERVATION_AUTHORITY_MISMATCH")
    last_attempt = _attempt_for_request(records, reserved.request_sha256)
    if not _record_relates_to_attempt(barrier.last_mutation_record, last_attempt):
        raise FinalEvidenceError("MUTATION_BARRIER_LINEAGE_MISMATCH")

    first_final_read = min(item.prepared_record.sequence for item in bundle.provenances)
    related_before_final = tuple(
        record
        for record in records
        if record.sequence < first_final_read and _record_relates_to_attempt(record, last_attempt)
    )
    if not related_before_final or related_before_final[-1] != barrier.last_mutation_record:
        reasons.add("FINAL_READ_NOT_AFTER_MUTATION")

    attempts = tuple(
        attempt
        for record in records
        if type(attempt := getattr(record.event, "attempt", None)) is MutationAttempt
    )
    mutation_records = tuple(
        record
        for record in records
        if any(_record_relates_to_attempt(record, attempt) for attempt in attempts)
    )
    if not mutation_records or mutation_records[-1] != barrier.last_mutation_record:
        reasons.add("FINAL_READ_NOT_AFTER_MUTATION")

    states: list[str] = []
    for attempt in attempts:
        try:
            state = journal.frontier(attempt.attempt_id)
        except ExecutionJournalError as exc:
            raise FinalEvidenceError("MUTATION_FRONTIER_REPLAY_FAILED") from exc
        states.append(state.value)
        if state is FrontierState.UNKNOWN:
            reasons.add("MUTATION_FRONTIER_UNKNOWN")
        elif state not in {FrontierState.CONFIRMED, FrontierState.NOT_DISPATCHED}:
            reasons.add("MUTATION_FRONTIER_NOT_FINAL")
    if not states:
        raise FinalEvidenceError("MUTATION_FRONTIER_MISSING")
    return tuple(states)


def validate_final_evidence(
    bundle: FinalEvidenceBundle,
    execution_journal: ExecutionJournal,
) -> FinalVerification:
    """Replay journal authorities, then recompute freshness and clean state."""

    if type(bundle) is not FinalEvidenceBundle:
        raise FinalEvidenceError("INVALID_FINAL_EVIDENCE_BUNDLE")
    records = _replay_execution_journal(execution_journal)
    reasons: set[str] = set()
    barrier = bundle.barrier
    reservations: set[str] = set()
    all_sequences: set[int] = set()

    _validate_preflight_evidence(bundle.preflight, execution_journal, records)
    if bundle.preflight.intent_binding_record.sequence >= barrier.last_mutation_record.sequence:
        reasons.add("PREFLIGHT_NOT_BEFORE_MUTATION")

    for provenance in bundle.provenances:
        _assert_record_replayed(provenance.prepared_record, records)
        _assert_record_replayed(provenance.result_record, records)
        if (
            provenance.reservation_sequence <= barrier.last_mutation_record.sequence
            or provenance.reservation_monotonic_sequence
            <= barrier.last_request.ledger.total_http_requests
        ):
            reasons.add("FINAL_READ_NOT_AFTER_MUTATION")
        if (
            provenance.observed_sequence <= provenance.reservation_sequence
            or provenance.observed_monotonic_sequence != provenance.reservation_monotonic_sequence
        ):
            reasons.add("FINAL_OBSERVATION_NOT_AFTER_RESERVATION")
        if provenance.reservation_sha256 in reservations:
            reasons.add("DUPLICATE_FINAL_RESERVATION")
        reservations.add(provenance.reservation_sha256)
        for sequence in (provenance.reservation_sequence, provenance.observed_sequence):
            if sequence in all_sequences:
                reasons.add("FINAL_SEQUENCE_COLLISION")
            all_sequences.add(sequence)

    bundle_requests = tuple(item.reservation_sha256 for item in bundle.provenances)
    durable_evidence_prepared = tuple(
        proof.request_sha256
        for record in records
        if record.sequence > barrier.last_mutation_record.sequence
        and type(proof := getattr(record.event, "proof", None)) is ReadReservationProof
        and proof.purpose is ReadPurpose.EVIDENCE
    )
    durable_evidence_results = tuple(
        proof.request_sha256
        for record in records
        if record.sequence > barrier.last_mutation_record.sequence
        and type(proof := getattr(record.event, "proof", None)) is ReadResultProof
        and proof.request_sha256 in durable_evidence_prepared
    )
    if bundle_requests != durable_evidence_prepared or bundle_requests != durable_evidence_results:
        reasons.add("FINAL_EVIDENCE_SET_NOT_LATEST")
    if tuple(item.kind for item in bundle.provenances) != _FROZEN_FINAL_SCHEDULE:
        reasons.add("FINAL_READ_SCHEDULE_MISMATCH")

    read_chain = sorted(bundle.provenances, key=lambda item: item.reservation_sequence)
    expected_monotonic = range(
        barrier.last_request.ledger.total_http_requests + 1,
        barrier.last_request.ledger.total_http_requests + 1 + len(read_chain),
    )
    if tuple(item.reservation_monotonic_sequence for item in read_chain) != tuple(
        expected_monotonic
    ):
        reasons.add("FINAL_READ_MONOTONIC_CHAIN_BROKEN")

    ordered = sorted(bundle.provenances, key=lambda item: item.observed_sequence)
    for earlier, later in pairwise(ordered):
        if (
            later.observed_monotonic_sequence <= earlier.observed_monotonic_sequence
            or later.observed_at_ns <= earlier.observed_at_ns
        ):
            reasons.add("FINAL_CLOCK_SEQUENCE_CONTRADICTION")

    mutation_states = _validate_mutation_barrier(
        bundle,
        execution_journal,
        records,
        reasons,
    )

    expected_outcomes = (
        ReadOutcome.ORDER_TERMINAL,
        ReadOutcome.NEGATIVE,
        ReadOutcome.NEGATIVE,
        ReadOutcome.NEGATIVE,
        ReadOutcome.NEGATIVE,
        ReadOutcome.SUCCESS,
        ReadOutcome.SUCCESS,
    )
    for provenance, expected_outcome in zip(
        bundle.provenances,
        expected_outcomes,
        strict=True,
    ):
        if provenance.result_proof.outcome is not expected_outcome:
            reasons.add("FINAL_READ_OUTCOME_MISMATCH")

    order = bundle.order
    order_query = dict(order.provenance.reserved_request.parameters)
    last_attempt = _attempt_for_request(
        records,
        barrier.last_request.request_sha256,
    )
    if any(
        provenance.reservation_proof.authorization_id != last_attempt.authorization_id
        or provenance.reservation_proof.intent_sha256 != last_attempt.intent_sha256
        or provenance.reservation_proof.generation != last_attempt.generation
        for provenance in bundle.provenances
    ):
        reasons.add("FINAL_READ_SESSION_LINEAGE_MISMATCH")
    if (
        order_query.get("origClientOrderId") != order.client_order_id
        or order.client_order_id != last_attempt.client_id
    ):
        reasons.add("FINAL_ORDER_RECONCILIATION_KEY_MISMATCH")
    if order.status is OrderFinalStatus.FILLED:
        reasons.add("PROBE_ORDER_FILLED")
    elif order.status is OrderFinalStatus.UNKNOWN:
        reasons.add("PROBE_ORDER_UNKNOWN")
    elif order.status is not OrderFinalStatus.CANCELED:
        reasons.add("PROBE_ORDER_NOT_CANCELED")
    if order.executed_quantity != 0:
        reasons.add("PROBE_EXECUTED_QUANTITY_NONZERO")
    if bundle.open_orders.open_order_id_sha256:
        reasons.add("FINAL_REGULAR_ORDERS_OPEN")
    if bundle.open_algo_orders.open_algo_order_id_sha256:
        reasons.add("FINAL_ALGO_ORDERS_OPEN")

    if bundle.position.nonzero_positions:
        reasons.add("FINAL_POSITION_NOT_FLAT")

    if bundle.trade.relevant_trades:
        reasons.add("RELEVANT_TRADES_PRESENT")
    baseline_trade_ids = {
        trade.trade_id_sha256
        for trade in _project_preflight_trades(bundle.preflight.by_kind(PreflightKind.TRADE))
    }
    final_trades = _project_trades(bundle.trade.provenance)
    final_order_id = _project_order(bundle.order.provenance)[3]
    if any(
        trade.trade_id_sha256 not in baseline_trade_ids and trade.order_id_sha256 != final_order_id
        for trade in final_trades
    ):
        reasons.add("UNEXPECTED_TRADES_PRESENT")
    if bundle.trade.fee_delta != 0:
        reasons.add("FINAL_FEE_DELTA_NONZERO")

    account = bundle.account
    if not account.can_trade:
        reasons.add("FINAL_ACCOUNT_CANNOT_TRADE")
    if not account.single_asset_mode:
        reasons.add("FINAL_ASSET_MODE_MISMATCH")
    if not bundle.symbol_config.symbol_config_matches:
        reasons.add("FINAL_SYMBOL_CONFIG_MISMATCH")
    if not bundle.position_mode.position_mode_one_way:
        reasons.add("FINAL_POSITION_MODE_MISMATCH")
    if account.wallet_delta != 0:
        reasons.add("FINAL_ACCOUNT_DELTA_NONZERO")

    reason_codes = tuple(sorted(reasons))
    return FinalVerification(
        review_eligible=not reason_codes,
        reason_codes=reason_codes,
        mutation_states=mutation_states,
        gate_pass_declared=False,
    )


def _bind_process_exit(
    verification: FinalVerification,
    reap: ReapAttestation,
) -> FinalVerification:
    reasons = set(verification.reason_codes)
    if reap.returncode != 0 or reap.signal is not None:
        reasons.add("CREDENTIAL_CHILD_EXIT_NONZERO")
    reason_codes = tuple(sorted(reasons))
    return FinalVerification(
        review_eligible=not reason_codes,
        reason_codes=reason_codes,
        mutation_states=verification.mutation_states,
        gate_pass_declared=False,
    )


def _reap_artifact_sha256(reap: ReapAttestation) -> str:
    """Digest a verified reap for artifact integrity, never as provenance."""

    material = {
        "attested_monotonic_ns": reap.attested_monotonic_ns,
        "descendant_creation_denied": reap.descendant_creation_denied,
        "exact_pid_waited": reap.exact_pid_waited,
        "execution_head_digest": reap.execution_head_digest,
        "execution_head_sequence": reap.execution_head_sequence,
        "execution_journal_digest": reap.execution_journal_digest,
        "execution_journal_sequence": reap.execution_journal_sequence,
        "generation": reap.generation,
        "identity": reap.identity.to_payload(),
        "journal_digest": reap.journal_digest,
        "journal_head_digest": reap.journal_head_digest,
        "journal_head_sequence": reap.journal_head_sequence,
        "journal_sequence": reap.journal_sequence,
        "local_process_quiesced": reap.local_process_quiesced,
        "process_identity_sha256": reap.process_identity_sha256,
        "returncode": reap.returncode,
        "schema_version": "gate1b.verified-reap-artifact.v1",
        "signal": reap.signal,
        "stage_ordinal": reap.stage_ordinal,
        "venue_mutation_absent_proven": reap.venue_mutation_absent_proven,
        "waited_pid": reap.waited_pid,
    }
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class LeakScanReport:
    leak_count: int
    findings: tuple[str, ...]


def _normalized_key(value: object) -> str:
    if type(value) is not str:
        return ""
    return value.strip().lower().replace("-", "_")


def _find_forbidden_fields(value: Any, location: str, findings: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in _FORBIDDEN_RETAINED_FIELDS:
                findings.add(f"{location}:FORBIDDEN_FIELD")
            _find_forbidden_fields(child, location, findings)
    elif isinstance(value, list):
        for child in value:
            _find_forbidden_fields(child, location, findings)


def _scan_payload(
    location: str,
    payload: bytes,
    canary_tokens: tuple[str, ...],
) -> tuple[str, ...]:
    findings: set[str] = set()
    lowered = payload.lower()
    for marker in _FORBIDDEN_RAW_MARKERS:
        if marker in lowered:
            findings.add(f"{location}:CREDENTIAL_MARKER")
    if b"signature=" in lowered and (b"?" in lowered or b"&" in lowered):
        findings.add(f"{location}:SIGNED_REQUEST_MATERIAL")
    for token in canary_tokens:
        if token.encode("utf-8") in payload:
            findings.add(f"{location}:CANARY_TOKEN")

    stripped = payload.lstrip()
    try:
        if stripped.startswith((b"{", b"[")):
            parsed = json.loads(payload)
            _find_forbidden_fields(parsed, location, findings)
        elif location.endswith(".jsonl") or location.endswith(".jsonl.head"):
            for line in payload.splitlines():
                if line.strip():
                    _find_forbidden_fields(json.loads(line), location, findings)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Malformed evidence is handled by its schema verifier.  The raw marker
        # and canary scans above remain valid and must not echo retained bytes.
        pass
    return tuple(sorted(findings))


def _validated_canaries(canary_tokens: tuple[str, ...]) -> tuple[str, ...]:
    if type(canary_tokens) is not tuple or any(
        type(token) is not str or not token for token in canary_tokens
    ):
        raise FinalEvidenceError("INVALID_SYNTHETIC_CANARY")
    return canary_tokens


def scan_evidence_tree(
    root: Path,
    *,
    canary_tokens: tuple[str, ...] = (),
) -> LeakScanReport:
    """Scan retained bytes without returning any matched secret/canary value."""

    root = Path(root)
    canaries = _validated_canaries(canary_tokens)
    findings: set[str] = set()
    if not root.exists():
        return LeakScanReport(leak_count=0, findings=())
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            findings.add(f"{relative}:SYMLINK_NOT_SCANNED")
            continue
        try:
            mode = candidate.stat().st_mode
        except OSError:
            findings.add(f"{relative}:UNREADABLE_ARTIFACT")
            continue
        if not stat.S_ISREG(mode):
            continue
        try:
            payload = candidate.read_bytes()
        except OSError:
            findings.add(f"{relative}:UNREADABLE_ARTIFACT")
            continue
        findings.update(_scan_payload(relative, payload, canaries))
    ordered = tuple(sorted(findings))
    return LeakScanReport(leak_count=len(ordered), findings=ordered)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short evidence write")
        offset += written


def _open_owner_evidence_directory(path: Path) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinalEvidenceError("FINALIZATION_DIRECTORY_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise FinalEvidenceError("FINALIZATION_DIRECTORY_NOT_OWNER_ONLY")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise FinalEvidenceError("FINALIZATION_DIRECTORY_PATH_RACE")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _assert_evidence_directory_identity(path: Path, expected: os.stat_result) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise FinalEvidenceError("FINALIZATION_DIRECTORY_PATH_RACE") from exc
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise FinalEvidenceError("FINALIZATION_DIRECTORY_PATH_RACE")


def _create_evidence_temporary(parent_fd: int, target_name: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(16):
        name = f".{target_name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise FinalEvidenceError("FINALIZATION_TEMPORARY_CREATE_FAILED") from exc
        return descriptor, name
    raise FinalEvidenceError("FINALIZATION_TEMPORARY_CREATE_FAILED")


def _record_from_artifact_anchor(
    records: tuple[JournalRecord, ...],
    anchor: dict[str, object],
) -> JournalRecord:
    if set(anchor) != {"digest", "sequence"}:
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_JOURNAL_ANCHOR_INVALID")
    sequence = anchor["sequence"]
    digest = anchor["digest"]
    if (
        type(sequence) is not int
        or sequence <= 0
        or sequence > len(records)
        or type(digest) is not str
    ):
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_JOURNAL_ANCHOR_INVALID")
    record = records[sequence - 1]
    if record.sequence != sequence or record.digest != digest:
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_JOURNAL_ANCHOR_MISMATCH")
    return record


def _read_preflight_projection_artifact(
    path: Path,
) -> tuple[dict[str, object], bytes]:
    if not isinstance(path, Path) or path.name != "preflight.json":
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_PATH_INVALID")
    parent_fd, parent_stat = _open_owner_evidence_directory(path.parent)
    descriptor = -1
    try:
        try:
            entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_UNREADABLE") from exc
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_size <= 0
            or entry.st_size > 1_048_576
        ):
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_FILE_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_PATH_RACE")
        chunks: list[bytes] = []
        remaining = 1_048_577
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        artifact_bytes = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            retained = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_PATH_RACE") from exc
        if (retained.st_dev, retained.st_ino) != (after.st_dev, after.st_ino):
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_PATH_RACE")
        if (
            len(artifact_bytes) != entry.st_size
            or len(artifact_bytes) > 1_048_576
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise FinalEvidenceError("PREFLIGHT_ARTIFACT_CHANGED_DURING_READ")
        _assert_evidence_directory_identity(path.parent, parent_stat)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError("duplicate or non-string JSON key")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            artifact_bytes.decode("ascii"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_JSON_INVALID") from exc
    if type(parsed) is not dict or artifact_bytes != _pretty_json_bytes(parsed):
        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_NOT_CANONICAL")
    return parsed, artifact_bytes


def _atomic_write_owner_only(path: Path, payload: bytes) -> None:
    """Create one non-overwriting owner-only file and fsync file plus directory."""

    path = Path(path)
    if path.name in {"", ".", ".."} or type(payload) is not bytes:
        raise FinalEvidenceError("FINALIZATION_ARTIFACT_INVALID")
    parent_fd, parent_stat = _open_owner_evidence_directory(path.parent)
    temporary_name: str | None = None
    temporary_fd = -1
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise FinalEvidenceError("FINALIZATION_DESTINATION_CHECK_FAILED") from exc
        else:
            raise FinalEvidenceError(f"FINALIZATION_ARTIFACT_EXISTS:{path.name}")
        try:
            temporary_fd, temporary_name = _create_evidence_temporary(
                parent_fd,
                path.name,
            )
            os.fchmod(temporary_fd, 0o600)
            _write_all(temporary_fd, payload)
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
        except FinalEvidenceError:
            raise
        except OSError as exc:
            raise FinalEvidenceError("FINALIZATION_ARTIFACT_WRITE_FAILED") from exc
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise FinalEvidenceError(f"FINALIZATION_ARTIFACT_EXISTS:{path.name}") from None
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FinalEvidenceError(f"FINALIZATION_ARTIFACT_EXISTS:{path.name}") from None
            raise FinalEvidenceError("FINALIZATION_ARTIFACT_PUBLICATION_FAILED") from exc
        try:
            published_stat = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise FinalEvidenceError("FINALIZATION_ARTIFACT_PUBLICATION_FAILED") from exc
        if (temporary_stat.st_dev, temporary_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ):
            raise FinalEvidenceError("FINALIZATION_TEMPORARY_INODE_CHANGED")
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_name = None
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise FinalEvidenceError("FINALIZATION_DIRECTORY_FSYNC_FAILED") from exc
        _assert_evidence_directory_identity(path.parent, parent_stat)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def _provenance_payload(provenance: FinalReadProvenance) -> dict[str, object]:
    reservation = provenance.reservation_proof
    result = provenance.result_proof
    return {
        "evidence_kind": provenance.kind.value,
        "prepared_record": {
            "digest": provenance.prepared_record.digest,
            "sequence": provenance.prepared_record.sequence,
        },
        "read_reservation": {
            "logical_request_sha256": reservation.logical_request_sha256,
            "monotonic_sequence": reservation.monotonic_sequence,
            "proof_sha256": reservation.proof_sha256,
            "request_sha256": reservation.request_sha256,
        },
        "read_result": {
            "observed_at_ns": result.observed_at_ns,
            "outcome": result.outcome.value,
            "result_proof_sha256": result.result_proof_sha256,
            "result_sha256": result.result_sha256,
        },
        "transport_result": {
            "fields": dict(provenance.transport_result.fields),
            "kind": provenance.transport_result.kind.value,
            "logical_request_sha256": provenance.transport_result.logical_request_sha256,
            "request_sha256": provenance.transport_result.request_sha256,
            "result_sha256": provenance.transport_result.result_sha256,
        },
        "result_record": {
            "digest": provenance.result_record.digest,
            "sequence": provenance.result_record.sequence,
        },
    }


def _preflight_provenance_payload(
    provenance: PreIntentReadProvenance,
) -> dict[str, object]:
    result = getattr(provenance.result_record.event, "result", None)
    if type(result) is not PreIntentReadResult:  # pragma: no cover - constructor bound
        raise FinalEvidenceError("PREFLIGHT_READ_AUTHORITY_MISMATCH")
    return {
        "kind": provenance.kind.value,
        "prepared_record": {
            "digest": provenance.prepared_record.digest,
            "sequence": provenance.prepared_record.sequence,
        },
        "reservation": {
            "deadline_ns": provenance.reservation.deadline_ns,
            "generation": provenance.reservation.generation,
            "logical_request_sha256": provenance.reservation.logical_request_sha256,
            "path": provenance.reservation.path,
            "reservation_sha256": provenance.reservation.reservation_sha256,
        },
        "result_record": {
            "digest": provenance.result_record.digest,
            "sequence": provenance.result_record.sequence,
        },
        "transport_result": {
            "fields": dict(provenance.transport_result.fields),
            "kind": provenance.transport_result.kind.value,
            "logical_request_sha256": provenance.transport_result.logical_request_sha256,
            "request_sha256": provenance.transport_result.request_sha256,
            "result_sha256": provenance.transport_result.result_sha256,
        },
        "observed_at_ns": result.observed_at_ns,
    }


def _preflight_projection_artifact_payload(
    projection: PreflightProjection,
) -> dict[str, object]:
    account = projection.account_state
    symbol = projection.symbol_state
    filters = projection.filters
    derivation = projection.order_derivation
    intent = projection.intent
    return {
        "account_state": {
            "auto_add_margin": account.auto_add_margin,
            "available_balance": _canonical_decimal(account.available_balance),
            "can_trade": account.can_trade,
            "dual_side_position": account.dual_side_position,
            "leverage": account.leverage,
            "margin_type": account.margin_type,
            "multi_assets_margin": account.multi_assets_margin,
            "nonzero_positions": [
                {"quantity": _canonical_decimal(quantity), "symbol": symbol_name}
                for symbol_name, quantity in account.nonzero_positions
            ],
            "open_algo_order_ids": list(account.open_algo_order_ids),
            "open_regular_order_ids": list(account.open_regular_order_ids),
            "server_time_skew_ms": _canonical_decimal(account.server_time_skew_ms),
            "wallet_balance": _canonical_decimal(account.wallet_balance),
        },
        "authority": {
            "authorization_id": projection.authority.authorization_id,
            "authority_sha256": projection.authority.authority_sha256,
            "client_id": projection.authority.client_id,
            "generation": projection.authority.generation,
            "record_digest": projection.authority_record.digest,
            "record_sequence": projection.authority_record.sequence,
            "runtime_commit": projection.authority.runtime_commit,
            "session_nonce": projection.authority.session_nonce,
        },
        "baseline_trades": [
            {
                "commission": _canonical_decimal(trade.fee),
                "orderIdSha256": trade.order_id_sha256,
                "quantity": _canonical_decimal(trade.quantity),
                "realizedPnl": _canonical_decimal(trade.realized_pnl),
                "tradeIdSha256": trade.trade_id_sha256,
            }
            for trade in projection.baseline_trades
        ],
        "filters": {
            "filter_contract_sha256": filters.canonical_sha256,
            "lot_size_filter_count": filters.lot_size_filter_count,
            "max_price": _canonical_decimal(filters.max_price),
            "max_quantity": _canonical_decimal(filters.max_quantity),
            "min_notional": _canonical_decimal(filters.min_notional),
            "min_notional_filter_count": filters.min_notional_filter_count,
            "min_price": _canonical_decimal(filters.min_price),
            "min_quantity": _canonical_decimal(filters.min_quantity),
            "percent_price_filter_count": filters.percent_price_filter_count,
            "percent_price_multiplier_down": _canonical_decimal(
                filters.percent_price_multiplier_down
            ),
            "percent_price_multiplier_up": _canonical_decimal(filters.percent_price_multiplier_up),
            "price_filter_count": filters.price_filter_count,
            "step_size": _canonical_decimal(filters.step_size),
            "tick_size": _canonical_decimal(filters.tick_size),
            "uninterpreted_applicable_filter_types": list(
                filters.uninterpreted_applicable_filter_types
            ),
        },
        "intent_candidate": {
            "client_order_id": intent.client_order_id,
            "filter_snapshot_sha256": intent.filter_snapshot_sha256,
            "intent_sha256": intent.intent_sha256,
            "persisted": intent.persisted,
            "probe_payload": intent.probe_payload,
            "protocol_commit": intent.protocol_commit,
            "protocol_sha256": intent.protocol_sha256,
            "protocol_tag_object": intent.protocol_tag_object,
        },
        "order_derivation": {
            "best_ask": _canonical_decimal(derivation.best_ask),
            "best_bid": _canonical_decimal(derivation.best_bid),
            "book_age_ms": _canonical_decimal(derivation.book_age_ms),
            "filter_contract_sha256": derivation.filter_contract_sha256,
            "filter_snapshot_sha256": derivation.filter_snapshot_sha256,
            "mark_age_ms": _canonical_decimal(derivation.mark_age_ms),
            "mark_price": _canonical_decimal(derivation.mark_price),
            "observed_elapsed_seconds": _canonical_decimal(derivation.observed_elapsed_seconds),
            "order_derivation_sha256": derivation.canonical_sha256,
        },
        "reads": [
            _preflight_provenance_payload(provenance) for provenance in projection.provenances
        ],
        "schema_version": "gate1b.preflight-projection.v1",
        "symbol_state": {
            "contract_type": symbol.contract_type,
            "filter_type_counts": dict(symbol.filter_type_counts),
            "margin_asset": symbol.margin_asset,
            "order_types": sorted(symbol.order_types),
            "quote_asset": symbol.quote_asset,
            "status": symbol.status,
            "symbol": symbol.symbol,
            "time_in_force": sorted(symbol.time_in_force),
            "uninterpreted_applicable_filter_types": list(
                symbol.uninterpreted_applicable_filter_types
            ),
        },
    }


def _preflight_artifact_payload(
    preflight: PreflightEvidenceBundle,
    projection: PreflightProjection,
) -> dict[str, object]:
    binding = getattr(preflight.intent_binding_record.event, "binding", None)
    if type(binding) is not IntentChainBinding:  # pragma: no cover - constructor bound
        raise FinalEvidenceError("PREFLIGHT_EVIDENCE_AUTHORITY_MISMATCH")
    payload = projection.artifact_payload
    payload.update(
        {
            "intent_binding": {
                "binding_sha256": binding.binding_sha256,
                "intent_file_sha256": binding.intent_file_sha256,
                "intent_sha256": binding.intent_sha256,
                "last_ledger_sha256": binding.last_ledger_sha256,
                "pre_intent_chain_sha256": binding.pre_intent_chain_sha256,
                "record_digest": preflight.intent_binding_record.digest,
                "record_sequence": preflight.intent_binding_record.sequence,
            },
            "schema_version": "gate1b.preflight-evidence.v2",
        }
    )
    return payload


def _bundle_artifact_payloads(
    bundle: FinalEvidenceBundle,
    verification: FinalVerification,
    execution_journal: ExecutionJournal,
) -> dict[str, dict[str, object]]:
    intent = bundle.preflight.persisted_intent.intent
    preflight_projection = project_preflight_evidence(
        authority=bundle.preflight.authority,
        authority_record=bundle.preflight.authority_record,
        provenances=bundle.preflight.provenances,
        execution_journal=execution_journal,
        protocol_commit=intent.protocol_commit,
        protocol_tag_object=intent.protocol_tag_object,
        protocol_sha256=intent.protocol_sha256,
    )
    order = {
        **_order_result_payload(bundle.order),
        "provenance": _provenance_payload(bundle.order.provenance),
        "schema_version": "gate1b.final-order.v1",
    }
    open_orders = {
        **_open_orders_result_payload(bundle.open_orders),
        "provenance": _provenance_payload(bundle.open_orders.provenance),
        "schema_version": "gate1b.final-open-orders.v1",
    }
    open_algo_orders = {
        **_open_algo_orders_result_payload(bundle.open_algo_orders),
        "provenance": _provenance_payload(bundle.open_algo_orders.provenance),
        "schema_version": "gate1b.final-open-algo-orders.v1",
    }
    position = {
        **_position_result_payload(bundle.position),
        "provenance": _provenance_payload(bundle.position.provenance),
        "schema_version": "gate1b.final-position.v1",
    }
    trade = {
        **_trade_result_payload(bundle.trade),
        "provenance": _provenance_payload(bundle.trade.provenance),
        "schema_version": "gate1b.final-trade.v1",
    }
    account = {
        **_account_result_payload(bundle.account),
        "provenance": _provenance_payload(bundle.account.provenance),
        "schema_version": "gate1b.final-account.v1",
    }
    symbol_config = {
        **_symbol_config_result_payload(bundle.symbol_config),
        "provenance": _provenance_payload(bundle.symbol_config.provenance),
        "schema_version": "gate1b.final-symbol-config.v1",
    }
    position_mode = {
        **_position_mode_result_payload(bundle.position_mode),
        "provenance": _provenance_payload(bundle.position_mode.provenance),
        "schema_version": "gate1b.final-position-mode.v1",
    }
    final_state = {
        "mutation_barrier": {
            "last_mutation_record_digest": bundle.barrier.last_mutation_record.digest,
            "last_mutation_record_sequence": bundle.barrier.last_mutation_record.sequence,
            "last_request_monotonic_sequence": (
                bundle.barrier.last_request.ledger.total_http_requests
            ),
            "last_request_sha256": bundle.barrier.last_request.request_sha256,
            "mutation_states": list(verification.mutation_states),
        },
        "schema_version": "gate1b.final-state.v1",
    }
    return {
        "final-account.json": account,
        "final-open-algo-orders.json": open_algo_orders,
        "final-open-orders.json": open_orders,
        "final-order.json": order,
        "final-position.json": position,
        "final-position-mode.json": position_mode,
        "final-state.json": final_state,
        "final-symbol-config.json": symbol_config,
        "final-trade.json": trade,
        "preflight.json": preflight_projection.artifact_payload,
    }


def final_evidence_bundle_sha256(
    bundle: FinalEvidenceBundle,
    execution_journal: ExecutionJournal,
) -> str:
    """Return a replay-validated domain digest of the canonical final artifacts."""

    if type(bundle) is not FinalEvidenceBundle:
        raise FinalEvidenceError("INVALID_FINAL_EVIDENCE_BUNDLE")
    if type(execution_journal) is not ExecutionJournal:
        raise FinalEvidenceError("EXECUTION_JOURNAL_REQUIRED")
    _validate_direct_transport_projections(bundle)
    verification = validate_final_evidence(bundle, execution_journal)
    payloads = _bundle_artifact_payloads(
        bundle,
        verification,
        execution_journal,
    )
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "artifacts": payloads,
                "schema_version": "gate1b.final-evidence-bundle-digest.v1",
            }
        )
    ).hexdigest()


def _process_exit_payload(
    reap: ReapAttestation,
    reap_artifact_sha256: str,
) -> dict[str, object]:
    return {
        "attestation_sha256": reap_artifact_sha256,
        "attested_monotonic_ns": reap.attested_monotonic_ns,
        "attested_sequence": reap.execution_journal_sequence,
        "descendants_quiesced": reap.descendant_creation_denied,
        "exact_pid_reaped": reap.exact_pid_waited,
        "execution_journal_digest": reap.execution_journal_digest,
        "execution_journal_sequence": reap.execution_journal_sequence,
        "finalization_authority": "CREDENTIAL_FREE_SUPERVISOR",
        "generation": reap.generation,
        "local_process_quiesced": reap.local_process_quiesced,
        "pid": reap.identity.pid,
        "process_identity_sha256": reap.process_identity_sha256,
        "process_journal_digest": reap.journal_digest,
        "process_journal_sequence": reap.journal_sequence,
        "returncode": reap.returncode,
        "schema_version": "gate1b.process-exit.v1",
        "signal": reap.signal,
        "venue_mutation_absent_proven": reap.venue_mutation_absent_proven,
        "waited_pid": reap.waited_pid,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detached_payload(path: Path) -> bytes:
    return f"{_sha256_file(path)}  {path.name}\n".encode("ascii")


@dataclass(frozen=True, slots=True)
class FinalizedEvidence:
    verification: FinalVerification
    process_exit_path: Path
    manifest_path: Path
    manifest_hash_path: Path
    verdict_path: Path
    verdict_hash_path: Path


def _verify_exact_reap_replay(
    reap: ReapAttestation,
    execution_journal: ExecutionJournal,
    records: tuple[JournalRecord, ...],
) -> None:
    if reap.process_journal_path.is_symlink():
        raise FinalEvidenceError("PROCESS_REAP_NOT_REPLAYED")
    try:
        process_journal = ProcessLifecycleJournal.restore(reap.process_journal_path)
        if process_journal.execution_journal_path.resolve() != execution_journal.path.resolve():
            raise ProcessBoundaryError("EXECUTION_JOURNAL_PATH_MISMATCH")
        process_journal.verify_reap_attestation(reap)
        if (
            process_journal.active_identity is not None
            or process_journal.active_generation is not None
            or process_journal.last_generation != reap.generation
        ):
            raise ProcessBoundaryError("PROCESS_REAP_NOT_LATEST")
    except (OSError, ProcessBoundaryError) as exc:
        raise FinalEvidenceError("PROCESS_REAP_NOT_REPLAYED") from exc

    execution_sequence = reap.execution_journal_sequence
    if (
        type(execution_sequence) is not int
        or not 0 < execution_sequence <= len(records)
        or records[execution_sequence - 1].digest != reap.execution_journal_digest
    ):
        raise FinalEvidenceError("PROCESS_REAP_NOT_REPLAYED")


def _validate_exact_reap_replay(
    reap: ReapAttestation,
    execution_journal: ExecutionJournal,
    records: tuple[JournalRecord, ...],
    bundle: FinalEvidenceBundle,
) -> None:
    _verify_exact_reap_replay(reap, execution_journal, records)
    execution_sequence = reap.execution_journal_sequence
    latest_sequence = max(item.observed_sequence for item in bundle.provenances)
    latest_monotonic_ns = max(item.observed_at_ns for item in bundle.provenances)
    if (
        execution_sequence <= latest_sequence
        or reap.attested_monotonic_ns <= latest_monotonic_ns
        or any(item.reservation_proof.generation != reap.generation for item in bundle.provenances)
    ):
        raise FinalEvidenceError("REAP_NOT_AFTER_FINAL_EVIDENCE")


class FinalEvidenceFinalizer:
    """Owner-only finalizer invoked by the credential-free supervisor."""

    def __init__(
        self,
        *,
        root: Path,
        execution_journal_path: Path,
        supervisor_environment: Mapping[str, str],
        canary_tokens: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(supervisor_environment, Mapping):
            raise FinalEvidenceError("INVALID_SUPERVISOR_ENVIRONMENT")
        if any(
            type(name) is not str or type(value) is not str
            for name, value in supervisor_environment.items()
        ):
            raise FinalEvidenceError("INVALID_SUPERVISOR_ENVIRONMENT")
        self._canary_tokens = _validated_canaries(canary_tokens)
        contaminated = sorted(
            name
            for name in supervisor_environment
            if name in _CREDENTIAL_ENVIRONMENT_NAMES
            or _normalized_key(name) in _FORBIDDEN_RETAINED_FIELDS
        )
        environment_findings = {
            finding
            for name, value in supervisor_environment.items()
            for finding in _scan_payload(
                "supervisor-environment",
                f"{name}={value}".encode(),
                self._canary_tokens,
            )
        }
        if contaminated or environment_findings:
            raise FinalEvidenceError("SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT")
        self.root = Path(root)
        self._execution_journal_path = Path(execution_journal_path)
        self._prepare_root()

    def _execution_journal(self) -> ExecutionJournal:
        path = self._execution_journal_path
        if path.is_symlink() or not path.exists() or path.parent.resolve() != self.root.resolve():
            raise FinalEvidenceError("EXECUTION_JOURNAL_REPLAY_FAILED")
        try:
            return ExecutionJournal(path)
        except (OSError, ExecutionJournalError) as exc:
            raise FinalEvidenceError("EXECUTION_JOURNAL_REPLAY_FAILED") from exc

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise FinalEvidenceError("EVIDENCE_ROOT_SYMLINK")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = self.root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) & 0o077
        ):
            raise FinalEvidenceError("EVIDENCE_ROOT_NOT_OWNER_ONLY")
        os.chmod(self.root, 0o700)

    def _validate_artifact_tree(self) -> None:
        for candidate in sorted(self.root.rglob("*")):
            relative = candidate.relative_to(self.root).as_posix()
            if candidate.is_symlink():
                raise FinalEvidenceError(f"ARTIFACT_SYMLINK:{relative}")
            entry = candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise FinalEvidenceError(f"ARTIFACT_NOT_ALLOWLISTED:{relative}")
            if relative not in ARTIFACT_ALLOWLIST:
                raise FinalEvidenceError(f"ARTIFACT_NOT_ALLOWLISTED:{relative}")
            if entry.st_uid != os.geteuid() or stat.S_IMODE(entry.st_mode) & 0o077:
                raise FinalEvidenceError(f"ARTIFACT_NOT_OWNER_ONLY:{relative}")

    def _assert_no_leaks(self) -> None:
        report = scan_evidence_tree(self.root, canary_tokens=self._canary_tokens)
        if report.leak_count:
            raise FinalEvidenceError("CREDENTIAL_MATERIAL_DETECTED")

    def _assert_payloads_secret_free(self, payloads: Mapping[str, bytes]) -> None:
        findings: set[str] = set()
        for name, payload in payloads.items():
            findings.update(_scan_payload(name, payload, self._canary_tokens))
        if findings:
            raise FinalEvidenceError("CREDENTIAL_MATERIAL_DETECTED")

    def _assert_finalization_targets_absent(
        self,
        *,
        expected_preflight: bytes | None,
    ) -> None:
        for name in _GENERATED_ARTIFACTS:
            path = self.root / name
            exists = path.exists() or path.is_symlink()
            if name == "preflight.json":
                if expected_preflight is not None:
                    if not exists:
                        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_REQUIRED")
                    _payload, retained = _read_preflight_projection_artifact(path)
                    if retained != expected_preflight:
                        raise FinalEvidenceError("PREFLIGHT_ARTIFACT_RECONSTRUCTION_MISMATCH")
                continue
            if exists:
                raise FinalEvidenceError(f"FINALIZATION_ARTIFACT_EXISTS:{name}")

    def _manifest_entries(self) -> list[dict[str, object]]:
        excluded = {
            "manifest.json",
            "manifest.json.sha256",
            "verdict.json",
            "verdict.json.sha256",
        }
        entries: list[dict[str, object]] = []
        for candidate in sorted(self.root.iterdir(), key=lambda path: path.name):
            if candidate.name in excluded:
                continue
            entries.append(
                {
                    "path": candidate.name,
                    "sha256": _sha256_file(candidate),
                    "size": candidate.stat().st_size,
                },
            )
        return entries

    def _emit_finalization(
        self,
        *,
        verification: FinalVerification,
        reap: ReapAttestation,
        evidence_payloads: Mapping[str, Mapping[str, object]],
    ) -> FinalizedEvidence:
        reap_artifact_sha256 = _reap_artifact_sha256(reap)
        payloads = {
            "process-exit.json": _pretty_json_bytes(
                _process_exit_payload(reap, reap_artifact_sha256)
            ),
            **{name: _pretty_json_bytes(payload) for name, payload in evidence_payloads.items()},
        }
        expected_preflight = payloads.pop("preflight.json", None)
        self._validate_artifact_tree()
        self._assert_finalization_targets_absent(
            expected_preflight=expected_preflight,
        )
        self._assert_no_leaks()
        # Scan every to-be-retained byte before writing even process-exit.json.
        self._assert_payloads_secret_free(
            {
                **payloads,
                **(
                    {"preflight.json": expected_preflight} if expected_preflight is not None else {}
                ),
            }
        )

        _atomic_write_owner_only(self.root / "process-exit.json", payloads.pop("process-exit.json"))
        for name in sorted(payloads):
            _atomic_write_owner_only(self.root / name, payloads[name])

        self._validate_artifact_tree()
        self._assert_no_leaks()
        manifest_payload = {
            "artifacts": self._manifest_entries(),
            "credential_leak_count": 0,
            "exact_reap_attestation_sha256": reap_artifact_sha256,
            "gate_pass_declared": False,
            "manifest_emitted_after_reap_sequence": reap.execution_journal_sequence,
            "schema_version": "gate1b.final-manifest.v1",
        }
        manifest_bytes = _pretty_json_bytes(manifest_payload)
        self._assert_payloads_secret_free({"manifest.json": manifest_bytes})
        manifest_path = self.root / "manifest.json"
        _atomic_write_owner_only(manifest_path, manifest_bytes)
        manifest_hash_path = self.root / "manifest.json.sha256"
        _atomic_write_owner_only(manifest_hash_path, _detached_payload(manifest_path))

        verdict_payload = {
            "credential_leak_count": 0,
            "exact_reap_attestation_sha256": reap_artifact_sha256,
            "gate_pass_declared": False,
            "manifest_sha256": _sha256_file(manifest_path),
            "reason_codes": list(verification.reason_codes),
            "review_eligible": verification.review_eligible,
            "schema_version": "gate1b.final-verdict.v1",
            "status": (
                "READY_FOR_INDEPENDENT_REVIEW" if verification.review_eligible else "BLOCKED"
            ),
        }
        verdict_bytes = _pretty_json_bytes(verdict_payload)
        self._assert_payloads_secret_free({"verdict.json": verdict_bytes})
        verdict_path = self.root / "verdict.json"
        _atomic_write_owner_only(verdict_path, verdict_bytes)
        verdict_hash_path = self.root / "verdict.json.sha256"
        _atomic_write_owner_only(verdict_hash_path, _detached_payload(verdict_path))

        self._validate_artifact_tree()
        self._assert_no_leaks()
        return FinalizedEvidence(
            verification=verification,
            process_exit_path=self.root / "process-exit.json",
            manifest_path=manifest_path,
            manifest_hash_path=manifest_hash_path,
            verdict_path=verdict_path,
            verdict_hash_path=verdict_hash_path,
        )

    def finalize(
        self,
        bundle: FinalEvidenceBundle,
        reap: ReapAttestation,
    ) -> FinalizedEvidence:
        """Finalize complete evidence after exact reap; never declare a Gate PASS."""

        if type(bundle) is not FinalEvidenceBundle:
            raise FinalEvidenceError("INVALID_FINAL_EVIDENCE_BUNDLE")
        if type(reap) is not ReapAttestation:
            raise FinalEvidenceError("PROCESS_REAP_ATTESTATION_REQUIRED")
        execution_journal = self._execution_journal()
        records = _replay_execution_journal(execution_journal)
        _validate_exact_reap_replay(reap, execution_journal, records, bundle)
        verification = _bind_process_exit(
            validate_final_evidence(bundle, execution_journal),
            reap,
        )
        return self._emit_finalization(
            verification=verification,
            reap=reap,
            evidence_payloads=_bundle_artifact_payloads(
                bundle,
                verification,
                execution_journal,
            ),
        )

    def finalize_blocked(
        self,
        reap: ReapAttestation,
        *,
        cause: BlockedFinalizationCause,
    ) -> FinalizedEvidence:
        """Retain an exact reap after an incomplete final schedule as BLOCKED."""

        if type(cause) is not BlockedFinalizationCause:
            raise FinalEvidenceError("BLOCKED_FINALIZATION_CAUSE_REQUIRED")
        if type(reap) is not ReapAttestation:
            raise FinalEvidenceError("PROCESS_REAP_ATTESTATION_REQUIRED")
        execution_journal = self._execution_journal()
        records = _replay_execution_journal(execution_journal)
        _verify_exact_reap_replay(reap, execution_journal, records)
        verification = _bind_process_exit(
            FinalVerification(
                review_eligible=False,
                reason_codes=("FINAL_EVIDENCE_INCOMPLETE", cause.value),
                mutation_states=(),
                gate_pass_declared=False,
            ),
            reap,
        )
        return self._emit_finalization(
            verification=verification,
            reap=reap,
            evidence_payloads={},
        )
