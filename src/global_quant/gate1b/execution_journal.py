"""Durable mutation-attempt frontier for the Gate 1B process boundary.

The journal accepts only fixed, sanitized event types and fixed unsigned request
parameters.  It stores no credentials, signatures, or arbitrary bodies and
performs no process or network operations.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from global_quant.gate1b.durable_intent import (
    DurableIntentError,
    PersistedIntent,
    load_persisted_intent,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    MAX_READ_RETRIES,
    NORMAL_PRE_CREATE_HTTP_REQUESTS,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    MarketCloseFilters,
    MarketCloseProof,
    MutationLedger,
    MutationProtocolError,
    OwnedPositionProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    build_client_order_id,
    build_emergency_client_order_id,
    validate_market_close_proof,
)

if TYPE_CHECKING:
    from global_quant.gate1b.credential_transport import TransportResult

SCHEMA_VERSION = "gate1b.execution-journal.v1"
HEAD_SCHEMA_VERSION = "gate1b.execution-journal-head.v1"
MAX_RECORD_BYTES = 8_192
ZERO_DIGEST = "0" * 64

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SESSION_NONCE = re.compile(r"^[0-9a-f]{16}$")
_AUTHORIZATION_ID = re.compile(r"^g1b16-[0-9a-f]{16}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_REQUEST_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{1,200}$")
_ATTEMPT_SCHEMA = "gate1b.mutation-attempt.v1"
_MUTATION_RESERVATION_SCHEMA = "gate1b.mutation-reservation.v1"
_READ_RESERVATION_SCHEMA = "gate1b.read-reservation.v1"
_READ_RESULT_SCHEMA = "gate1b.read-result.v1"
_OWNED_FILL_CLOSE_SCHEMA = "gate1b.owned-fill-close-proof.v2"
_OWNED_POSITION_SEMANTICS_SCHEMA = "gate1b.owned-position-semantics.v1"
_STAGED_GENERATION_RECOVERY_SCHEMA = "gate1b.staged-generation-recovery.v1"
_OBSERVATION_SCHEMA = "gate1b.reconciliation-observation.v1"
_SESSION_AUTHORITY_SCHEMA = "gate1b.session-request-authority.v1"
_RECOVERY_SESSION_AUTHORITY_SCHEMA = "gate1b.recovery-session-authority.v1"
_INTENT_BOUND_RECOVERY_AUTHORITY_SCHEMA = "gate1b.intent-bound-recovery-authority.v1"
_PRE_INTENT_READ_SCHEMA = "gate1b.pre-intent-read-reservation.v1"
_PRE_INTENT_RESULT_SCHEMA = "gate1b.pre-intent-read-result.v1"
_PRE_INTENT_FAILURE_SCHEMA = "gate1b.pre-intent-read-failure.v1"
_EXACT_READ_FAILURE_SCHEMA = "gate1b.exact-read-failure.v1"
_INTENT_CHAIN_BINDING_SCHEMA = "gate1b.intent-chain-binding.v1"
_EXACT_REQUEST_RESERVATION_SCHEMA = "gate1b.exact-request-reservation.v1"
_VERIFIED_DISPATCH_RECEIPT_TOKEN = object()
_PRE_INTENT_READ_PATHS = (
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
_SAFE_REQUEST_PARAMETER_KEYS = frozenset(
    {
        "newClientOrderId",
        "newOrderRespType",
        "origClientOrderId",
        "positionSide",
        "price",
        "quantity",
        "recvWindow",
        "reduceOnly",
        "side",
        "symbol",
        "timeInForce",
        "type",
    }
)


def _expected_pre_intent_parameters(
    path: str,
    authority: SessionAuthority,
) -> dict[str, str]:
    recv_window = {"recvWindow": str(RECEIVE_WINDOW_MS)}
    expected: dict[str, dict[str, str]] = {
        "/fapi/v1/time": {},
        "/fapi/v1/positionSide/dual": recv_window,
        "/fapi/v1/symbolConfig": {"symbol": SYMBOL, **recv_window},
        "/fapi/v2/account": recv_window,
        "/fapi/v1/openOrders": recv_window,
        "/fapi/v1/openAlgoOrders": recv_window,
        "/fapi/v1/exchangeInfo": {},
        "/fapi/v1/order": {
            "symbol": SYMBOL,
            "origClientOrderId": authority.client_id,
            **recv_window,
        },
        "/fapi/v1/userTrades": {"symbol": SYMBOL, **recv_window},
        "/fapi/v1/ticker/bookTicker": {"symbol": SYMBOL},
        "/fapi/v1/premiumIndex": {"symbol": SYMBOL},
    }
    try:
        return expected[path]
    except KeyError as exc:
        raise ExecutionJournalError("PRE_INTENT_READ_PATH_NOT_ALLOWED") from exc


def _expected_intent_bound_recovery_parameters(
    path: str,
    authority: SessionAuthority,
) -> dict[str, str]:
    recv_window = {"recvWindow": str(RECEIVE_WINDOW_MS)}
    expected = {
        "/fapi/v1/order": {
            "symbol": SYMBOL,
            "origClientOrderId": authority.client_id,
            **recv_window,
        },
        "/fapi/v1/userTrades": {"symbol": SYMBOL, **recv_window},
        "/fapi/v2/account": recv_window,
        "/fapi/v1/openOrders": recv_window,
        "/fapi/v1/openAlgoOrders": recv_window,
    }
    try:
        return expected[path]
    except KeyError as exc:
        raise ExecutionJournalError("INTENT_BOUND_RECOVERY_READ_BINDING_MISMATCH") from exc


class ExecutionJournalError(ValueError):
    """Raised whenever journal safety or lifecycle reconstruction fails closed."""


class MutationKind(StrEnum):
    """The only mutation transports that may cross the worker boundary."""

    CREATE = "CREATE"
    CANCEL = "CANCEL"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


class MutationPurpose(StrEnum):
    """Exact economic purpose bound to one reserved mutation request."""

    PRIMARY_CREATE = "PRIMARY_CREATE"
    PRIMARY_CANCEL = "PRIMARY_CANCEL"
    PRIMARY_EMERGENCY_CLOSE = "PRIMARY_EMERGENCY_CLOSE"
    RECOVERY_CONDITIONAL_CANCEL = "RECOVERY_CONDITIONAL_CANCEL"
    RECOVERY_OWNED_FILL_CLOSE = "RECOVERY_OWNED_FILL_CLOSE"


class FrontierState(StrEnum):
    """Durable knowledge frontier for one mutation attempt."""

    PREPARED = "PREPARED"
    GO_DURABLE = "GO_DURABLE"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    NOT_DISPATCHED = "NOT_DISPATCHED"


class GenerationCapability(StrEnum):
    """Mutation authority granted to one exact worker generation."""

    PRIMARY = "PRIMARY"
    RECOVERY = "RECOVERY"


class RecoveryAuthorityTarget(StrEnum):
    """Discriminant for attempt-bound versus intent-only recovery authority."""

    ATTEMPT = "ATTEMPT"
    INTENT_BOUND_NO_ATTEMPT = "INTENT_BOUND_NO_ATTEMPT"


class BoundaryResult(StrEnum):
    """Sanitized supervisor observation of the worker result channel."""

    CONFIRMED = "CONFIRMED"
    ABSENT = "ABSENT"
    CORRUPT = "CORRUPT"
    EOF = "EOF"
    TIMEOUT = "TIMEOUT"
    KILLED = "KILLED"
    PARTIAL_WRITE = "PARTIAL_WRITE"
    RESPONSE_LOSS = "RESPONSE_LOSS"
    DECODE_FAILURE = "DECODE_FAILURE"
    RESULT_DURABILITY_FAILURE = "RESULT_DURABILITY_FAILURE"


class ReconciledOrderStatus(StrEnum):
    """Only fresh venue states that can authorize cleanup cancellation."""

    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


class ReadKind(StrEnum):
    """Fixed sanitized class of one ledger-reserved read."""

    ORDER = "ORDER"
    TRADE = "TRADE"
    ACCOUNT = "ACCOUNT"
    SYMBOL_FILTER = "SYMBOL_FILTER"
    MARK_PRICE = "MARK_PRICE"
    GENERAL = "GENERAL"


_INTENT_BOUND_RECOVERY_READ_KINDS = {
    "/fapi/v1/order": ReadKind.ORDER,
    "/fapi/v1/userTrades": ReadKind.TRADE,
    "/fapi/v2/account": ReadKind.ACCOUNT,
    "/fapi/v1/openOrders": ReadKind.GENERAL,
    "/fapi/v1/openAlgoOrders": ReadKind.GENERAL,
}


class ReadPurpose(StrEnum):
    """Why a ledger-reserved read is allowed to exist."""

    EVIDENCE = "EVIDENCE"
    ORDER_RECONCILIATION = "ORDER_RECONCILIATION"
    OWNED_FILL_CLOSE = "OWNED_FILL_CLOSE"


class ReadOutcome(StrEnum):
    """Sanitized typed read result; never a venue response blob."""

    ORDER_NEW = "ORDER_NEW"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_TERMINAL = "ORDER_TERMINAL"
    OWNED_ORDER_FILL_CONFIRMED = "OWNED_ORDER_FILL_CONFIRMED"
    OWNED_TRADE_FILL_CONFIRMED = "OWNED_TRADE_FILL_CONFIRMED"
    OWNED_ACCOUNT_POSITION_CONFIRMED = "OWNED_ACCOUNT_POSITION_CONFIRMED"
    FILTER_SNAPSHOT_CONFIRMED = "FILTER_SNAPSHOT_CONFIRMED"
    MARK_PRICE_CONFIRMED = "MARK_PRICE_CONFIRMED"
    SUCCESS = "SUCCESS"
    NEGATIVE = "NEGATIVE"


class ReadFailureKind(StrEnum):
    """Sanitized read-boundary failure; no response or credential material."""

    TIMEOUT = "TIMEOUT"
    EOF = "EOF"
    CORRUPT = "CORRUPT"
    TRUNCATED = "TRUNCATED"
    OVERSIZED = "OVERSIZED"
    VERSION = "VERSION"
    SEQUENCE = "SEQUENCE"
    DIGEST = "DIGEST"
    PARSE = "PARSE"
    IO_AMBIGUOUS = "IO_AMBIGUOUS"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    NETWORK_GUARD = "NETWORK_GUARD"
    TRANSPORT_REJECTED = "TRANSPORT_REJECTED"
    EXECUTOR_FAILURE = "EXECUTOR_FAILURE"
    RESULT_INVALID = "RESULT_INVALID"


def project_read_failure_kind(value: str) -> ReadFailureKind:
    """Project a child failure enum value into the journal's exact allowlist."""

    if type(value) is not str:
        raise ExecutionJournalError("READ_FAILURE_KIND_NOT_ALLOWED")
    try:
        return ReadFailureKind(value)
    except ValueError as exc:
        raise ExecutionJournalError("READ_FAILURE_KIND_NOT_ALLOWED") from exc


class ReconciliationKeyKind(StrEnum):
    """Kind-specific venue lookup key; values are never request payloads."""

    PROBE_CLIENT_ID = "PROBE_CLIENT_ID"
    PROBE_TERMINAL_STATE = "PROBE_TERMINAL_STATE"
    EMERGENCY_CLOSE_CLIENT_ID = "EMERGENCY_CLOSE_CLIENT_ID"


class RecoveryMode(StrEnum):
    """Read-first recovery action for an UNKNOWN mutation."""

    QUERY_PROBE_CLIENT_ID = "QUERY_PROBE_CLIENT_ID"
    QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL = "QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL"
    QUERY_CLOSE_ID_AND_FRESH_STATE = "QUERY_CLOSE_ID_AND_FRESH_STATE"


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionJournalError("JOURNAL_VALUE_NOT_CANONICAL") from exc


def reserved_request_parameters_sha256(reserved: ReservedRequest) -> str:
    """Canonical digest of the exact protocol reservation parameters."""

    if type(reserved) is not ReservedRequest:
        raise ExecutionJournalError("INVALID_RESERVED_REQUEST")
    return hashlib.sha256(_canonical_json(dict(reserved.parameters))).hexdigest()


def reserved_request_ledger_sha256(reserved: ReservedRequest) -> str:
    """Canonical digest of the exact protocol ledger snapshot."""

    if type(reserved) is not ReservedRequest:
        raise ExecutionJournalError("INVALID_RESERVED_REQUEST")
    ledger = reserved.ledger
    material = {
        "cancel_requests": ledger.cancel_requests,
        "create_requests": ledger.create_requests,
        "emergency_close_requests": ledger.emergency_close_requests,
        "last_elapsed_seconds": format(ledger.last_elapsed_seconds, "f"),
        "post_create_read_requests": ledger.post_create_read_requests,
        "read_retry_requests": ledger.read_retry_requests,
        "retryable_read_sha256": ledger.retryable_read_sha256,
        "stage": ledger.stage.value,
        "total_http_requests": ledger.total_http_requests,
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _parse_canonical_decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise ExecutionJournalError("INVALID_CANONICAL_DECIMAL")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ExecutionJournalError("INVALID_CANONICAL_DECIMAL") from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise ExecutionJournalError("INVALID_CANONICAL_DECIMAL")
    return parsed


def _ledger_material(ledger: MutationLedger) -> dict[str, object]:
    if type(ledger) is not MutationLedger:
        raise ExecutionJournalError("INVALID_REQUEST_LEDGER")
    return {
        "cancel_requests": ledger.cancel_requests,
        "create_requests": ledger.create_requests,
        "emergency_close_requests": ledger.emergency_close_requests,
        "last_elapsed_seconds": format(ledger.last_elapsed_seconds, "f"),
        "post_create_read_requests": ledger.post_create_read_requests,
        "read_retry_requests": ledger.read_retry_requests,
        "retryable_read_sha256": ledger.retryable_read_sha256,
        "stage": ledger.stage.value,
        "total_http_requests": ledger.total_http_requests,
    }


def _ledger_sha256(ledger: MutationLedger) -> str:
    return hashlib.sha256(_canonical_json(_ledger_material(ledger))).hexdigest()


def _session_authority_material(
    *,
    authorization_id: str,
    runtime_commit: str,
    session_nonce: str,
    generation: int,
    client_id: str,
) -> dict[str, object]:
    return {
        "authority_schema": _SESSION_AUTHORITY_SCHEMA,
        "authorization_id": authorization_id,
        "runtime_commit": runtime_commit,
        "session_nonce": session_nonce,
        "generation": generation,
        "client_id": client_id,
    }


@dataclass(frozen=True, slots=True)
class SessionAuthority:
    """Non-secret authority for reads that must precede final intent derivation."""

    authority_sha256: str
    authorization_id: str
    runtime_commit: str
    session_nonce: str
    generation: int
    client_id: str

    def __post_init__(self) -> None:
        try:
            expected_client_id = build_client_order_id(
                self.runtime_commit,
                self.session_nonce,
            )
        except ValueError as exc:
            raise ExecutionJournalError("INVALID_SESSION_AUTHORITY") from exc
        material = _session_authority_material(
            authorization_id=self.authorization_id,
            runtime_commit=self.runtime_commit,
            session_nonce=self.session_nonce,
            generation=self.generation,
            client_id=self.client_id,
        )
        if (
            type(self.authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
            or type(self.runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.runtime_commit) is None
            or type(self.session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.session_nonce) is None
            or not _is_positive_int(self.generation)
            or self.client_id != expected_client_id
            or self.authority_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_SESSION_AUTHORITY")

    @classmethod
    def build(
        cls,
        *,
        authorization_id: str,
        runtime_commit: str,
        session_nonce: str,
        generation: int,
    ) -> SessionAuthority:
        try:
            client_id = build_client_order_id(runtime_commit, session_nonce)
        except ValueError as exc:
            raise ExecutionJournalError("INVALID_SESSION_AUTHORITY") from exc
        material = _session_authority_material(
            authorization_id=authorization_id,
            runtime_commit=runtime_commit,
            session_nonce=session_nonce,
            generation=generation,
            client_id=client_id,
        )
        return cls(
            authority_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            authorization_id=authorization_id,
            runtime_commit=runtime_commit,
            session_nonce=session_nonce,
            generation=generation,
            client_id=client_id,
        )


def _recovery_session_authority_material(
    *,
    primary_authority_sha256: str,
    source_attempt_id: str,
    source_generation: int,
    source_kind: MutationKind,
    source_authorization_id: str,
    source_intent_sha256: str,
    source_runtime_commit: str,
    source_session_nonce: str,
    source_client_id: str,
    generation: int,
) -> dict[str, object]:
    return {
        "authority_schema": _RECOVERY_SESSION_AUTHORITY_SCHEMA,
        "primary_authority_sha256": primary_authority_sha256,
        "source_attempt_id": source_attempt_id,
        "source_generation": source_generation,
        "source_kind": source_kind.value,
        "source_authorization_id": source_authorization_id,
        "source_intent_sha256": source_intent_sha256,
        "source_runtime_commit": source_runtime_commit,
        "source_session_nonce": source_session_nonce,
        "source_client_id": source_client_id,
        "generation": generation,
    }


@dataclass(frozen=True, slots=True)
class RecoverySessionAuthority:
    """Journal-issued, read/cleanup-only authority for one recovery generation."""

    authority_sha256: str
    primary_authority_sha256: str
    source_attempt_id: str
    source_generation: int
    source_kind: MutationKind
    source_authorization_id: str
    source_intent_sha256: str
    source_runtime_commit: str
    source_session_nonce: str
    source_client_id: str
    generation: int

    @property
    def target(self) -> RecoveryAuthorityTarget:
        return RecoveryAuthorityTarget.ATTEMPT

    def __post_init__(self) -> None:
        material = _recovery_session_authority_material(
            primary_authority_sha256=self.primary_authority_sha256,
            source_attempt_id=self.source_attempt_id,
            source_generation=self.source_generation,
            source_kind=self.source_kind,
            source_authorization_id=self.source_authorization_id,
            source_intent_sha256=self.source_intent_sha256,
            source_runtime_commit=self.source_runtime_commit,
            source_session_nonce=self.source_session_nonce,
            source_client_id=self.source_client_id,
            generation=self.generation,
        )
        if (
            not _is_sha256(self.primary_authority_sha256)
            or not _is_sha256(self.source_attempt_id)
            or not _is_positive_int(self.source_generation)
            or type(self.source_kind) is not MutationKind
            or type(self.source_authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.source_authorization_id) is None
            or not _is_sha256(self.source_intent_sha256)
            or type(self.source_runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_runtime_commit) is None
            or type(self.source_session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.source_session_nonce) is None
            or type(self.source_client_id) is not str
            or _CLIENT_ID.fullmatch(self.source_client_id) is None
            or not _is_positive_int(self.generation)
            or self.generation <= self.source_generation
            or self.authority_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_RECOVERY_SESSION_AUTHORITY")

    @classmethod
    def build(
        cls,
        *,
        primary_authority_sha256: str,
        source_attempt: MutationAttempt,
        generation: int,
    ) -> RecoverySessionAuthority:
        if type(source_attempt) is not MutationAttempt:
            raise ExecutionJournalError("INVALID_RECOVERY_SESSION_AUTHORITY")
        material = _recovery_session_authority_material(
            primary_authority_sha256=primary_authority_sha256,
            source_attempt_id=source_attempt.attempt_id,
            source_generation=source_attempt.generation,
            source_kind=source_attempt.kind,
            source_authorization_id=source_attempt.authorization_id,
            source_intent_sha256=source_attempt.intent_sha256,
            source_runtime_commit=source_attempt.runtime_commit,
            source_session_nonce=source_attempt.session_nonce,
            source_client_id=source_attempt.client_id,
            generation=generation,
        )
        return cls(
            authority_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            primary_authority_sha256=primary_authority_sha256,
            source_attempt_id=source_attempt.attempt_id,
            source_generation=source_attempt.generation,
            source_kind=source_attempt.kind,
            source_authorization_id=source_attempt.authorization_id,
            source_intent_sha256=source_attempt.intent_sha256,
            source_runtime_commit=source_attempt.runtime_commit,
            source_session_nonce=source_attempt.session_nonce,
            source_client_id=source_attempt.client_id,
            generation=generation,
        )


def _intent_bound_recovery_authority_material(
    *,
    primary_authority_sha256: str,
    intent_binding_sha256: str,
    source_generation: int,
    source_authorization_id: str,
    source_intent_sha256: str,
    source_runtime_commit: str,
    source_session_nonce: str,
    query_client_id: str,
    generation: int,
    abandoned_create_request_sha256: str | None,
) -> dict[str, object]:
    return {
        "authority_schema": _INTENT_BOUND_RECOVERY_AUTHORITY_SCHEMA,
        "target": RecoveryAuthorityTarget.INTENT_BOUND_NO_ATTEMPT.value,
        "primary_authority_sha256": primary_authority_sha256,
        "intent_binding_sha256": intent_binding_sha256,
        "source_generation": source_generation,
        "source_authorization_id": source_authorization_id,
        "source_intent_sha256": source_intent_sha256,
        "source_runtime_commit": source_runtime_commit,
        "source_session_nonce": source_session_nonce,
        "query_client_id": query_client_id,
        "generation": generation,
        "abandoned_create_request_sha256": abandoned_create_request_sha256,
    }


@dataclass(frozen=True, slots=True)
class IntentBoundRecoveryAuthority:
    """Read-only recovery authority for a durable intent with no attempt."""

    authority_sha256: str
    primary_authority_sha256: str
    intent_binding_sha256: str
    source_generation: int
    source_authorization_id: str
    source_intent_sha256: str
    source_runtime_commit: str
    source_session_nonce: str
    query_client_id: str
    generation: int
    abandoned_create_request_sha256: str | None

    @property
    def target(self) -> RecoveryAuthorityTarget:
        return RecoveryAuthorityTarget.INTENT_BOUND_NO_ATTEMPT

    @property
    def allows_create(self) -> bool:
        return False

    @property
    def allows_mutation(self) -> bool:
        return False

    def __post_init__(self) -> None:
        material = _intent_bound_recovery_authority_material(
            primary_authority_sha256=self.primary_authority_sha256,
            intent_binding_sha256=self.intent_binding_sha256,
            source_generation=self.source_generation,
            source_authorization_id=self.source_authorization_id,
            source_intent_sha256=self.source_intent_sha256,
            source_runtime_commit=self.source_runtime_commit,
            source_session_nonce=self.source_session_nonce,
            query_client_id=self.query_client_id,
            generation=self.generation,
            abandoned_create_request_sha256=(self.abandoned_create_request_sha256),
        )
        try:
            expected_client_id = build_client_order_id(
                self.source_runtime_commit,
                self.source_session_nonce,
            )
        except ValueError as exc:
            raise ExecutionJournalError("INVALID_INTENT_BOUND_RECOVERY_AUTHORITY") from exc
        if (
            not _is_sha256(self.primary_authority_sha256)
            or not _is_sha256(self.intent_binding_sha256)
            or not _is_positive_int(self.source_generation)
            or type(self.source_authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.source_authorization_id) is None
            or not _is_sha256(self.source_intent_sha256)
            or type(self.source_runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_runtime_commit) is None
            or type(self.source_session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.source_session_nonce) is None
            or self.query_client_id != expected_client_id
            or not _is_positive_int(self.generation)
            or self.generation <= self.source_generation
            or (
                self.abandoned_create_request_sha256 is not None
                and not _is_sha256(self.abandoned_create_request_sha256)
            )
            or self.authority_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_INTENT_BOUND_RECOVERY_AUTHORITY")

    @classmethod
    def build(
        cls,
        *,
        primary_authority: SessionAuthority,
        intent_binding: IntentChainBinding,
        generation: int,
        abandoned_create_request_sha256: str | None,
    ) -> IntentBoundRecoveryAuthority:
        if (
            type(primary_authority) is not SessionAuthority
            or type(intent_binding) is not IntentChainBinding
        ):
            raise ExecutionJournalError("INVALID_INTENT_BOUND_RECOVERY_AUTHORITY")
        material = _intent_bound_recovery_authority_material(
            primary_authority_sha256=primary_authority.authority_sha256,
            intent_binding_sha256=intent_binding.binding_sha256,
            source_generation=primary_authority.generation,
            source_authorization_id=primary_authority.authorization_id,
            source_intent_sha256=intent_binding.intent_sha256,
            source_runtime_commit=primary_authority.runtime_commit,
            source_session_nonce=primary_authority.session_nonce,
            query_client_id=primary_authority.client_id,
            generation=generation,
            abandoned_create_request_sha256=abandoned_create_request_sha256,
        )
        return cls(
            authority_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            primary_authority_sha256=primary_authority.authority_sha256,
            intent_binding_sha256=intent_binding.binding_sha256,
            source_generation=primary_authority.generation,
            source_authorization_id=primary_authority.authorization_id,
            source_intent_sha256=intent_binding.intent_sha256,
            source_runtime_commit=primary_authority.runtime_commit,
            source_session_nonce=primary_authority.session_nonce,
            query_client_id=primary_authority.client_id,
            generation=generation,
            abandoned_create_request_sha256=abandoned_create_request_sha256,
        )


def _pre_intent_logical_material(
    *,
    session_authority_sha256: str,
    origin: str,
    method: str,
    path: str,
    purpose: RequestPurpose,
    parameters: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    return {
        "session_authority_sha256": session_authority_sha256,
        "origin": origin,
        "method": method,
        "path": path,
        "purpose": purpose.value,
        "parameters": dict(parameters),
    }


def _pre_intent_reservation_material(
    *,
    logical_request_sha256: str,
    session_authority_sha256: str,
    generation: int,
    deadline_ns: int,
    origin: str,
    method: str,
    path: str,
    purpose: RequestPurpose,
    parameters: tuple[tuple[str, str], ...],
    ledger: MutationLedger,
    elapsed_seconds: Decimal,
    retry_index: int,
) -> dict[str, object]:
    return {
        "reservation_schema": _PRE_INTENT_READ_SCHEMA,
        "logical_request_sha256": logical_request_sha256,
        "session_authority_sha256": session_authority_sha256,
        "generation": generation,
        "deadline_ns": deadline_ns,
        "origin": origin,
        "method": method,
        "path": path,
        "purpose": purpose.value,
        "parameters": dict(parameters),
        "ledger": _ledger_material(ledger),
        "elapsed_seconds": format(elapsed_seconds, "f"),
        "retry_index": retry_index,
    }


@dataclass(frozen=True, slots=True)
class PreIntentReadReservation:
    """Exact pre-intent GET reservation bound to session authority, not intent."""

    reservation_sha256: str
    logical_request_sha256: str
    session_authority_sha256: str
    generation: int
    deadline_ns: int
    origin: str
    method: str
    path: str
    purpose: RequestPurpose
    parameters: tuple[tuple[str, str], ...]
    ledger: MutationLedger
    elapsed_seconds: Decimal
    retry_index: int

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.session_authority_sha256)
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.deadline_ns)
            or self.origin != DEMO_HTTP_ORIGIN
            or self.method != "GET"
            or self.path not in _PRE_INTENT_READ_PATHS
            or self.purpose is not RequestPurpose.READ
            or type(self.parameters) is not tuple
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not str
                or type(pair[1]) is not str
                for pair in self.parameters
            )
            or tuple(sorted(self.parameters)) != self.parameters
            or len(dict(self.parameters)) != len(self.parameters)
            or type(self.ledger) is not MutationLedger
            or type(self.elapsed_seconds) is not Decimal
            or not self.elapsed_seconds.is_finite()
            or self.elapsed_seconds != self.ledger.last_elapsed_seconds
            or self.retry_index not in {0, 1}
        ):
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_RESERVATION")
        logical_material = _pre_intent_logical_material(
            session_authority_sha256=self.session_authority_sha256,
            origin=self.origin,
            method=self.method,
            path=self.path,
            purpose=self.purpose,
            parameters=self.parameters,
        )
        expected_logical = hashlib.sha256(_canonical_json(logical_material)).hexdigest()
        material = _pre_intent_reservation_material(
            logical_request_sha256=self.logical_request_sha256,
            session_authority_sha256=self.session_authority_sha256,
            generation=self.generation,
            deadline_ns=self.deadline_ns,
            origin=self.origin,
            method=self.method,
            path=self.path,
            purpose=self.purpose,
            parameters=self.parameters,
            ledger=self.ledger,
            elapsed_seconds=self.elapsed_seconds,
            retry_index=self.retry_index,
        )
        if (
            self.logical_request_sha256 != expected_logical
            or self.reservation_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("PRE_INTENT_READ_RESERVATION_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        session_authority_sha256: str,
        generation: int,
        deadline_ns: int,
        path: str,
        parameters: Mapping[str, object],
        ledger: MutationLedger,
        elapsed_seconds: Decimal,
        retry_index: int,
    ) -> PreIntentReadReservation:
        normalized = tuple(sorted((str(key), str(value)) for key, value in parameters.items()))
        logical_material = _pre_intent_logical_material(
            session_authority_sha256=session_authority_sha256,
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=normalized,
        )
        logical = hashlib.sha256(_canonical_json(logical_material)).hexdigest()
        material = _pre_intent_reservation_material(
            logical_request_sha256=logical,
            session_authority_sha256=session_authority_sha256,
            generation=generation,
            deadline_ns=deadline_ns,
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=normalized,
            ledger=ledger,
            elapsed_seconds=elapsed_seconds,
            retry_index=retry_index,
        )
        return cls(
            reservation_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            logical_request_sha256=logical,
            session_authority_sha256=session_authority_sha256,
            generation=generation,
            deadline_ns=deadline_ns,
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=normalized,
            ledger=ledger,
            elapsed_seconds=elapsed_seconds,
            retry_index=retry_index,
        )


@dataclass(frozen=True, slots=True)
class PreparedPreIntentRead:
    reservation: PreIntentReadReservation
    record_sequence: int
    record_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.reservation) is not PreIntentReadReservation
            or not _is_positive_int(self.record_sequence)
            or not _is_sha256(self.record_digest)
        ):
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_RECEIPT")


def _pre_intent_result_material(
    *,
    reservation_sha256: str,
    prepared_record_sequence: int,
    prepared_record_digest: str,
    result_sha256: str,
    observed_at_ns: int,
) -> dict[str, object]:
    return {
        "result_schema": _PRE_INTENT_RESULT_SCHEMA,
        "reservation_sha256": reservation_sha256,
        "prepared_record_sequence": prepared_record_sequence,
        "prepared_record_digest": prepared_record_digest,
        "result_sha256": result_sha256,
        "observed_at_ns": observed_at_ns,
    }


@dataclass(frozen=True, slots=True)
class PreIntentReadResult:
    result_proof_sha256: str
    reservation_sha256: str
    prepared_record_sequence: int
    prepared_record_digest: str
    result_sha256: str
    observed_at_ns: int

    def __post_init__(self) -> None:
        material = _pre_intent_result_material(
            reservation_sha256=self.reservation_sha256,
            prepared_record_sequence=self.prepared_record_sequence,
            prepared_record_digest=self.prepared_record_digest,
            result_sha256=self.result_sha256,
            observed_at_ns=self.observed_at_ns,
        )
        if (
            not _is_sha256(self.reservation_sha256)
            or not _is_positive_int(self.prepared_record_sequence)
            or not _is_sha256(self.prepared_record_digest)
            or not _is_sha256(self.result_sha256)
            or not _is_positive_int(self.observed_at_ns)
            or self.result_proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_RESULT")

    @classmethod
    def build(
        cls,
        *,
        reservation_sha256: str,
        prepared_record_sequence: int,
        prepared_record_digest: str,
        result_sha256: str,
        observed_at_ns: int,
    ) -> PreIntentReadResult:
        material = _pre_intent_result_material(
            reservation_sha256=reservation_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            result_sha256=result_sha256,
            observed_at_ns=observed_at_ns,
        )
        return cls(
            result_proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            reservation_sha256=reservation_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            result_sha256=result_sha256,
            observed_at_ns=observed_at_ns,
        )


def _pre_intent_failure_material(
    *,
    reservation_sha256: str,
    prepared_record_sequence: int,
    prepared_record_digest: str,
    failure: ReadFailureKind,
    io_may_have_occurred: bool,
    observed_at_ns: int,
) -> dict[str, object]:
    return {
        "failure_schema": _PRE_INTENT_FAILURE_SCHEMA,
        "reservation_sha256": reservation_sha256,
        "prepared_record_sequence": prepared_record_sequence,
        "prepared_record_digest": prepared_record_digest,
        "failure": failure.value,
        "io_may_have_occurred": io_may_have_occurred,
        "observed_at_ns": observed_at_ns,
    }


@dataclass(frozen=True, slots=True)
class PreIntentReadFailure:
    failure_proof_sha256: str
    reservation_sha256: str
    prepared_record_sequence: int
    prepared_record_digest: str
    failure: ReadFailureKind
    io_may_have_occurred: bool
    observed_at_ns: int

    def __post_init__(self) -> None:
        if type(self.failure) is not ReadFailureKind:
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_FAILURE")
        material = _pre_intent_failure_material(
            reservation_sha256=self.reservation_sha256,
            prepared_record_sequence=self.prepared_record_sequence,
            prepared_record_digest=self.prepared_record_digest,
            failure=self.failure,
            io_may_have_occurred=self.io_may_have_occurred,
            observed_at_ns=self.observed_at_ns,
        )
        if (
            not _is_sha256(self.reservation_sha256)
            or not _is_positive_int(self.prepared_record_sequence)
            or not _is_sha256(self.prepared_record_digest)
            or type(self.io_may_have_occurred) is not bool
            or not _is_positive_int(self.observed_at_ns)
            or self.failure_proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_FAILURE")

    @classmethod
    def build(
        cls,
        *,
        reservation_sha256: str,
        prepared_record_sequence: int,
        prepared_record_digest: str,
        failure: ReadFailureKind,
        io_may_have_occurred: bool,
        observed_at_ns: int,
    ) -> PreIntentReadFailure:
        if type(failure) is not ReadFailureKind:
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_FAILURE")
        material = _pre_intent_failure_material(
            reservation_sha256=reservation_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            failure=failure,
            io_may_have_occurred=io_may_have_occurred,
            observed_at_ns=observed_at_ns,
        )
        return cls(
            failure_proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            reservation_sha256=reservation_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            failure=failure,
            io_may_have_occurred=io_may_have_occurred,
            observed_at_ns=observed_at_ns,
        )


def _exact_read_failure_material(
    *,
    request_sha256: str,
    read_proof_sha256: str,
    prepared_record_sequence: int,
    prepared_record_digest: str,
    generation: int,
    monotonic_sequence: int,
    failure: ReadFailureKind,
    io_may_have_occurred: bool,
    observed_at_ns: int,
) -> dict[str, object]:
    return {
        "failure_schema": _EXACT_READ_FAILURE_SCHEMA,
        "request_sha256": request_sha256,
        "read_proof_sha256": read_proof_sha256,
        "prepared_record_sequence": prepared_record_sequence,
        "prepared_record_digest": prepared_record_digest,
        "generation": generation,
        "monotonic_sequence": monotonic_sequence,
        "failure": failure.value,
        "io_may_have_occurred": io_may_have_occurred,
        "observed_at_ns": observed_at_ns,
    }


@dataclass(frozen=True, slots=True)
class ExactReadFailure:
    """Durable typed failure of one exact post-intent GET reservation."""

    failure_proof_sha256: str
    request_sha256: str
    read_proof_sha256: str
    prepared_record_sequence: int
    prepared_record_digest: str
    generation: int
    monotonic_sequence: int
    failure: ReadFailureKind
    io_may_have_occurred: bool
    observed_at_ns: int

    def __post_init__(self) -> None:
        if type(self.failure) is not ReadFailureKind:
            raise ExecutionJournalError("INVALID_EXACT_READ_FAILURE")
        material = _exact_read_failure_material(
            request_sha256=self.request_sha256,
            read_proof_sha256=self.read_proof_sha256,
            prepared_record_sequence=self.prepared_record_sequence,
            prepared_record_digest=self.prepared_record_digest,
            generation=self.generation,
            monotonic_sequence=self.monotonic_sequence,
            failure=self.failure,
            io_may_have_occurred=self.io_may_have_occurred,
            observed_at_ns=self.observed_at_ns,
        )
        if (
            not _is_sha256(self.request_sha256)
            or not _is_sha256(self.read_proof_sha256)
            or not _is_positive_int(self.prepared_record_sequence)
            or not _is_sha256(self.prepared_record_digest)
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.monotonic_sequence)
            or type(self.io_may_have_occurred) is not bool
            or not _is_positive_int(self.observed_at_ns)
            or self.failure_proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_EXACT_READ_FAILURE")

    @classmethod
    def build(
        cls,
        *,
        request_sha256: str,
        read_proof_sha256: str,
        prepared_record_sequence: int,
        prepared_record_digest: str,
        generation: int,
        monotonic_sequence: int,
        failure: ReadFailureKind,
        io_may_have_occurred: bool,
        observed_at_ns: int,
    ) -> ExactReadFailure:
        if type(failure) is not ReadFailureKind:
            raise ExecutionJournalError("INVALID_EXACT_READ_FAILURE")
        material = _exact_read_failure_material(
            request_sha256=request_sha256,
            read_proof_sha256=read_proof_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            generation=generation,
            monotonic_sequence=monotonic_sequence,
            failure=failure,
            io_may_have_occurred=io_may_have_occurred,
            observed_at_ns=observed_at_ns,
        )
        return cls(
            failure_proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            request_sha256=request_sha256,
            read_proof_sha256=read_proof_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            generation=generation,
            monotonic_sequence=monotonic_sequence,
            failure=failure,
            io_may_have_occurred=io_may_have_occurred,
            observed_at_ns=observed_at_ns,
        )


@dataclass(frozen=True, slots=True)
class IntentChainBinding:
    binding_sha256: str
    session_authority_sha256: str
    intent_sha256: str
    intent_file_sha256: str
    intent_path_sha256: str
    pre_intent_chain_sha256: str
    last_ledger_sha256: str

    def __post_init__(self) -> None:
        material = {
            "binding_schema": _INTENT_CHAIN_BINDING_SCHEMA,
            "session_authority_sha256": self.session_authority_sha256,
            "intent_sha256": self.intent_sha256,
            "intent_file_sha256": self.intent_file_sha256,
            "intent_path_sha256": self.intent_path_sha256,
            "pre_intent_chain_sha256": self.pre_intent_chain_sha256,
            "last_ledger_sha256": self.last_ledger_sha256,
        }
        if (
            any(
                not _is_sha256(value)
                for value in (
                    self.session_authority_sha256,
                    self.intent_sha256,
                    self.intent_file_sha256,
                    self.intent_path_sha256,
                    self.pre_intent_chain_sha256,
                    self.last_ledger_sha256,
                )
            )
            or self.binding_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest()
        ):
            raise ExecutionJournalError("INVALID_INTENT_CHAIN_BINDING")

    @classmethod
    def build(
        cls,
        *,
        session_authority_sha256: str,
        intent_sha256: str,
        intent_file_sha256: str,
        intent_path_sha256: str,
        pre_intent_chain_sha256: str,
        last_ledger_sha256: str,
    ) -> IntentChainBinding:
        material = {
            "binding_schema": _INTENT_CHAIN_BINDING_SCHEMA,
            "session_authority_sha256": session_authority_sha256,
            "intent_sha256": intent_sha256,
            "intent_file_sha256": intent_file_sha256,
            "intent_path_sha256": intent_path_sha256,
            "pre_intent_chain_sha256": pre_intent_chain_sha256,
            "last_ledger_sha256": last_ledger_sha256,
        }
        return cls(
            binding_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            session_authority_sha256=session_authority_sha256,
            intent_sha256=intent_sha256,
            intent_file_sha256=intent_file_sha256,
            intent_path_sha256=intent_path_sha256,
            pre_intent_chain_sha256=pre_intent_chain_sha256,
            last_ledger_sha256=last_ledger_sha256,
        )


@dataclass(frozen=True, slots=True)
class ExactRequestReservation:
    authority_sha256: str
    generation: int
    deadline_ns: int
    reserved_request: ReservedRequest

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.authority_sha256)
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.deadline_ns)
            or type(self.reserved_request) is not ReservedRequest
        ):
            raise ExecutionJournalError("INVALID_EXACT_REQUEST_RESERVATION")


@dataclass(frozen=True, slots=True)
class RequestLedgerSnapshot:
    authority: SessionAuthority
    bound_intent_sha256: str | None
    last_ledger: MutationLedger
    pending_pre_intent_reads: tuple[PreIntentReadReservation, ...]
    pending_requests: tuple[ReservedRequest, ...]
    completed_pre_intent_paths: tuple[str, ...]
    retryable_logical_request_sha256: str | None
    exact_reserved_requests: tuple[ReservedRequest, ...]


def owned_close_parameters_sha256(*, quantity: str, client_id: str) -> str:
    """Digest the sole fixed emergency-close parameter set."""

    parsed_quantity = _parse_canonical_decimal(quantity)
    if (
        parsed_quantity <= 0
        or type(client_id) is not str
        or _CLIENT_ID.fullmatch(client_id) is None
    ):
        raise ExecutionJournalError("INVALID_OWNED_CLOSE_PARAMETERS")
    parameters = {
        "symbol": SYMBOL,
        "side": "SELL",
        "type": "MARKET",
        "quantity": quantity,
        "positionSide": "BOTH",
        "reduceOnly": "true",
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
        "recvWindow": str(RECEIVE_WINDOW_MS),
    }
    return hashlib.sha256(_canonical_json(parameters)).hexdigest()


def _mutation_reservation_material(
    *,
    request_sha256: str,
    logical_request_sha256: str,
    kind: MutationKind,
    purpose: MutationPurpose,
    method: str,
    path: str,
    retry_index: int,
    client_id: str,
    authorization_id: str,
    intent_sha256: str,
    generation: int,
    deadline_ns: int,
    monotonic_sequence: int,
    parameters_sha256: str,
    ledger_sha256: str,
    source_attempt_id: str | None,
    precondition_sha256: str | None,
) -> dict[str, object]:
    return {
        "reservation_schema": _MUTATION_RESERVATION_SCHEMA,
        "request_sha256": request_sha256,
        "logical_request_sha256": logical_request_sha256,
        "kind": kind.value,
        "purpose": purpose.value,
        "method": method,
        "path": path,
        "retry_index": retry_index,
        "client_id": client_id,
        "authorization_id": authorization_id,
        "intent_sha256": intent_sha256,
        "generation": generation,
        "deadline_ns": deadline_ns,
        "monotonic_sequence": monotonic_sequence,
        "parameters_sha256": parameters_sha256,
        "ledger_sha256": ledger_sha256,
        "source_attempt_id": source_attempt_id,
        "precondition_sha256": precondition_sha256,
    }


@dataclass(frozen=True, slots=True)
class MutationReservationProof:
    """Digest of one exact, payload-free request reserved before PREPARED."""

    request_sha256: str
    proof_sha256: str
    logical_request_sha256: str
    kind: MutationKind
    purpose: MutationPurpose
    method: str
    path: str
    retry_index: int
    client_id: str
    authorization_id: str
    intent_sha256: str
    generation: int
    deadline_ns: int
    monotonic_sequence: int
    parameters_sha256: str
    ledger_sha256: str
    source_attempt_id: str | None
    precondition_sha256: str | None

    def __post_init__(self) -> None:
        expected_kind = {
            MutationPurpose.PRIMARY_CREATE: MutationKind.CREATE,
            MutationPurpose.PRIMARY_CANCEL: MutationKind.CANCEL,
            MutationPurpose.PRIMARY_EMERGENCY_CLOSE: MutationKind.EMERGENCY_CLOSE,
            MutationPurpose.RECOVERY_CONDITIONAL_CANCEL: MutationKind.CANCEL,
            MutationPurpose.RECOVERY_OWNED_FILL_CLOSE: MutationKind.EMERGENCY_CLOSE,
        }
        expected_method = {
            MutationKind.CREATE: "POST",
            MutationKind.CANCEL: "DELETE",
            MutationKind.EMERGENCY_CLOSE: "POST",
        }
        requires_precondition = self.kind is not MutationKind.CREATE
        if (
            not _is_sha256(self.request_sha256)
            or not _is_sha256(self.logical_request_sha256)
            or type(self.kind) is not MutationKind
            or type(self.purpose) is not MutationPurpose
            or expected_kind[self.purpose] is not self.kind
            or type(self.method) is not str
            or self.method != expected_method[self.kind]
            or type(self.path) is not str
            or _REQUEST_PATH.fullmatch(self.path) is None
            or self.retry_index != 0
            or type(self.client_id) is not str
            or _CLIENT_ID.fullmatch(self.client_id) is None
            or type(self.authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
            or not _is_sha256(self.intent_sha256)
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.deadline_ns)
            or not _is_positive_int(self.monotonic_sequence)
            or not _is_sha256(self.parameters_sha256)
            or not _is_sha256(self.ledger_sha256)
            or (
                requires_precondition
                and (
                    not _is_sha256(self.source_attempt_id)
                    or not _is_sha256(self.precondition_sha256)
                )
            )
            or (
                not requires_precondition
                and (self.source_attempt_id is not None or self.precondition_sha256 is not None)
            )
        ):
            raise ExecutionJournalError("INVALID_MUTATION_RESERVATION")
        material = _mutation_reservation_material(
            request_sha256=self.request_sha256,
            logical_request_sha256=self.logical_request_sha256,
            kind=self.kind,
            purpose=self.purpose,
            method=self.method,
            path=self.path,
            retry_index=self.retry_index,
            client_id=self.client_id,
            authorization_id=self.authorization_id,
            intent_sha256=self.intent_sha256,
            generation=self.generation,
            deadline_ns=self.deadline_ns,
            monotonic_sequence=self.monotonic_sequence,
            parameters_sha256=self.parameters_sha256,
            ledger_sha256=self.ledger_sha256,
            source_attempt_id=self.source_attempt_id,
            precondition_sha256=self.precondition_sha256,
        )
        expected = hashlib.sha256(_canonical_json(material)).hexdigest()
        if self.proof_sha256 != expected:
            raise ExecutionJournalError("MUTATION_RESERVATION_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        request_sha256: str,
        logical_request_sha256: str,
        kind: MutationKind,
        purpose: MutationPurpose,
        method: str,
        path: str,
        retry_index: int,
        client_id: str,
        authorization_id: str,
        intent_sha256: str,
        generation: int,
        deadline_ns: int,
        monotonic_sequence: int,
        parameters_sha256: str,
        ledger_sha256: str,
        source_attempt_id: str | None,
        precondition_sha256: str | None,
    ) -> MutationReservationProof:
        if type(kind) is not MutationKind or type(purpose) is not MutationPurpose:
            raise ExecutionJournalError("INVALID_MUTATION_RESERVATION")
        material = _mutation_reservation_material(
            request_sha256=request_sha256,
            logical_request_sha256=logical_request_sha256,
            kind=kind,
            purpose=purpose,
            method=method,
            path=path,
            retry_index=retry_index,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=monotonic_sequence,
            parameters_sha256=parameters_sha256,
            ledger_sha256=ledger_sha256,
            source_attempt_id=source_attempt_id,
            precondition_sha256=precondition_sha256,
        )
        return cls(
            request_sha256=request_sha256,
            proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            logical_request_sha256=logical_request_sha256,
            kind=kind,
            purpose=purpose,
            method=method,
            path=path,
            retry_index=retry_index,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=monotonic_sequence,
            parameters_sha256=parameters_sha256,
            ledger_sha256=ledger_sha256,
            source_attempt_id=source_attempt_id,
            precondition_sha256=precondition_sha256,
        )

    @classmethod
    def from_reserved_request(
        cls,
        reserved: ReservedRequest,
        *,
        purpose: MutationPurpose,
        generation: int,
        deadline_ns: int,
        client_id: str,
        authorization_id: str,
        source_attempt_id: str | None,
        precondition_sha256: str | None,
    ) -> MutationReservationProof:
        """Bind one exact protocol mutation reservation into the journal."""

        if type(reserved) is not ReservedRequest or type(purpose) is not MutationPurpose:
            raise ExecutionJournalError("INVALID_MUTATION_RESERVATION")
        request_kind = {
            RequestPurpose.CREATE: MutationKind.CREATE,
            RequestPurpose.CANCEL: MutationKind.CANCEL,
            RequestPurpose.EMERGENCY_CLOSE: MutationKind.EMERGENCY_CLOSE,
        }.get(reserved.purpose)
        if request_kind is None:
            raise ExecutionJournalError("INVALID_MUTATION_RESERVATION")
        client_parameter = (
            "origClientOrderId" if request_kind is MutationKind.CANCEL else "newClientOrderId"
        )
        if dict(reserved.parameters).get(client_parameter) != client_id:
            raise ExecutionJournalError("MUTATION_RESERVED_CLIENT_ID_MISMATCH")
        return cls.build(
            request_sha256=reserved.request_sha256,
            logical_request_sha256=reserved.logical_request_sha256,
            kind=request_kind,
            purpose=purpose,
            method=reserved.method,
            path=reserved.path,
            retry_index=reserved.retry_index,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=reserved.intent_sha256,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=reserved.ledger.total_http_requests,
            parameters_sha256=reserved_request_parameters_sha256(reserved),
            ledger_sha256=reserved_request_ledger_sha256(reserved),
            source_attempt_id=source_attempt_id,
            precondition_sha256=precondition_sha256,
        )

    def validate_dispatch_binding(
        self,
        reserved: ReservedRequest,
        attempt: MutationAttempt,
    ) -> None:
        """Fail closed unless GO carries this exact request and attempt."""

        if type(reserved) is not ReservedRequest:
            raise ExecutionJournalError("MUTATION_RESERVED_REQUEST_MISMATCH")
        expected_request_purpose = {
            MutationKind.CREATE: RequestPurpose.CREATE,
            MutationKind.CANCEL: RequestPurpose.CANCEL,
            MutationKind.EMERGENCY_CLOSE: RequestPurpose.EMERGENCY_CLOSE,
        }[self.kind]
        client_parameter = (
            "origClientOrderId" if self.kind is MutationKind.CANCEL else "newClientOrderId"
        )
        if (
            reserved.purpose is not expected_request_purpose
            or reserved.request_sha256 != self.request_sha256
            or reserved.logical_request_sha256 != self.logical_request_sha256
            or reserved.method != self.method
            or reserved.path != self.path
            or reserved.retry_index != self.retry_index
            or reserved.intent_sha256 != self.intent_sha256
            or reserved.ledger.total_http_requests != self.monotonic_sequence
            or reserved_request_parameters_sha256(reserved) != self.parameters_sha256
            or reserved_request_ledger_sha256(reserved) != self.ledger_sha256
            or dict(reserved.parameters).get(client_parameter) != self.client_id
        ):
            raise ExecutionJournalError("MUTATION_RESERVED_REQUEST_MISMATCH")
        if (
            type(attempt) is not MutationAttempt
            or attempt.reservation_sha256 != self.request_sha256
            or attempt.kind is not self.kind
            or attempt.client_id != self.client_id
            or attempt.authorization_id != self.authorization_id
            or attempt.intent_sha256 != self.intent_sha256
            or attempt.generation != self.generation
            or attempt.retry_index != self.retry_index
            or attempt.deadline_ns > self.deadline_ns
            or (
                self.kind is MutationKind.CANCEL
                and attempt.fresh_open_proof_sha256 != self.precondition_sha256
            )
        ):
            raise ExecutionJournalError("MUTATION_DISPATCH_BINDING_MISMATCH")


def _read_reservation_material(
    *,
    request_sha256: str,
    logical_request_sha256: str,
    read_kind: ReadKind,
    purpose: ReadPurpose,
    method: str,
    path: str,
    retry_index: int,
    generation: int,
    deadline_ns: int,
    monotonic_sequence: int,
    parameters_sha256: str,
    ledger_sha256: str,
    source_attempt_id: str | None,
    client_id: str | None,
    authorization_id: str,
    intent_sha256: str,
) -> dict[str, object]:
    return {
        "reservation_schema": _READ_RESERVATION_SCHEMA,
        "request_sha256": request_sha256,
        "logical_request_sha256": logical_request_sha256,
        "read_kind": read_kind.value,
        "purpose": purpose.value,
        "method": method,
        "path": path,
        "retry_index": retry_index,
        "generation": generation,
        "deadline_ns": deadline_ns,
        "monotonic_sequence": monotonic_sequence,
        "parameters_sha256": parameters_sha256,
        "ledger_sha256": ledger_sha256,
        "source_attempt_id": source_attempt_id,
        "client_id": client_id,
        "authorization_id": authorization_id,
        "intent_sha256": intent_sha256,
    }


@dataclass(frozen=True, slots=True)
class ReadReservationProof:
    """Exact protocol request identity durably recorded before read I/O."""

    request_sha256: str
    proof_sha256: str
    logical_request_sha256: str
    read_kind: ReadKind
    purpose: ReadPurpose
    method: str
    path: str
    retry_index: int
    generation: int
    deadline_ns: int
    monotonic_sequence: int
    parameters_sha256: str
    ledger_sha256: str
    source_attempt_id: str | None
    client_id: str | None
    authorization_id: str
    intent_sha256: str

    def __post_init__(self) -> None:
        source_bound = self.source_attempt_id is not None or self.client_id is not None
        if (
            not _is_sha256(self.request_sha256)
            or not _is_sha256(self.logical_request_sha256)
            or type(self.read_kind) is not ReadKind
            or type(self.purpose) is not ReadPurpose
            or self.method != "GET"
            or type(self.path) is not str
            or _REQUEST_PATH.fullmatch(self.path) is None
            or type(self.retry_index) is not int
            or self.retry_index not in {0, 1}
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.deadline_ns)
            or not _is_positive_int(self.monotonic_sequence)
            or not _is_sha256(self.parameters_sha256)
            or not _is_sha256(self.ledger_sha256)
            or (
                source_bound
                and (
                    not _is_sha256(self.source_attempt_id)
                    or type(self.client_id) is not str
                    or _CLIENT_ID.fullmatch(self.client_id) is None
                )
            )
            or (
                not source_bound
                and (self.source_attempt_id is not None or self.client_id is not None)
            )
            or (
                self.purpose is ReadPurpose.ORDER_RECONCILIATION
                and (not source_bound or self.read_kind is not ReadKind.ORDER)
            )
            or (
                self.purpose is ReadPurpose.OWNED_FILL_CLOSE
                and (not source_bound or self.read_kind is ReadKind.GENERAL)
            )
            or type(self.authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
            or not _is_sha256(self.intent_sha256)
        ):
            raise ExecutionJournalError("INVALID_READ_RESERVATION")
        material = _read_reservation_material(
            request_sha256=self.request_sha256,
            logical_request_sha256=self.logical_request_sha256,
            read_kind=self.read_kind,
            purpose=self.purpose,
            method=self.method,
            path=self.path,
            retry_index=self.retry_index,
            generation=self.generation,
            deadline_ns=self.deadline_ns,
            monotonic_sequence=self.monotonic_sequence,
            parameters_sha256=self.parameters_sha256,
            ledger_sha256=self.ledger_sha256,
            source_attempt_id=self.source_attempt_id,
            client_id=self.client_id,
            authorization_id=self.authorization_id,
            intent_sha256=self.intent_sha256,
        )
        if self.proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest():
            raise ExecutionJournalError("READ_RESERVATION_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        request_sha256: str,
        logical_request_sha256: str,
        read_kind: ReadKind,
        purpose: ReadPurpose,
        method: str,
        path: str,
        retry_index: int,
        generation: int,
        deadline_ns: int,
        monotonic_sequence: int,
        parameters_sha256: str,
        ledger_sha256: str,
        source_attempt_id: str | None,
        client_id: str | None,
        authorization_id: str,
        intent_sha256: str,
    ) -> ReadReservationProof:
        if type(read_kind) is not ReadKind or type(purpose) is not ReadPurpose:
            raise ExecutionJournalError("INVALID_READ_RESERVATION")
        material = _read_reservation_material(
            request_sha256=request_sha256,
            logical_request_sha256=logical_request_sha256,
            read_kind=read_kind,
            purpose=purpose,
            method=method,
            path=path,
            retry_index=retry_index,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=monotonic_sequence,
            parameters_sha256=parameters_sha256,
            ledger_sha256=ledger_sha256,
            source_attempt_id=source_attempt_id,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
        )
        return cls(
            request_sha256=request_sha256,
            proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            logical_request_sha256=logical_request_sha256,
            read_kind=read_kind,
            purpose=purpose,
            method=method,
            path=path,
            retry_index=retry_index,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=monotonic_sequence,
            parameters_sha256=parameters_sha256,
            ledger_sha256=ledger_sha256,
            source_attempt_id=source_attempt_id,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
        )

    @classmethod
    def from_reserved_request(
        cls,
        reserved: ReservedRequest,
        *,
        read_kind: ReadKind,
        purpose: ReadPurpose,
        generation: int,
        deadline_ns: int,
        source_attempt_id: str | None,
        client_id: str | None,
        authorization_id: str,
    ) -> ReadReservationProof:
        """Bind one exact protocol read reservation into the journal."""

        if type(reserved) is not ReservedRequest or reserved.purpose is not RequestPurpose.READ:
            raise ExecutionJournalError("INVALID_READ_RESERVATION")
        if (
            reserved.path == "/fapi/v1/order"
            and client_id is not None
            and dict(reserved.parameters).get("origClientOrderId") != client_id
        ):
            raise ExecutionJournalError("READ_RESERVED_CLIENT_ID_MISMATCH")
        return cls.build(
            request_sha256=reserved.request_sha256,
            logical_request_sha256=reserved.logical_request_sha256,
            read_kind=read_kind,
            purpose=purpose,
            method=reserved.method,
            path=reserved.path,
            retry_index=reserved.retry_index,
            generation=generation,
            deadline_ns=deadline_ns,
            monotonic_sequence=reserved.ledger.total_http_requests,
            parameters_sha256=reserved_request_parameters_sha256(reserved),
            ledger_sha256=reserved_request_ledger_sha256(reserved),
            source_attempt_id=source_attempt_id,
            client_id=client_id,
            authorization_id=authorization_id,
            intent_sha256=reserved.intent_sha256,
        )

    def validate_reserved_request(self, reserved: ReservedRequest) -> None:
        """Fail closed unless this proof names the exact protocol read."""

        if (
            type(reserved) is not ReservedRequest
            or reserved.purpose is not RequestPurpose.READ
            or reserved.request_sha256 != self.request_sha256
            or reserved.logical_request_sha256 != self.logical_request_sha256
            or reserved.method != self.method
            or reserved.path != self.path
            or reserved.retry_index != self.retry_index
            or reserved.intent_sha256 != self.intent_sha256
            or reserved.ledger.total_http_requests != self.monotonic_sequence
            or reserved_request_parameters_sha256(reserved) != self.parameters_sha256
            or reserved_request_ledger_sha256(reserved) != self.ledger_sha256
            or (
                reserved.path == "/fapi/v1/order"
                and self.client_id is not None
                and dict(reserved.parameters).get("origClientOrderId") != self.client_id
            )
        ):
            raise ExecutionJournalError("READ_RESERVED_REQUEST_MISMATCH")


def _read_result_material(
    *,
    request_sha256: str,
    prepared_record_sequence: int,
    prepared_record_digest: str,
    generation: int,
    monotonic_sequence: int,
    read_kind: ReadKind,
    outcome: ReadOutcome,
    result_sha256: str,
    observed_at_ns: int,
) -> dict[str, object]:
    return {
        "result_schema": _READ_RESULT_SCHEMA,
        "request_sha256": request_sha256,
        "prepared_record_sequence": prepared_record_sequence,
        "prepared_record_digest": prepared_record_digest,
        "generation": generation,
        "monotonic_sequence": monotonic_sequence,
        "read_kind": read_kind.value,
        "outcome": outcome.value,
        "result_sha256": result_sha256,
        "observed_at_ns": observed_at_ns,
    }


@dataclass(frozen=True, slots=True)
class ReadResultProof:
    """Sanitized result bound to the exact durable READ_PREPARED record."""

    result_proof_sha256: str
    request_sha256: str
    prepared_record_sequence: int
    prepared_record_digest: str
    generation: int
    monotonic_sequence: int
    read_kind: ReadKind
    outcome: ReadOutcome
    result_sha256: str
    observed_at_ns: int

    def __post_init__(self) -> None:
        allowed_outcomes = {
            ReadKind.ORDER: {
                ReadOutcome.ORDER_NEW,
                ReadOutcome.ORDER_PARTIALLY_FILLED,
                ReadOutcome.ORDER_TERMINAL,
                ReadOutcome.OWNED_ORDER_FILL_CONFIRMED,
                ReadOutcome.NEGATIVE,
            },
            ReadKind.TRADE: {
                ReadOutcome.OWNED_TRADE_FILL_CONFIRMED,
                ReadOutcome.NEGATIVE,
            },
            ReadKind.ACCOUNT: {
                ReadOutcome.OWNED_ACCOUNT_POSITION_CONFIRMED,
                ReadOutcome.NEGATIVE,
            },
            ReadKind.SYMBOL_FILTER: {ReadOutcome.FILTER_SNAPSHOT_CONFIRMED},
            ReadKind.MARK_PRICE: {ReadOutcome.MARK_PRICE_CONFIRMED},
            ReadKind.GENERAL: {ReadOutcome.SUCCESS, ReadOutcome.NEGATIVE},
        }
        if (
            not _is_sha256(self.request_sha256)
            or not _is_positive_int(self.prepared_record_sequence)
            or not _is_sha256(self.prepared_record_digest)
            or not _is_positive_int(self.generation)
            or not _is_positive_int(self.monotonic_sequence)
            or type(self.read_kind) is not ReadKind
            or type(self.outcome) is not ReadOutcome
            or self.outcome not in allowed_outcomes[self.read_kind]
            or not _is_sha256(self.result_sha256)
            or not _is_positive_int(self.observed_at_ns)
        ):
            raise ExecutionJournalError("INVALID_READ_RESULT")
        material = _read_result_material(
            request_sha256=self.request_sha256,
            prepared_record_sequence=self.prepared_record_sequence,
            prepared_record_digest=self.prepared_record_digest,
            generation=self.generation,
            monotonic_sequence=self.monotonic_sequence,
            read_kind=self.read_kind,
            outcome=self.outcome,
            result_sha256=self.result_sha256,
            observed_at_ns=self.observed_at_ns,
        )
        if self.result_proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest():
            raise ExecutionJournalError("READ_RESULT_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        request_sha256: str,
        prepared_record_sequence: int,
        prepared_record_digest: str,
        generation: int,
        monotonic_sequence: int,
        read_kind: ReadKind,
        outcome: ReadOutcome,
        result_sha256: str,
        observed_at_ns: int,
    ) -> ReadResultProof:
        if type(read_kind) is not ReadKind or type(outcome) is not ReadOutcome:
            raise ExecutionJournalError("INVALID_READ_RESULT")
        material = _read_result_material(
            request_sha256=request_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            generation=generation,
            monotonic_sequence=monotonic_sequence,
            read_kind=read_kind,
            outcome=outcome,
            result_sha256=result_sha256,
            observed_at_ns=observed_at_ns,
        )
        return cls(
            result_proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            request_sha256=request_sha256,
            prepared_record_sequence=prepared_record_sequence,
            prepared_record_digest=prepared_record_digest,
            generation=generation,
            monotonic_sequence=monotonic_sequence,
            read_kind=read_kind,
            outcome=outcome,
            result_sha256=result_sha256,
            observed_at_ns=observed_at_ns,
        )


@dataclass(frozen=True, slots=True)
class DurableReadResultReference:
    """Exact journal-record anchor for one sanitized durable read result."""

    result_proof_sha256: str
    record_sequence: int
    record_digest: str
    request_sha256: str
    logical_request_sha256: str
    transport_result_sha256: str
    transport_kind: str

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.result_proof_sha256)
            or not _is_positive_int(self.record_sequence)
            or not _is_sha256(self.record_digest)
            or not _is_sha256(self.request_sha256)
            or not _is_sha256(self.logical_request_sha256)
            or not _is_sha256(self.transport_result_sha256)
            or self.transport_kind
            not in {
                "ORDER_OBSERVATION",
                "USER_TRADES",
                "ACCOUNT",
                "EXCHANGE_INFO",
                "MARK_PRICE",
            }
        ):
            raise ExecutionJournalError("INVALID_READ_RESULT_REFERENCE")


def _read_reference_material(reference: DurableReadResultReference) -> dict[str, object]:
    return {
        "result_proof_sha256": reference.result_proof_sha256,
        "record_sequence": reference.record_sequence,
        "record_digest": reference.record_digest,
        "request_sha256": reference.request_sha256,
        "logical_request_sha256": reference.logical_request_sha256,
        "transport_result_sha256": reference.transport_result_sha256,
        "transport_kind": reference.transport_kind,
    }


def _owned_position_semantics_material(
    *,
    source_intent_sha256: str,
    residual_quantity: str,
    owned_executed_quantity: str,
    open_remainder_quantity: str,
    other_activity_absent: bool,
    position_direction: str,
    probe_terminal_status: str,
    market_close_proof_sha256: str,
    observed_elapsed_seconds: str,
    observed_after_http_attempt: int,
    order_result: DurableReadResultReference,
    trade_result: DurableReadResultReference,
    account_result: DurableReadResultReference,
    symbol_filter_result: DurableReadResultReference,
    mark_price_result: DurableReadResultReference,
) -> dict[str, object]:
    return {
        "proof_schema": _OWNED_POSITION_SEMANTICS_SCHEMA,
        "intent_sha256": source_intent_sha256,
        "symbol": SYMBOL,
        "residual_quantity": residual_quantity,
        "owned_executed_quantity": owned_executed_quantity,
        "open_remainder_quantity": open_remainder_quantity,
        "other_activity_absent": other_activity_absent,
        "position_direction": position_direction,
        "probe_terminal_status": probe_terminal_status,
        "market_close_proof_sha256": market_close_proof_sha256,
        "observed_elapsed_seconds": observed_elapsed_seconds,
        "observed_after_http_attempt": observed_after_http_attempt,
        "source_request_sha256s": {
            "/fapi/v1/order": order_result.request_sha256,
            "/fapi/v1/userTrades": trade_result.request_sha256,
            "/fapi/v2/account": account_result.request_sha256,
            "/fapi/v1/exchangeInfo": symbol_filter_result.request_sha256,
            "/fapi/v1/premiumIndex": mark_price_result.request_sha256,
        },
    }


def _owned_fill_transport_fields(
    result: TransportResult,
    expected: frozenset[str],
) -> dict[str, object]:
    material = dict(result.fields)
    if frozenset(material) != expected:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    return material


def _owned_fill_transport_decimal(
    value: object,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    try:
        parsed = _parse_canonical_decimal(value)
    except ExecutionJournalError:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH") from None
    if (positive and parsed <= 0) or (nonnegative and parsed < 0):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    return parsed


def _validate_owned_fill_transport_semantics(
    *,
    source: MutationAttempt,
    owned_position_proof: OwnedPositionProof,
    order_transport_result: TransportResult,
    trade_transport_result: TransportResult,
    account_transport_result: TransportResult,
    symbol_filter_transport_result: TransportResult,
    mark_price_transport_result: TransportResult,
    request_sha_by_path: Mapping[str, str],
    request_sequences: tuple[int, ...],
    exact_by_kind: Mapping[ReadKind, ReservedRequest],
    results_by_kind: Mapping[ReadKind, ReadResultProof],
) -> None:
    """Bind typed venue semantics to the exact five durable transport digests."""

    if type(owned_position_proof) is not OwnedPositionProof:
        raise ExecutionJournalError("INVALID_OWNED_POSITION_PROOF")
    market_proof = owned_position_proof.market_close_proof

    order_fields = _owned_fill_transport_fields(
        order_transport_result,
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
    executed_quantity = _owned_fill_transport_decimal(
        order_fields["executedQty"],
        positive=True,
    )
    original_quantity = _owned_fill_transport_decimal(
        order_fields["origQty"],
        positive=True,
    )
    _owned_fill_transport_decimal(order_fields["price"], nonnegative=True)
    order_id_sha256 = order_fields["orderIdSha256"]
    terminal_status = order_fields["status"]
    if (
        order_fields["clientOrderId"] != source.client_id
        or not _is_sha256(order_id_sha256)
        or original_quantity < executed_quantity
        or order_fields["positionSide"] != "BOTH"
        or order_fields["reduceOnly"] is not False
        or order_fields["side"] != "BUY"
        or terminal_status not in {"CANCELED", "FILLED", "EXPIRED", "EXPIRED_IN_MATCH"}
        or order_fields["symbol"] != SYMBOL
        or order_fields["timeInForce"] != "GTX"
        or order_fields["type"] != "LIMIT"
    ):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")

    trade_fields = _owned_fill_transport_fields(
        trade_transport_result,
        frozenset({"count", "trades"}),
    )
    trades = trade_fields["trades"]
    if (
        type(trade_fields["count"]) is not int
        or type(trades) is not list
        or trade_fields["count"] != len(trades)
        or not trades
    ):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    trade_quantity = Decimal(0)
    trade_ids: set[str] = set()
    for trade in trades:
        if type(trade) is not dict or frozenset(trade) != frozenset(
            {
                "commission",
                "orderIdSha256",
                "quantity",
                "realizedPnl",
                "tradeIdSha256",
            }
        ):
            raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
        _owned_fill_transport_decimal(trade["commission"], nonnegative=True)
        realized_pnl = _owned_fill_transport_decimal(trade["realizedPnl"])
        quantity = _owned_fill_transport_decimal(trade["quantity"], positive=True)
        trade_id = trade["tradeIdSha256"]
        if (
            realized_pnl != 0
            or trade["orderIdSha256"] != order_id_sha256
            or not _is_sha256(trade_id)
            or trade_id in trade_ids
        ):
            raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
        trade_ids.add(trade_id)
        trade_quantity += quantity
    if trade_quantity != executed_quantity:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")

    account_fields = _owned_fill_transport_fields(
        account_transport_result,
        frozenset({"balances", "canTrade", "multiAssetsMargin", "nonzeroPositions"}),
    )
    positions = account_fields["nonzeroPositions"]
    if (
        type(account_fields["balances"]) is not list
        or account_fields["canTrade"] is not True
        or account_fields["multiAssetsMargin"] is not False
        or type(positions) is not list
        or len(positions) != 1
        or type(positions[0]) is not dict
        or frozenset(positions[0]) != frozenset({"positionAmt", "positionSide", "symbol"})
        or positions[0]["positionSide"] != "BOTH"
        or positions[0]["symbol"] != SYMBOL
    ):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    residual_quantity = _owned_fill_transport_decimal(
        positions[0]["positionAmt"],
        positive=True,
    )
    if residual_quantity != executed_quantity:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")

    filter_fields = _owned_fill_transport_fields(
        symbol_filter_transport_result,
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
    counts = filter_fields["filterTypeCounts"]
    market_lot = filter_fields["marketLotSize"]
    order_types = filter_fields["orderTypes"]
    time_in_force = filter_fields["timeInForce"]
    uninterpreted = filter_fields["uninterpretedFilterTypes"]
    if (
        type(counts) is not dict
        or any(type(key) is not str or type(value) is not int for key, value in counts.items())
        or type(market_lot) is not dict
        or frozenset(market_lot) != frozenset({"minQuantity", "maxQuantity", "stepSize"})
        or type(order_types) is not list
        or "MARKET" not in order_types
        or type(time_in_force) is not list
        or "GTX" not in time_in_force
        or type(uninterpreted) is not list
        or any(type(value) is not str for value in uninterpreted)
        or filter_fields["contractType"] != "PERPETUAL"
        or filter_fields["marginAsset"] != "USDT"
        or filter_fields["quoteAsset"] != "USDT"
        or filter_fields["status"] != "TRADING"
        or filter_fields["symbol"] != SYMBOL
    ):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    try:
        transport_filters = MarketCloseFilters(
            min_quantity=_owned_fill_transport_decimal(
                market_lot["minQuantity"],
                positive=True,
            ),
            max_quantity=_owned_fill_transport_decimal(
                market_lot["maxQuantity"],
                positive=True,
            ),
            step_size=_owned_fill_transport_decimal(
                market_lot["stepSize"],
                positive=True,
            ),
            min_notional=_owned_fill_transport_decimal(
                filter_fields["minNotional"],
                positive=True,
            ),
            market_lot_size_filter_count=counts.get("MARKET_LOT_SIZE", 0),
            min_notional_filter_count=counts.get("MIN_NOTIONAL", 0),
            uninterpreted_applicable_filter_types=tuple(uninterpreted),
        )
    except MutationProtocolError as exc:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH") from exc

    mark_fields = _owned_fill_transport_fields(
        mark_price_transport_result,
        frozenset(
            {
                "localMonotonicAfterNs",
                "localMonotonicBeforeNs",
                "localWallAfterMs",
                "localWallBeforeMs",
                "markPrice",
                "symbol",
                "time",
            }
        ),
    )
    timed_values = (
        mark_fields["localMonotonicAfterNs"],
        mark_fields["localMonotonicBeforeNs"],
        mark_fields["localWallAfterMs"],
        mark_fields["localWallBeforeMs"],
        mark_fields["time"],
    )
    mark_result = results_by_kind[ReadKind.MARK_PRICE]
    if (
        any(type(value) is not int or value <= 0 for value in timed_values)
        or mark_fields["localMonotonicAfterNs"] < mark_fields["localMonotonicBeforeNs"]
        or mark_fields["localWallAfterMs"] < mark_fields["localWallBeforeMs"]
        or mark_fields["localMonotonicAfterNs"] > mark_result.observed_at_ns
        or mark_fields["symbol"] != SYMBOL
    ):
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")
    mark_price = _owned_fill_transport_decimal(mark_fields["markPrice"], positive=True)
    mark_age_ms = (
        Decimal(mark_fields["localWallBeforeMs"]) + Decimal(mark_fields["localWallAfterMs"])
    ) / Decimal(2) - Decimal(mark_fields["time"])
    if mark_age_ms < 0:
        raise ExecutionJournalError("OWNED_FILL_TRANSPORT_SEMANTICS_MISMATCH")

    expected_observed_elapsed = max(request.elapsed_seconds for request in exact_by_kind.values())
    values_are_exact = (
        owned_position_proof.intent_sha256 == source.intent_sha256
        and owned_position_proof.symbol == SYMBOL
        and type(owned_position_proof.residual_quantity) is Decimal
        and type(owned_position_proof.owned_executed_quantity) is Decimal
        and type(owned_position_proof.open_remainder_quantity) is Decimal
        and owned_position_proof.residual_quantity.is_finite()
        and owned_position_proof.owned_executed_quantity.is_finite()
        and owned_position_proof.open_remainder_quantity.is_finite()
        and owned_position_proof.residual_quantity
        == residual_quantity
        == owned_position_proof.owned_executed_quantity
        == executed_quantity
        and owned_position_proof.open_remainder_quantity == 0
        and owned_position_proof.position_direction == "LONG"
        and owned_position_proof.probe_terminal_status == terminal_status
        and owned_position_proof.other_activity_absent is True
        and owned_position_proof.source_request_sha256s
        == tuple(sorted(request_sha_by_path.items()))
        and owned_position_proof.observed_after_http_attempt == max(request_sequences)
        and type(owned_position_proof.observed_elapsed_seconds) is Decimal
        and owned_position_proof.observed_elapsed_seconds == expected_observed_elapsed
        and type(market_proof) is MarketCloseProof
        and market_proof.quantity == residual_quantity
        and market_proof.filters == transport_filters
        and market_proof.filter_snapshot_sha256 == symbol_filter_transport_result.result_sha256
        and market_proof.filter_contract_sha256 == transport_filters.canonical_sha256
        and market_proof.mark_price == mark_price
        and market_proof.mark_price_age_ms == mark_age_ms
        and market_proof.observed_elapsed_seconds
        == exact_by_kind[ReadKind.MARK_PRICE].elapsed_seconds
    )
    if not values_are_exact:
        raise ExecutionJournalError("OWNED_FILL_POSITION_PROOF_MISMATCH")
    try:
        validate_market_close_proof(
            market_proof,
            max_owned_quantity=owned_position_proof.owned_executed_quantity,
            reservation_elapsed_seconds=owned_position_proof.observed_elapsed_seconds,
        )
    except MutationProtocolError as exc:
        raise ExecutionJournalError("OWNED_FILL_MARKET_PROOF_INVALID") from exc


def _owned_fill_close_material(
    *,
    source_attempt_id: str,
    source_authorization_id: str,
    source_intent_sha256: str,
    source_runtime_commit: str,
    source_session_nonce: str,
    source_client_id: str,
    generation: int,
    residual_quantity: str,
    owned_executed_quantity: str,
    open_remainder_quantity: str,
    other_activity_absent: bool,
    position_direction: str,
    probe_terminal_status: str,
    market_close_proof_sha256: str,
    owned_position_proof_sha256: str,
    filter_snapshot_sha256: str,
    filter_contract_sha256: str,
    mark_price: str,
    mark_price_age_ms: str,
    market_observed_elapsed_seconds: str,
    observed_elapsed_seconds: str,
    observed_after_http_attempt: int,
    order_result: DurableReadResultReference,
    trade_result: DurableReadResultReference,
    account_result: DurableReadResultReference,
    symbol_filter_result: DurableReadResultReference,
    mark_price_result: DurableReadResultReference,
) -> dict[str, object]:
    return {
        "proof_schema": _OWNED_FILL_CLOSE_SCHEMA,
        "source_attempt_id": source_attempt_id,
        "source_authorization_id": source_authorization_id,
        "source_intent_sha256": source_intent_sha256,
        "source_runtime_commit": source_runtime_commit,
        "source_session_nonce": source_session_nonce,
        "source_client_id": source_client_id,
        "generation": generation,
        "residual_quantity": residual_quantity,
        "owned_executed_quantity": owned_executed_quantity,
        "open_remainder_quantity": open_remainder_quantity,
        "other_activity_absent": other_activity_absent,
        "position_direction": position_direction,
        "probe_terminal_status": probe_terminal_status,
        "market_close_proof_sha256": market_close_proof_sha256,
        "owned_position_proof_sha256": owned_position_proof_sha256,
        "filter_snapshot_sha256": filter_snapshot_sha256,
        "filter_contract_sha256": filter_contract_sha256,
        "mark_price": mark_price,
        "mark_price_age_ms": mark_price_age_ms,
        "market_observed_elapsed_seconds": market_observed_elapsed_seconds,
        "observed_elapsed_seconds": observed_elapsed_seconds,
        "observed_after_http_attempt": observed_after_http_attempt,
        "order_result": _read_reference_material(order_result),
        "trade_result": _read_reference_material(trade_result),
        "account_result": _read_reference_material(account_result),
        "symbol_filter_result": _read_reference_material(symbol_filter_result),
        "mark_price_result": _read_reference_material(mark_price_result),
    }


@dataclass(frozen=True, slots=True)
class OwnedFillCloseProof:
    """Five-source durable ownership proof authorizing one first close."""

    proof_sha256: str
    source_attempt_id: str
    source_authorization_id: str
    source_intent_sha256: str
    source_runtime_commit: str
    source_session_nonce: str
    source_client_id: str
    generation: int
    residual_quantity: str
    owned_executed_quantity: str
    open_remainder_quantity: str
    other_activity_absent: bool
    position_direction: str
    probe_terminal_status: str
    market_close_proof_sha256: str
    owned_position_proof_sha256: str
    filter_snapshot_sha256: str
    filter_contract_sha256: str
    mark_price: str
    mark_price_age_ms: str
    market_observed_elapsed_seconds: str
    observed_elapsed_seconds: str
    observed_after_http_attempt: int
    order_result: DurableReadResultReference
    trade_result: DurableReadResultReference
    account_result: DurableReadResultReference
    symbol_filter_result: DurableReadResultReference
    mark_price_result: DurableReadResultReference

    def __post_init__(self) -> None:
        references = (
            self.order_result,
            self.trade_result,
            self.account_result,
            self.symbol_filter_result,
            self.mark_price_result,
        )
        if any(type(reference) is not DurableReadResultReference for reference in references):
            raise ExecutionJournalError("INVALID_OWNED_FILL_CLOSE_PROOF")
        residual = _parse_canonical_decimal(self.residual_quantity)
        owned_executed = _parse_canonical_decimal(self.owned_executed_quantity)
        open_remainder = _parse_canonical_decimal(self.open_remainder_quantity)
        mark_price = _parse_canonical_decimal(self.mark_price)
        mark_age = _parse_canonical_decimal(self.mark_price_age_ms)
        market_observed_elapsed = _parse_canonical_decimal(self.market_observed_elapsed_seconds)
        observed_elapsed = _parse_canonical_decimal(self.observed_elapsed_seconds)
        expected_market_proof_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "filter_snapshot_sha256": self.filter_snapshot_sha256,
                    "filter_contract_sha256": self.filter_contract_sha256,
                    "mark_price": self.mark_price,
                    "mark_price_age_ms": self.mark_price_age_ms,
                    "quantity": self.residual_quantity,
                    "observed_elapsed_seconds": self.market_observed_elapsed_seconds,
                }
            )
        ).hexdigest()
        expected_owned_position_sha256 = hashlib.sha256(
            _canonical_json(
                _owned_position_semantics_material(
                    source_intent_sha256=self.source_intent_sha256,
                    residual_quantity=self.residual_quantity,
                    owned_executed_quantity=self.owned_executed_quantity,
                    open_remainder_quantity=self.open_remainder_quantity,
                    other_activity_absent=self.other_activity_absent,
                    position_direction=self.position_direction,
                    probe_terminal_status=self.probe_terminal_status,
                    market_close_proof_sha256=self.market_close_proof_sha256,
                    observed_elapsed_seconds=self.observed_elapsed_seconds,
                    observed_after_http_attempt=self.observed_after_http_attempt,
                    order_result=self.order_result,
                    trade_result=self.trade_result,
                    account_result=self.account_result,
                    symbol_filter_result=self.symbol_filter_result,
                    mark_price_result=self.mark_price_result,
                )
            )
        ).hexdigest()
        if (
            not _is_sha256(self.source_attempt_id)
            or type(self.source_authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.source_authorization_id) is None
            or not _is_sha256(self.source_intent_sha256)
            or type(self.source_runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_runtime_commit) is None
            or type(self.source_session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.source_session_nonce) is None
            or type(self.source_client_id) is not str
            or _CLIENT_ID.fullmatch(self.source_client_id) is None
            or not _is_positive_int(self.generation)
            or residual <= 0
            or owned_executed != residual
            or open_remainder != 0
            or self.other_activity_absent is not True
            or self.position_direction != "LONG"
            or self.probe_terminal_status
            not in {"CANCELED", "FILLED", "EXPIRED", "EXPIRED_IN_MATCH"}
            or not _is_sha256(self.market_close_proof_sha256)
            or self.market_close_proof_sha256 != expected_market_proof_sha256
            or self.owned_position_proof_sha256 != expected_owned_position_sha256
            or not _is_sha256(self.filter_snapshot_sha256)
            or not _is_sha256(self.filter_contract_sha256)
            or mark_price <= 0
            or mark_age < 0
            or mark_age > Decimal("1000")
            or market_observed_elapsed < 0
            or observed_elapsed < 0
            or observed_elapsed < market_observed_elapsed
            or not _is_positive_int(self.observed_after_http_attempt)
            or len({reference.result_proof_sha256 for reference in references}) != 5
        ):
            raise ExecutionJournalError("INVALID_OWNED_FILL_CLOSE_PROOF")
        material = _owned_fill_close_material(
            source_attempt_id=self.source_attempt_id,
            source_authorization_id=self.source_authorization_id,
            source_intent_sha256=self.source_intent_sha256,
            source_runtime_commit=self.source_runtime_commit,
            source_session_nonce=self.source_session_nonce,
            source_client_id=self.source_client_id,
            generation=self.generation,
            residual_quantity=self.residual_quantity,
            owned_executed_quantity=self.owned_executed_quantity,
            open_remainder_quantity=self.open_remainder_quantity,
            other_activity_absent=self.other_activity_absent,
            position_direction=self.position_direction,
            probe_terminal_status=self.probe_terminal_status,
            market_close_proof_sha256=self.market_close_proof_sha256,
            owned_position_proof_sha256=self.owned_position_proof_sha256,
            filter_snapshot_sha256=self.filter_snapshot_sha256,
            filter_contract_sha256=self.filter_contract_sha256,
            mark_price=self.mark_price,
            mark_price_age_ms=self.mark_price_age_ms,
            market_observed_elapsed_seconds=self.market_observed_elapsed_seconds,
            observed_elapsed_seconds=self.observed_elapsed_seconds,
            observed_after_http_attempt=self.observed_after_http_attempt,
            order_result=self.order_result,
            trade_result=self.trade_result,
            account_result=self.account_result,
            symbol_filter_result=self.symbol_filter_result,
            mark_price_result=self.mark_price_result,
        )
        if self.proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest():
            raise ExecutionJournalError("OWNED_FILL_CLOSE_PROOF_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        source_attempt_id: str,
        source_authorization_id: str,
        source_intent_sha256: str,
        source_runtime_commit: str,
        source_session_nonce: str,
        source_client_id: str,
        generation: int,
        residual_quantity: str,
        owned_executed_quantity: str,
        open_remainder_quantity: str,
        other_activity_absent: bool,
        position_direction: str,
        probe_terminal_status: str,
        market_close_proof_sha256: str,
        filter_snapshot_sha256: str,
        filter_contract_sha256: str,
        mark_price: str,
        mark_price_age_ms: str,
        market_observed_elapsed_seconds: str,
        observed_elapsed_seconds: str,
        observed_after_http_attempt: int,
        order_result: DurableReadResultReference,
        trade_result: DurableReadResultReference,
        account_result: DurableReadResultReference,
        symbol_filter_result: DurableReadResultReference,
        mark_price_result: DurableReadResultReference,
    ) -> OwnedFillCloseProof:
        owned_position_proof_sha256 = hashlib.sha256(
            _canonical_json(
                _owned_position_semantics_material(
                    source_intent_sha256=source_intent_sha256,
                    residual_quantity=residual_quantity,
                    owned_executed_quantity=owned_executed_quantity,
                    open_remainder_quantity=open_remainder_quantity,
                    other_activity_absent=other_activity_absent,
                    position_direction=position_direction,
                    probe_terminal_status=probe_terminal_status,
                    market_close_proof_sha256=market_close_proof_sha256,
                    observed_elapsed_seconds=observed_elapsed_seconds,
                    observed_after_http_attempt=observed_after_http_attempt,
                    order_result=order_result,
                    trade_result=trade_result,
                    account_result=account_result,
                    symbol_filter_result=symbol_filter_result,
                    mark_price_result=mark_price_result,
                )
            )
        ).hexdigest()
        material = _owned_fill_close_material(
            source_attempt_id=source_attempt_id,
            source_authorization_id=source_authorization_id,
            source_intent_sha256=source_intent_sha256,
            source_runtime_commit=source_runtime_commit,
            source_session_nonce=source_session_nonce,
            source_client_id=source_client_id,
            generation=generation,
            residual_quantity=residual_quantity,
            owned_executed_quantity=owned_executed_quantity,
            open_remainder_quantity=open_remainder_quantity,
            other_activity_absent=other_activity_absent,
            position_direction=position_direction,
            probe_terminal_status=probe_terminal_status,
            market_close_proof_sha256=market_close_proof_sha256,
            owned_position_proof_sha256=owned_position_proof_sha256,
            filter_snapshot_sha256=filter_snapshot_sha256,
            filter_contract_sha256=filter_contract_sha256,
            mark_price=mark_price,
            mark_price_age_ms=mark_price_age_ms,
            market_observed_elapsed_seconds=market_observed_elapsed_seconds,
            observed_elapsed_seconds=observed_elapsed_seconds,
            observed_after_http_attempt=observed_after_http_attempt,
            order_result=order_result,
            trade_result=trade_result,
            account_result=account_result,
            symbol_filter_result=symbol_filter_result,
            mark_price_result=mark_price_result,
        )
        return cls(
            proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            source_attempt_id=source_attempt_id,
            source_authorization_id=source_authorization_id,
            source_intent_sha256=source_intent_sha256,
            source_runtime_commit=source_runtime_commit,
            source_session_nonce=source_session_nonce,
            source_client_id=source_client_id,
            generation=generation,
            residual_quantity=residual_quantity,
            owned_executed_quantity=owned_executed_quantity,
            open_remainder_quantity=open_remainder_quantity,
            other_activity_absent=other_activity_absent,
            position_direction=position_direction,
            probe_terminal_status=probe_terminal_status,
            market_close_proof_sha256=market_close_proof_sha256,
            owned_position_proof_sha256=owned_position_proof_sha256,
            filter_snapshot_sha256=filter_snapshot_sha256,
            filter_contract_sha256=filter_contract_sha256,
            mark_price=mark_price,
            mark_price_age_ms=mark_price_age_ms,
            market_observed_elapsed_seconds=market_observed_elapsed_seconds,
            observed_elapsed_seconds=observed_elapsed_seconds,
            observed_after_http_attempt=observed_after_http_attempt,
            order_result=order_result,
            trade_result=trade_result,
            account_result=account_result,
            symbol_filter_result=symbol_filter_result,
            mark_price_result=mark_price_result,
        )


@dataclass(frozen=True, slots=True)
class DurableGenerationAdmission:
    """Process identity durably bound to a mutation-capable generation."""

    generation: int
    process_identity_sha256: str

    def __post_init__(self) -> None:
        if not _is_positive_int(self.generation) or not _is_sha256(self.process_identity_sha256):
            raise ExecutionJournalError("INVALID_GENERATION_ADMISSION")


def _staged_generation_recovery_material(
    *,
    generation: int,
    capability: GenerationCapability,
    process_identity_sha256: str,
    process_staged_record_sequence: int,
    process_staged_record_digest: str,
    process_reaped_record_sequence: int,
    process_reaped_record_digest: str,
    reap_attestation_sha256: str,
    returncode: int,
    signal: int | None,
    local_process_quiesced: bool,
    venue_mutation_absent_proven: bool,
) -> dict[str, object]:
    return {
        "proof_schema": _STAGED_GENERATION_RECOVERY_SCHEMA,
        "generation": generation,
        "capability": capability.value,
        "process_identity_sha256": process_identity_sha256,
        "process_staged_record_sequence": process_staged_record_sequence,
        "process_staged_record_digest": process_staged_record_digest,
        "process_reaped_record_sequence": process_reaped_record_sequence,
        "process_reaped_record_digest": process_reaped_record_digest,
        "reap_attestation_sha256": reap_attestation_sha256,
        "returncode": returncode,
        "signal": signal,
        "local_process_quiesced": local_process_quiesced,
        "venue_mutation_absent_proven": venue_mutation_absent_proven,
    }


@dataclass(frozen=True, slots=True)
class StagedGenerationRecoveryProof:
    """External-WAL anchors for a process generation never admitted here."""

    proof_sha256: str
    generation: int
    capability: GenerationCapability
    process_identity_sha256: str
    process_staged_record_sequence: int
    process_staged_record_digest: str
    process_reaped_record_sequence: int
    process_reaped_record_digest: str
    reap_attestation_sha256: str
    returncode: int
    signal: int | None
    local_process_quiesced: bool
    venue_mutation_absent_proven: bool

    def __post_init__(self) -> None:
        expected_capability = (
            GenerationCapability.PRIMARY if self.generation == 1 else GenerationCapability.RECOVERY
        )
        if (
            not _is_positive_int(self.generation)
            or type(self.capability) is not GenerationCapability
            or self.capability is not expected_capability
            or not _is_sha256(self.process_identity_sha256)
            or not _is_positive_int(self.process_staged_record_sequence)
            or not _is_sha256(self.process_staged_record_digest)
            or not _is_positive_int(self.process_reaped_record_sequence)
            or self.process_reaped_record_sequence <= self.process_staged_record_sequence
            or not _is_sha256(self.process_reaped_record_digest)
            or not _is_sha256(self.reap_attestation_sha256)
            or type(self.returncode) is not int
            or (self.signal is not None and (type(self.signal) is not int or self.signal <= 0))
            or self.local_process_quiesced is not True
            or self.venue_mutation_absent_proven is not False
        ):
            raise ExecutionJournalError("INVALID_STAGED_GENERATION_RECOVERY")
        material = _staged_generation_recovery_material(
            generation=self.generation,
            capability=self.capability,
            process_identity_sha256=self.process_identity_sha256,
            process_staged_record_sequence=self.process_staged_record_sequence,
            process_staged_record_digest=self.process_staged_record_digest,
            process_reaped_record_sequence=self.process_reaped_record_sequence,
            process_reaped_record_digest=self.process_reaped_record_digest,
            reap_attestation_sha256=self.reap_attestation_sha256,
            returncode=self.returncode,
            signal=self.signal,
            local_process_quiesced=self.local_process_quiesced,
            venue_mutation_absent_proven=self.venue_mutation_absent_proven,
        )
        if self.proof_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest():
            raise ExecutionJournalError("STAGED_GENERATION_RECOVERY_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        generation: int,
        capability: GenerationCapability,
        process_identity_sha256: str,
        process_staged_record_sequence: int,
        process_staged_record_digest: str,
        process_reaped_record_sequence: int,
        process_reaped_record_digest: str,
        reap_attestation_sha256: str,
        returncode: int,
        signal: int | None,
        local_process_quiesced: bool,
        venue_mutation_absent_proven: bool,
    ) -> StagedGenerationRecoveryProof:
        if type(capability) is not GenerationCapability:
            raise ExecutionJournalError("INVALID_STAGED_GENERATION_RECOVERY")
        material = _staged_generation_recovery_material(
            generation=generation,
            capability=capability,
            process_identity_sha256=process_identity_sha256,
            process_staged_record_sequence=process_staged_record_sequence,
            process_staged_record_digest=process_staged_record_digest,
            process_reaped_record_sequence=process_reaped_record_sequence,
            process_reaped_record_digest=process_reaped_record_digest,
            reap_attestation_sha256=reap_attestation_sha256,
            returncode=returncode,
            signal=signal,
            local_process_quiesced=local_process_quiesced,
            venue_mutation_absent_proven=venue_mutation_absent_proven,
        )
        return cls(
            proof_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            generation=generation,
            capability=capability,
            process_identity_sha256=process_identity_sha256,
            process_staged_record_sequence=process_staged_record_sequence,
            process_staged_record_digest=process_staged_record_digest,
            process_reaped_record_sequence=process_reaped_record_sequence,
            process_reaped_record_digest=process_reaped_record_digest,
            reap_attestation_sha256=reap_attestation_sha256,
            returncode=returncode,
            signal=signal,
            local_process_quiesced=local_process_quiesced,
            venue_mutation_absent_proven=venue_mutation_absent_proven,
        )


@dataclass(frozen=True, slots=True)
class ProcessReapReceipt:
    """Sanitized exact-reap fact; venue absence is intentionally never proven."""

    generation: int
    process_identity_sha256: str
    admission_record_sequence: int
    admission_record_digest: str
    returncode: int
    signal: int | None
    local_process_quiesced: bool
    venue_mutation_absent_proven: bool

    def __post_init__(self) -> None:
        if (
            not _is_positive_int(self.generation)
            or not _is_sha256(self.process_identity_sha256)
            or not _is_positive_int(self.admission_record_sequence)
            or not _is_sha256(self.admission_record_digest)
            or type(self.returncode) is not int
            or (self.signal is not None and (type(self.signal) is not int or self.signal <= 0))
        ):
            raise ExecutionJournalError("INVALID_REAP_RECEIPT")
        if self.local_process_quiesced is not True:
            raise ExecutionJournalError("LOCAL_QUIESCENCE_REQUIRED")
        if self.venue_mutation_absent_proven is not False:
            raise ExecutionJournalError("VENUE_ABSENCE_MUST_REMAIN_UNPROVEN")


def _observation_material(
    *,
    source_attempt_id: str,
    source_authorization_id: str,
    source_client_id: str,
    generation: int,
    order_status: ReconciledOrderStatus,
    read_reservation_sha256: str,
    read_result_proof_sha256: str,
    read_result_record_sequence: int,
    read_result_record_digest: str,
) -> dict[str, object]:
    return {
        "observation_schema": _OBSERVATION_SCHEMA,
        "source_attempt_id": source_attempt_id,
        "source_authorization_id": source_authorization_id,
        "source_client_id": source_client_id,
        "generation": generation,
        "order_status": order_status.value,
        "read_reservation_sha256": read_reservation_sha256,
        "read_result_proof_sha256": read_result_proof_sha256,
        "read_result_record_sequence": read_result_record_sequence,
        "read_result_record_digest": read_result_record_digest,
    }


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """Durable fresh OPEN observation bound to one source and recovery generation."""

    observation_sha256: str
    source_attempt_id: str
    source_authorization_id: str
    source_client_id: str
    generation: int
    order_status: ReconciledOrderStatus
    read_reservation_sha256: str
    read_result_proof_sha256: str
    read_result_record_sequence: int
    read_result_record_digest: str

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.source_attempt_id)
            or type(self.source_authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.source_authorization_id) is None
            or type(self.source_client_id) is not str
            or _CLIENT_ID.fullmatch(self.source_client_id) is None
            or not _is_positive_int(self.generation)
            or type(self.order_status) is not ReconciledOrderStatus
            or not _is_sha256(self.read_reservation_sha256)
            or not _is_sha256(self.read_result_proof_sha256)
            or not _is_positive_int(self.read_result_record_sequence)
            or not _is_sha256(self.read_result_record_digest)
        ):
            raise ExecutionJournalError("INVALID_RECONCILIATION_OBSERVATION")
        material = _observation_material(
            source_attempt_id=self.source_attempt_id,
            source_authorization_id=self.source_authorization_id,
            source_client_id=self.source_client_id,
            generation=self.generation,
            order_status=self.order_status,
            read_reservation_sha256=self.read_reservation_sha256,
            read_result_proof_sha256=self.read_result_proof_sha256,
            read_result_record_sequence=self.read_result_record_sequence,
            read_result_record_digest=self.read_result_record_digest,
        )
        if self.observation_sha256 != hashlib.sha256(_canonical_json(material)).hexdigest():
            raise ExecutionJournalError("OBSERVATION_DIGEST_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        source_attempt_id: str,
        source_authorization_id: str,
        source_client_id: str,
        generation: int,
        order_status: ReconciledOrderStatus,
        read_reservation_sha256: str,
        read_result_proof_sha256: str,
        read_result_record_sequence: int,
        read_result_record_digest: str,
    ) -> ReconciliationObservation:
        if type(order_status) is not ReconciledOrderStatus:
            raise ExecutionJournalError("INVALID_RECONCILIATION_OBSERVATION")
        material = _observation_material(
            source_attempt_id=source_attempt_id,
            source_authorization_id=source_authorization_id,
            source_client_id=source_client_id,
            generation=generation,
            order_status=order_status,
            read_reservation_sha256=read_reservation_sha256,
            read_result_proof_sha256=read_result_proof_sha256,
            read_result_record_sequence=read_result_record_sequence,
            read_result_record_digest=read_result_record_digest,
        )
        return cls(
            observation_sha256=hashlib.sha256(_canonical_json(material)).hexdigest(),
            source_attempt_id=source_attempt_id,
            source_authorization_id=source_authorization_id,
            source_client_id=source_client_id,
            generation=generation,
            order_status=order_status,
            read_reservation_sha256=read_reservation_sha256,
            read_result_proof_sha256=read_result_proof_sha256,
            read_result_record_sequence=read_result_record_sequence,
            read_result_record_digest=read_result_record_digest,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationKey:
    """A fixed typed lookup key for post-crash venue reconciliation."""

    kind: ReconciliationKeyKind
    client_id: str

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not ReconciliationKeyKind
            or type(self.client_id) is not str
            or _CLIENT_ID.fullmatch(self.client_id) is None
        ):
            raise ExecutionJournalError("INVALID_RECONCILIATION_KEY")


def _attempt_identity_material(
    *,
    kind: MutationKind,
    client_id: str,
    reconciliation_key: ReconciliationKey,
    retry_index: int,
    deadline_ns: int,
    generation: int,
    reservation_sha256: str,
    authorization_id: str,
    intent_sha256: str,
    runtime_commit: str,
    session_nonce: str,
    fresh_open_proof_sha256: str | None,
    recovery_of_attempt_id: str | None,
) -> dict[str, object]:
    return {
        "attempt_schema": _ATTEMPT_SCHEMA,
        "authorization_id": authorization_id,
        "client_id": client_id,
        "deadline_ns": deadline_ns,
        "fresh_open_proof_sha256": fresh_open_proof_sha256,
        "generation": generation,
        "intent_sha256": intent_sha256,
        "kind": kind.value,
        "reconciliation_key": {
            "client_id": reconciliation_key.client_id,
            "kind": reconciliation_key.kind.value,
        },
        "recovery_of_attempt_id": recovery_of_attempt_id,
        "reservation_sha256": reservation_sha256,
        "retry_index": retry_index,
        "runtime_commit": runtime_commit,
        "session_nonce": session_nonce,
    }


@dataclass(frozen=True, slots=True)
class MutationAttempt:
    """Immutable, hash-identified mutation attempt with no transport payload."""

    attempt_id: str
    kind: MutationKind
    client_id: str
    reconciliation_key: ReconciliationKey
    retry_index: int
    deadline_ns: int
    generation: int
    reservation_sha256: str
    authorization_id: str
    intent_sha256: str
    runtime_commit: str
    session_nonce: str
    fresh_open_proof_sha256: str | None = None
    recovery_of_attempt_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.retry_index) is not int or self.retry_index != 0:
            raise ExecutionJournalError("MUTATION_RETRY_FORBIDDEN")
        if (
            type(self.kind) is not MutationKind
            or not _is_positive_int(self.deadline_ns)
            or not _is_positive_int(self.generation)
            or not _is_sha256(self.reservation_sha256)
            or type(self.authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
            or not _is_sha256(self.intent_sha256)
            or type(self.runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.runtime_commit) is None
            or type(self.session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.session_nonce) is None
            or type(self.client_id) is not str
            or _CLIENT_ID.fullmatch(self.client_id) is None
            or type(self.reconciliation_key) is not ReconciliationKey
        ):
            raise ExecutionJournalError("INVALID_ATTEMPT")
        if self.kind is MutationKind.CANCEL:
            if not _is_sha256(self.fresh_open_proof_sha256):
                raise ExecutionJournalError("FRESH_OPEN_PROOF_REQUIRED")
        elif self.fresh_open_proof_sha256 is not None:
            raise ExecutionJournalError("UNEXPECTED_OPEN_PROOF")
        if self.recovery_of_attempt_id is not None and (
            self.kind not in {MutationKind.CANCEL, MutationKind.EMERGENCY_CLOSE}
            or not _is_sha256(self.recovery_of_attempt_id)
        ):
            raise ExecutionJournalError("INVALID_RECOVERY_ATTEMPT_LINK")

        if self.kind is MutationKind.EMERGENCY_CLOSE:
            expected_client_id = build_emergency_client_order_id(
                self.runtime_commit,
                self.session_nonce,
            )
            key_kind = ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID
        else:
            expected_client_id = build_client_order_id(
                self.runtime_commit,
                self.session_nonce,
            )
            key_kind = (
                ReconciliationKeyKind.PROBE_CLIENT_ID
                if self.kind is MutationKind.CREATE
                else ReconciliationKeyKind.PROBE_TERMINAL_STATE
            )
        if self.client_id != expected_client_id:
            raise ExecutionJournalError("CLIENT_ID_MISMATCH")
        expected_key = ReconciliationKey(kind=key_kind, client_id=expected_client_id)
        if self.reconciliation_key != expected_key:
            raise ExecutionJournalError("RECONCILIATION_KEY_MISMATCH")

        material = _attempt_identity_material(
            kind=self.kind,
            client_id=self.client_id,
            reconciliation_key=self.reconciliation_key,
            retry_index=self.retry_index,
            deadline_ns=self.deadline_ns,
            generation=self.generation,
            reservation_sha256=self.reservation_sha256,
            authorization_id=self.authorization_id,
            intent_sha256=self.intent_sha256,
            runtime_commit=self.runtime_commit,
            session_nonce=self.session_nonce,
            fresh_open_proof_sha256=self.fresh_open_proof_sha256,
            recovery_of_attempt_id=self.recovery_of_attempt_id,
        )
        expected_attempt_id = hashlib.sha256(_canonical_json(material)).hexdigest()
        if self.attempt_id != expected_attempt_id:
            raise ExecutionJournalError("ATTEMPT_IDENTITY_MISMATCH")

    @classmethod
    def build(
        cls,
        *,
        kind: MutationKind,
        generation: int,
        retry_index: int,
        deadline_ns: int,
        reservation_sha256: str,
        authorization_id: str,
        intent_sha256: str,
        runtime_commit: str,
        session_nonce: str,
        fresh_open_proof_sha256: str | None = None,
        recovery_of_attempt_id: str | None = None,
    ) -> MutationAttempt:
        """Derive all identity and reconciliation fields from sanitized bindings."""

        if type(kind) is not MutationKind:
            raise ExecutionJournalError("INVALID_ATTEMPT")
        if type(retry_index) is not int or retry_index != 0:
            raise ExecutionJournalError("MUTATION_RETRY_FORBIDDEN")
        try:
            if kind is MutationKind.EMERGENCY_CLOSE:
                client_id = build_emergency_client_order_id(runtime_commit, session_nonce)
                key_kind = ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID
            else:
                client_id = build_client_order_id(runtime_commit, session_nonce)
                key_kind = (
                    ReconciliationKeyKind.PROBE_CLIENT_ID
                    if kind is MutationKind.CREATE
                    else ReconciliationKeyKind.PROBE_TERMINAL_STATE
                )
        except ValueError as exc:
            raise ExecutionJournalError("INVALID_ATTEMPT") from exc
        reconciliation_key = ReconciliationKey(key_kind, client_id)
        material = _attempt_identity_material(
            kind=kind,
            client_id=client_id,
            reconciliation_key=reconciliation_key,
            retry_index=retry_index,
            deadline_ns=deadline_ns,
            generation=generation,
            reservation_sha256=reservation_sha256,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
            runtime_commit=runtime_commit,
            session_nonce=session_nonce,
            fresh_open_proof_sha256=fresh_open_proof_sha256,
            recovery_of_attempt_id=recovery_of_attempt_id,
        )
        attempt_id = hashlib.sha256(_canonical_json(material)).hexdigest()
        return cls(
            attempt_id=attempt_id,
            kind=kind,
            client_id=client_id,
            reconciliation_key=reconciliation_key,
            retry_index=retry_index,
            deadline_ns=deadline_ns,
            generation=generation,
            reservation_sha256=reservation_sha256,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
            runtime_commit=runtime_commit,
            session_nonce=session_nonce,
            fresh_open_proof_sha256=fresh_open_proof_sha256,
            recovery_of_attempt_id=recovery_of_attempt_id,
        )


@dataclass(frozen=True, slots=True, init=False)
class VerifiedMutationDispatchReceipt:
    """Unforgeable-by-constructor receipt for one journal-replayed GO request."""

    attempt: MutationAttempt
    reservation_proof: MutationReservationProof
    reserved_request: ReservedRequest
    intent_binding: IntentChainBinding
    precondition_sha256: str | None
    journal_head_sequence: int
    journal_head_digest: str

    @classmethod
    def _from_verified_replay(
        cls,
        *,
        attempt: MutationAttempt,
        reservation_proof: MutationReservationProof,
        reserved_request: ReservedRequest,
        intent_binding: IntentChainBinding,
        precondition_sha256: str | None,
        journal_head_sequence: int,
        journal_head_digest: str,
        _token: object,
    ) -> VerifiedMutationDispatchReceipt:
        if _token is not _VERIFIED_DISPATCH_RECEIPT_TOKEN:
            raise ExecutionJournalError("DISPATCH_RECEIPT_CONSTRUCTOR_FORBIDDEN")
        instance = object.__new__(cls)
        object.__setattr__(instance, "attempt", attempt)
        object.__setattr__(instance, "reservation_proof", reservation_proof)
        object.__setattr__(instance, "reserved_request", reserved_request)
        object.__setattr__(instance, "intent_binding", intent_binding)
        object.__setattr__(instance, "precondition_sha256", precondition_sha256)
        object.__setattr__(instance, "journal_head_sequence", journal_head_sequence)
        object.__setattr__(instance, "journal_head_digest", journal_head_digest)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if (
            type(self.attempt) is not MutationAttempt
            or type(self.reservation_proof) is not MutationReservationProof
            or type(self.reserved_request) is not ReservedRequest
            or type(self.intent_binding) is not IntentChainBinding
            or self.precondition_sha256 != self.reservation_proof.precondition_sha256
            or not _is_positive_int(self.journal_head_sequence)
            or not _is_sha256(self.journal_head_digest)
        ):
            raise ExecutionJournalError("INVALID_VERIFIED_DISPATCH_RECEIPT")
        self.reservation_proof.validate_dispatch_binding(
            self.reserved_request,
            self.attempt,
        )


@dataclass(frozen=True, slots=True)
class RecoveryDirective:
    """Fixed read-first action for one durably UNKNOWN mutation attempt."""

    source_attempt_id: str
    source_generation: int
    kind: MutationKind
    mode: RecoveryMode
    query_client_id: str
    source_authorization_id: str
    source_intent_sha256: str
    source_runtime_commit: str
    source_session_nonce: str

    def __post_init__(self) -> None:
        expected_mode = {
            MutationKind.CREATE: RecoveryMode.QUERY_PROBE_CLIENT_ID,
            MutationKind.CANCEL: RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL,
            MutationKind.EMERGENCY_CLOSE: RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
        }
        if (
            not _is_sha256(self.source_attempt_id)
            or not _is_positive_int(self.source_generation)
            or type(self.kind) is not MutationKind
            or type(self.mode) is not RecoveryMode
            or self.mode is not expected_mode[self.kind]
            or type(self.query_client_id) is not str
            or _CLIENT_ID.fullmatch(self.query_client_id) is None
            or type(self.source_authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.source_authorization_id) is None
            or not _is_sha256(self.source_intent_sha256)
            or type(self.source_runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_runtime_commit) is None
            or type(self.source_session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.source_session_nonce) is None
        ):
            raise ExecutionJournalError("INVALID_RECOVERY_DIRECTIVE")

    @property
    def allows_post_create(self) -> bool:
        return False

    @property
    def allows_blind_retry(self) -> bool:
        return False

    @property
    def queries_terminal_state(self) -> bool:
        return self.kind is MutationKind.CANCEL

    @property
    def requires_fresh_open_proof(self) -> bool:
        return self.kind in {MutationKind.CREATE, MutationKind.CANCEL}

    @property
    def allows_conditional_cleanup_cancel(self) -> bool:
        return self.kind in {MutationKind.CREATE, MutationKind.CANCEL}

    @property
    def allows_first_owned_fill_cleanup_close(self) -> bool:
        """A CREATE/CANCEL UNKNOWN may authorize one new proof-bound close."""

        return self.kind in {MutationKind.CREATE, MutationKind.CANCEL}

    @property
    def requires_owned_fill_proof_for_close(self) -> bool:
        return self.allows_first_owned_fill_cleanup_close

    @property
    def requires_fresh_position_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE

    @property
    def requires_fresh_order_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE

    @property
    def requires_fresh_trade_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE


@dataclass(frozen=True, slots=True)
class _JournalCreated:
    pass


@dataclass(frozen=True, slots=True)
class _GenerationAdmitted:
    generation: int
    capability: GenerationCapability
    process_identity_sha256: str


@dataclass(frozen=True, slots=True)
class _StagedGenerationReconciled:
    proof: StagedGenerationRecoveryProof


@dataclass(frozen=True, slots=True)
class _SessionAuthorityEstablished:
    authority: SessionAuthority


@dataclass(frozen=True, slots=True)
class _RecoverySessionAuthorityIssued:
    authority: RecoverySessionAuthority


@dataclass(frozen=True, slots=True)
class _IntentBoundRecoveryAuthorityIssued:
    authority: IntentBoundRecoveryAuthority


@dataclass(frozen=True, slots=True)
class _PreIntentReadReserved:
    reservation: PreIntentReadReservation


@dataclass(frozen=True, slots=True)
class _PreIntentReadResultRecorded:
    result: PreIntentReadResult


@dataclass(frozen=True, slots=True)
class _PreIntentReadFailureRecorded:
    failure: PreIntentReadFailure


@dataclass(frozen=True, slots=True)
class _IntentChainBound:
    binding: IntentChainBinding


@dataclass(frozen=True, slots=True)
class _ExactRequestReserved:
    authority_sha256: str
    generation: int
    deadline_ns: int
    reserved_request: ReservedRequest


@dataclass(frozen=True, slots=True)
class _MutationReserved:
    proof: MutationReservationProof


@dataclass(frozen=True, slots=True)
class _ReadPrepared:
    proof: ReadReservationProof


@dataclass(frozen=True, slots=True)
class _ReadResultRecorded:
    proof: ReadResultProof


@dataclass(frozen=True, slots=True)
class _ExactReadFailureRecorded:
    failure: ExactReadFailure


@dataclass(frozen=True, slots=True)
class _OwnedFillCloseProven:
    proof: OwnedFillCloseProof


@dataclass(frozen=True, slots=True)
class _AttemptPrepared:
    attempt: MutationAttempt


@dataclass(frozen=True, slots=True)
class _GoDurable:
    attempt_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class _AttemptConfirmed:
    attempt_id: str
    generation: int
    result_sha256: str


@dataclass(frozen=True, slots=True)
class _GenerationReaped:
    receipt: ProcessReapReceipt


@dataclass(frozen=True, slots=True)
class _ReconciliationObserved:
    observation: ReconciliationObservation


@dataclass(frozen=True, slots=True)
class _AttemptResolved:
    attempt_id: str
    generation: int
    state: FrontierState
    boundary_result: BoundaryResult


_JournalEvent = (
    _JournalCreated
    | _GenerationAdmitted
    | _StagedGenerationReconciled
    | _SessionAuthorityEstablished
    | _RecoverySessionAuthorityIssued
    | _IntentBoundRecoveryAuthorityIssued
    | _PreIntentReadReserved
    | _PreIntentReadResultRecorded
    | _PreIntentReadFailureRecorded
    | _IntentChainBound
    | _ExactRequestReserved
    | _MutationReserved
    | _ReadPrepared
    | _ReadResultRecorded
    | _ExactReadFailureRecorded
    | _OwnedFillCloseProven
    | _AttemptPrepared
    | _GoDurable
    | _AttemptConfirmed
    | _GenerationReaped
    | _ReconciliationObserved
    | _AttemptResolved
)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One verified record from the canonical hash chain."""

    schema_version: str
    sequence: int
    previous_digest: str
    digest: str
    event: _JournalEvent


def _attempt_to_mapping(attempt: MutationAttempt) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "authorization_id": attempt.authorization_id,
        "client_id": attempt.client_id,
        "deadline_ns": attempt.deadline_ns,
        "fresh_open_proof_sha256": attempt.fresh_open_proof_sha256,
        "generation": attempt.generation,
        "intent_sha256": attempt.intent_sha256,
        "kind": attempt.kind.value,
        "reconciliation_key": {
            "client_id": attempt.reconciliation_key.client_id,
            "kind": attempt.reconciliation_key.kind.value,
        },
        "recovery_of_attempt_id": attempt.recovery_of_attempt_id,
        "reservation_sha256": attempt.reservation_sha256,
        "retry_index": attempt.retry_index,
        "runtime_commit": attempt.runtime_commit,
        "session_nonce": attempt.session_nonce,
    }


_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_id",
        "authorization_id",
        "client_id",
        "deadline_ns",
        "fresh_open_proof_sha256",
        "generation",
        "intent_sha256",
        "kind",
        "reconciliation_key",
        "recovery_of_attempt_id",
        "reservation_sha256",
        "retry_index",
        "runtime_commit",
        "session_nonce",
    }
)


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    error: str,
) -> Mapping[str, object]:
    if type(value) is not dict or frozenset(value) != expected:
        raise ExecutionJournalError(error)
    return value


def _enum(enum_type: type[StrEnum], value: object) -> StrEnum:
    if type(value) is not str:
        raise ExecutionJournalError("JOURNAL_EVENT_VALUE")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ExecutionJournalError("JOURNAL_EVENT_VALUE") from exc


def _attempt_from_mapping(value: object) -> MutationAttempt:
    item = _require_exact_fields(value, _ATTEMPT_FIELDS, "JOURNAL_ATTEMPT_FIELDS")
    key_item = _require_exact_fields(
        item["reconciliation_key"],
        frozenset({"kind", "client_id"}),
        "JOURNAL_RECONCILIATION_FIELDS",
    )
    return MutationAttempt(
        attempt_id=item["attempt_id"],  # type: ignore[arg-type]
        kind=_enum(MutationKind, item["kind"]),  # type: ignore[arg-type]
        client_id=item["client_id"],  # type: ignore[arg-type]
        reconciliation_key=ReconciliationKey(
            kind=_enum(ReconciliationKeyKind, key_item["kind"]),  # type: ignore[arg-type]
            client_id=key_item["client_id"],  # type: ignore[arg-type]
        ),
        retry_index=item["retry_index"],  # type: ignore[arg-type]
        deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        reservation_sha256=item["reservation_sha256"],  # type: ignore[arg-type]
        authorization_id=item["authorization_id"],  # type: ignore[arg-type]
        intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
        runtime_commit=item["runtime_commit"],  # type: ignore[arg-type]
        session_nonce=item["session_nonce"],  # type: ignore[arg-type]
        fresh_open_proof_sha256=item["fresh_open_proof_sha256"],  # type: ignore[arg-type]
        recovery_of_attempt_id=item["recovery_of_attempt_id"],  # type: ignore[arg-type]
    )


_OBSERVATION_FIELDS = frozenset(
    {
        "observation_sha256",
        "source_attempt_id",
        "source_authorization_id",
        "source_client_id",
        "generation",
        "order_status",
        "read_reservation_sha256",
        "read_result_proof_sha256",
        "read_result_record_sequence",
        "read_result_record_digest",
    }
)


def _observation_to_mapping(observation: ReconciliationObservation) -> dict[str, object]:
    return {
        "observation_sha256": observation.observation_sha256,
        "source_attempt_id": observation.source_attempt_id,
        "source_authorization_id": observation.source_authorization_id,
        "source_client_id": observation.source_client_id,
        "generation": observation.generation,
        "order_status": observation.order_status.value,
        "read_reservation_sha256": observation.read_reservation_sha256,
        "read_result_proof_sha256": observation.read_result_proof_sha256,
        "read_result_record_sequence": observation.read_result_record_sequence,
        "read_result_record_digest": observation.read_result_record_digest,
    }


def _observation_from_mapping(value: object) -> ReconciliationObservation:
    item = _require_exact_fields(
        value,
        _OBSERVATION_FIELDS,
        "JOURNAL_OBSERVATION_FIELDS",
    )
    return ReconciliationObservation(
        observation_sha256=item["observation_sha256"],  # type: ignore[arg-type]
        source_attempt_id=item["source_attempt_id"],  # type: ignore[arg-type]
        source_authorization_id=item["source_authorization_id"],  # type: ignore[arg-type]
        source_client_id=item["source_client_id"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        order_status=_enum(ReconciledOrderStatus, item["order_status"]),  # type: ignore[arg-type]
        read_reservation_sha256=item["read_reservation_sha256"],  # type: ignore[arg-type]
        read_result_proof_sha256=item["read_result_proof_sha256"],  # type: ignore[arg-type]
        read_result_record_sequence=item["read_result_record_sequence"],  # type: ignore[arg-type]
        read_result_record_digest=item["read_result_record_digest"],  # type: ignore[arg-type]
    )


_LEDGER_FIELDS = frozenset(
    {
        "cancel_requests",
        "create_requests",
        "emergency_close_requests",
        "last_elapsed_seconds",
        "post_create_read_requests",
        "read_retry_requests",
        "retryable_read_sha256",
        "stage",
        "total_http_requests",
    }
)


def _ledger_from_mapping(value: object) -> MutationLedger:
    item = _require_exact_fields(value, _LEDGER_FIELDS, "JOURNAL_REQUEST_LEDGER_FIELDS")
    try:
        return MutationLedger(
            total_http_requests=item["total_http_requests"],  # type: ignore[arg-type]
            create_requests=item["create_requests"],  # type: ignore[arg-type]
            cancel_requests=item["cancel_requests"],  # type: ignore[arg-type]
            emergency_close_requests=item["emergency_close_requests"],  # type: ignore[arg-type]
            read_retry_requests=item["read_retry_requests"],  # type: ignore[arg-type]
            post_create_read_requests=item["post_create_read_requests"],  # type: ignore[arg-type]
            stage=_enum(RequestStage, item["stage"]),  # type: ignore[arg-type]
            last_elapsed_seconds=_parse_canonical_decimal(item["last_elapsed_seconds"]),
            retryable_read_sha256=item["retryable_read_sha256"],  # type: ignore[arg-type]
        )
    except MutationProtocolError as exc:
        raise ExecutionJournalError("JOURNAL_REQUEST_LEDGER_INVALID") from exc


_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "logical_request_sha256",
        "ledger",
        "intent_sha256",
        "origin",
        "method",
        "path",
        "purpose",
        "parameters",
        "elapsed_seconds",
        "retry_index",
    }
)


def _reserved_request_to_mapping(reserved: ReservedRequest) -> dict[str, object]:
    if type(reserved) is not ReservedRequest:
        raise ExecutionJournalError("INVALID_RESERVED_REQUEST")
    return {
        "schema_version": _EXACT_REQUEST_RESERVATION_SCHEMA,
        "request_sha256": reserved.request_sha256,
        "logical_request_sha256": reserved.logical_request_sha256,
        "ledger": _ledger_material(reserved.ledger),
        "intent_sha256": reserved.intent_sha256,
        "origin": reserved.origin,
        "method": reserved.method,
        "path": reserved.path,
        "purpose": reserved.purpose.value,
        "parameters": dict(reserved.parameters),
        "elapsed_seconds": format(reserved.elapsed_seconds, "f"),
        "retry_index": reserved.retry_index,
    }


def _reserved_request_from_mapping(value: object) -> ReservedRequest:
    item = _require_exact_fields(
        value,
        _RESERVED_REQUEST_FIELDS,
        "JOURNAL_RESERVED_REQUEST_FIELDS",
    )
    parameters = item["parameters"]
    if (
        item["schema_version"] != _EXACT_REQUEST_RESERVATION_SCHEMA
        or type(parameters) is not dict
        or any(
            type(key) is not str or type(field_value) is not str
            for key, field_value in parameters.items()
        )
    ):
        raise ExecutionJournalError("JOURNAL_RESERVED_REQUEST_FIELDS")
    try:
        reserved = ReservedRequest(
            ledger=_ledger_from_mapping(item["ledger"]),
            intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
            origin=item["origin"],  # type: ignore[arg-type]
            method=item["method"],  # type: ignore[arg-type]
            path=item["path"],  # type: ignore[arg-type]
            purpose=_enum(RequestPurpose, item["purpose"]),  # type: ignore[arg-type]
            parameters=tuple(sorted(parameters.items())),
            elapsed_seconds=_parse_canonical_decimal(item["elapsed_seconds"]),
            retry_index=item["retry_index"],  # type: ignore[arg-type]
        )
    except MutationProtocolError as exc:
        raise ExecutionJournalError("JOURNAL_RESERVED_REQUEST_INVALID") from exc
    if (
        item["request_sha256"] != reserved.request_sha256
        or item["logical_request_sha256"] != reserved.logical_request_sha256
    ):
        raise ExecutionJournalError("JOURNAL_RESERVED_REQUEST_DIGEST")
    return reserved


_SESSION_AUTHORITY_FIELDS = frozenset(
    {
        "authority_sha256",
        "authorization_id",
        "runtime_commit",
        "session_nonce",
        "generation",
        "client_id",
    }
)


def _session_authority_to_mapping(authority: SessionAuthority) -> dict[str, object]:
    return {
        "authority_sha256": authority.authority_sha256,
        "authorization_id": authority.authorization_id,
        "runtime_commit": authority.runtime_commit,
        "session_nonce": authority.session_nonce,
        "generation": authority.generation,
        "client_id": authority.client_id,
    }


def _session_authority_from_mapping(value: object) -> SessionAuthority:
    item = _require_exact_fields(
        value,
        _SESSION_AUTHORITY_FIELDS,
        "JOURNAL_SESSION_AUTHORITY_FIELDS",
    )
    return SessionAuthority(
        authority_sha256=item["authority_sha256"],  # type: ignore[arg-type]
        authorization_id=item["authorization_id"],  # type: ignore[arg-type]
        runtime_commit=item["runtime_commit"],  # type: ignore[arg-type]
        session_nonce=item["session_nonce"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        client_id=item["client_id"],  # type: ignore[arg-type]
    )


_RECOVERY_SESSION_AUTHORITY_FIELDS = frozenset(
    {
        "authority_sha256",
        "primary_authority_sha256",
        "source_attempt_id",
        "source_generation",
        "source_kind",
        "source_authorization_id",
        "source_intent_sha256",
        "source_runtime_commit",
        "source_session_nonce",
        "source_client_id",
        "generation",
    }
)


def _recovery_session_authority_to_mapping(
    authority: RecoverySessionAuthority,
) -> dict[str, object]:
    return {
        "authority_sha256": authority.authority_sha256,
        "primary_authority_sha256": authority.primary_authority_sha256,
        "source_attempt_id": authority.source_attempt_id,
        "source_generation": authority.source_generation,
        "source_kind": authority.source_kind.value,
        "source_authorization_id": authority.source_authorization_id,
        "source_intent_sha256": authority.source_intent_sha256,
        "source_runtime_commit": authority.source_runtime_commit,
        "source_session_nonce": authority.source_session_nonce,
        "source_client_id": authority.source_client_id,
        "generation": authority.generation,
    }


def _recovery_session_authority_from_mapping(
    value: object,
) -> RecoverySessionAuthority:
    item = _require_exact_fields(
        value,
        _RECOVERY_SESSION_AUTHORITY_FIELDS,
        "JOURNAL_RECOVERY_SESSION_AUTHORITY_FIELDS",
    )
    return RecoverySessionAuthority(
        authority_sha256=item["authority_sha256"],  # type: ignore[arg-type]
        primary_authority_sha256=item["primary_authority_sha256"],  # type: ignore[arg-type]
        source_attempt_id=item["source_attempt_id"],  # type: ignore[arg-type]
        source_generation=item["source_generation"],  # type: ignore[arg-type]
        source_kind=_enum(MutationKind, item["source_kind"]),  # type: ignore[arg-type]
        source_authorization_id=item["source_authorization_id"],  # type: ignore[arg-type]
        source_intent_sha256=item["source_intent_sha256"],  # type: ignore[arg-type]
        source_runtime_commit=item["source_runtime_commit"],  # type: ignore[arg-type]
        source_session_nonce=item["source_session_nonce"],  # type: ignore[arg-type]
        source_client_id=item["source_client_id"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
    )


_INTENT_BOUND_RECOVERY_AUTHORITY_FIELDS = frozenset(
    {
        "authority_sha256",
        "primary_authority_sha256",
        "intent_binding_sha256",
        "source_generation",
        "source_authorization_id",
        "source_intent_sha256",
        "source_runtime_commit",
        "source_session_nonce",
        "query_client_id",
        "generation",
        "abandoned_create_request_sha256",
    }
)


def _intent_bound_recovery_authority_to_mapping(
    authority: IntentBoundRecoveryAuthority,
) -> dict[str, object]:
    return {
        "authority_sha256": authority.authority_sha256,
        "primary_authority_sha256": authority.primary_authority_sha256,
        "intent_binding_sha256": authority.intent_binding_sha256,
        "source_generation": authority.source_generation,
        "source_authorization_id": authority.source_authorization_id,
        "source_intent_sha256": authority.source_intent_sha256,
        "source_runtime_commit": authority.source_runtime_commit,
        "source_session_nonce": authority.source_session_nonce,
        "query_client_id": authority.query_client_id,
        "generation": authority.generation,
        "abandoned_create_request_sha256": (authority.abandoned_create_request_sha256),
    }


def _intent_bound_recovery_authority_from_mapping(
    value: object,
) -> IntentBoundRecoveryAuthority:
    item = _require_exact_fields(
        value,
        _INTENT_BOUND_RECOVERY_AUTHORITY_FIELDS,
        "JOURNAL_INTENT_BOUND_RECOVERY_AUTHORITY_FIELDS",
    )
    return IntentBoundRecoveryAuthority(
        authority_sha256=item["authority_sha256"],  # type: ignore[arg-type]
        primary_authority_sha256=item["primary_authority_sha256"],  # type: ignore[arg-type]
        intent_binding_sha256=item["intent_binding_sha256"],  # type: ignore[arg-type]
        source_generation=item["source_generation"],  # type: ignore[arg-type]
        source_authorization_id=item["source_authorization_id"],  # type: ignore[arg-type]
        source_intent_sha256=item["source_intent_sha256"],  # type: ignore[arg-type]
        source_runtime_commit=item["source_runtime_commit"],  # type: ignore[arg-type]
        source_session_nonce=item["source_session_nonce"],  # type: ignore[arg-type]
        query_client_id=item["query_client_id"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        abandoned_create_request_sha256=item[  # type: ignore[arg-type]
            "abandoned_create_request_sha256"
        ],
    )


_PRE_INTENT_RESERVATION_FIELDS = frozenset(
    {
        "reservation_sha256",
        "logical_request_sha256",
        "session_authority_sha256",
        "generation",
        "deadline_ns",
        "origin",
        "method",
        "path",
        "purpose",
        "parameters",
        "ledger",
        "elapsed_seconds",
        "retry_index",
    }
)


def _pre_intent_reservation_to_mapping(
    reservation: PreIntentReadReservation,
) -> dict[str, object]:
    return {
        "reservation_sha256": reservation.reservation_sha256,
        "logical_request_sha256": reservation.logical_request_sha256,
        "session_authority_sha256": reservation.session_authority_sha256,
        "generation": reservation.generation,
        "deadline_ns": reservation.deadline_ns,
        "origin": reservation.origin,
        "method": reservation.method,
        "path": reservation.path,
        "purpose": reservation.purpose.value,
        "parameters": dict(reservation.parameters),
        "ledger": _ledger_material(reservation.ledger),
        "elapsed_seconds": format(reservation.elapsed_seconds, "f"),
        "retry_index": reservation.retry_index,
    }


def _pre_intent_reservation_from_mapping(value: object) -> PreIntentReadReservation:
    item = _require_exact_fields(
        value,
        _PRE_INTENT_RESERVATION_FIELDS,
        "JOURNAL_PRE_INTENT_RESERVATION_FIELDS",
    )
    parameters = item["parameters"]
    if type(parameters) is not dict or any(
        type(key) is not str or type(field_value) is not str
        for key, field_value in parameters.items()
    ):
        raise ExecutionJournalError("JOURNAL_PRE_INTENT_RESERVATION_FIELDS")
    return PreIntentReadReservation(
        reservation_sha256=item["reservation_sha256"],  # type: ignore[arg-type]
        logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
        session_authority_sha256=item["session_authority_sha256"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
        origin=item["origin"],  # type: ignore[arg-type]
        method=item["method"],  # type: ignore[arg-type]
        path=item["path"],  # type: ignore[arg-type]
        purpose=_enum(RequestPurpose, item["purpose"]),  # type: ignore[arg-type]
        parameters=tuple(sorted(parameters.items())),
        ledger=_ledger_from_mapping(item["ledger"]),
        elapsed_seconds=_parse_canonical_decimal(item["elapsed_seconds"]),
        retry_index=item["retry_index"],  # type: ignore[arg-type]
    )


_PRE_INTENT_RESULT_FIELDS = frozenset(
    {
        "result_proof_sha256",
        "reservation_sha256",
        "prepared_record_sequence",
        "prepared_record_digest",
        "result_sha256",
        "observed_at_ns",
    }
)


def _pre_intent_result_to_mapping(result: PreIntentReadResult) -> dict[str, object]:
    return {
        "result_proof_sha256": result.result_proof_sha256,
        "reservation_sha256": result.reservation_sha256,
        "prepared_record_sequence": result.prepared_record_sequence,
        "prepared_record_digest": result.prepared_record_digest,
        "result_sha256": result.result_sha256,
        "observed_at_ns": result.observed_at_ns,
    }


def _pre_intent_result_from_mapping(value: object) -> PreIntentReadResult:
    item = _require_exact_fields(
        value,
        _PRE_INTENT_RESULT_FIELDS,
        "JOURNAL_PRE_INTENT_RESULT_FIELDS",
    )
    return PreIntentReadResult(
        result_proof_sha256=item["result_proof_sha256"],  # type: ignore[arg-type]
        reservation_sha256=item["reservation_sha256"],  # type: ignore[arg-type]
        prepared_record_sequence=item["prepared_record_sequence"],  # type: ignore[arg-type]
        prepared_record_digest=item["prepared_record_digest"],  # type: ignore[arg-type]
        result_sha256=item["result_sha256"],  # type: ignore[arg-type]
        observed_at_ns=item["observed_at_ns"],  # type: ignore[arg-type]
    )


_PRE_INTENT_FAILURE_FIELDS = frozenset(
    {
        "failure_proof_sha256",
        "reservation_sha256",
        "prepared_record_sequence",
        "prepared_record_digest",
        "failure",
        "io_may_have_occurred",
        "observed_at_ns",
    }
)


def _pre_intent_failure_to_mapping(failure: PreIntentReadFailure) -> dict[str, object]:
    return {
        "failure_proof_sha256": failure.failure_proof_sha256,
        "reservation_sha256": failure.reservation_sha256,
        "prepared_record_sequence": failure.prepared_record_sequence,
        "prepared_record_digest": failure.prepared_record_digest,
        "failure": failure.failure.value,
        "io_may_have_occurred": failure.io_may_have_occurred,
        "observed_at_ns": failure.observed_at_ns,
    }


def _pre_intent_failure_from_mapping(value: object) -> PreIntentReadFailure:
    item = _require_exact_fields(
        value,
        _PRE_INTENT_FAILURE_FIELDS,
        "JOURNAL_PRE_INTENT_FAILURE_FIELDS",
    )
    return PreIntentReadFailure(
        failure_proof_sha256=item["failure_proof_sha256"],  # type: ignore[arg-type]
        reservation_sha256=item["reservation_sha256"],  # type: ignore[arg-type]
        prepared_record_sequence=item["prepared_record_sequence"],  # type: ignore[arg-type]
        prepared_record_digest=item["prepared_record_digest"],  # type: ignore[arg-type]
        failure=_enum(ReadFailureKind, item["failure"]),  # type: ignore[arg-type]
        io_may_have_occurred=item["io_may_have_occurred"],  # type: ignore[arg-type]
        observed_at_ns=item["observed_at_ns"],  # type: ignore[arg-type]
    )


_EXACT_READ_FAILURE_FIELDS = frozenset(
    {
        "failure_proof_sha256",
        "request_sha256",
        "read_proof_sha256",
        "prepared_record_sequence",
        "prepared_record_digest",
        "generation",
        "monotonic_sequence",
        "failure",
        "io_may_have_occurred",
        "observed_at_ns",
    }
)


def _exact_read_failure_to_mapping(failure: ExactReadFailure) -> dict[str, object]:
    return {
        "failure_proof_sha256": failure.failure_proof_sha256,
        "request_sha256": failure.request_sha256,
        "read_proof_sha256": failure.read_proof_sha256,
        "prepared_record_sequence": failure.prepared_record_sequence,
        "prepared_record_digest": failure.prepared_record_digest,
        "generation": failure.generation,
        "monotonic_sequence": failure.monotonic_sequence,
        "failure": failure.failure.value,
        "io_may_have_occurred": failure.io_may_have_occurred,
        "observed_at_ns": failure.observed_at_ns,
    }


def _exact_read_failure_from_mapping(value: object) -> ExactReadFailure:
    item = _require_exact_fields(
        value,
        _EXACT_READ_FAILURE_FIELDS,
        "JOURNAL_EXACT_READ_FAILURE_FIELDS",
    )
    return ExactReadFailure(
        failure_proof_sha256=item["failure_proof_sha256"],  # type: ignore[arg-type]
        request_sha256=item["request_sha256"],  # type: ignore[arg-type]
        read_proof_sha256=item["read_proof_sha256"],  # type: ignore[arg-type]
        prepared_record_sequence=item["prepared_record_sequence"],  # type: ignore[arg-type]
        prepared_record_digest=item["prepared_record_digest"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        monotonic_sequence=item["monotonic_sequence"],  # type: ignore[arg-type]
        failure=_enum(ReadFailureKind, item["failure"]),  # type: ignore[arg-type]
        io_may_have_occurred=item["io_may_have_occurred"],  # type: ignore[arg-type]
        observed_at_ns=item["observed_at_ns"],  # type: ignore[arg-type]
    )


_INTENT_CHAIN_BINDING_FIELDS = frozenset(
    {
        "binding_sha256",
        "session_authority_sha256",
        "intent_sha256",
        "intent_file_sha256",
        "intent_path_sha256",
        "pre_intent_chain_sha256",
        "last_ledger_sha256",
    }
)


def _intent_chain_binding_to_mapping(binding: IntentChainBinding) -> dict[str, object]:
    return {
        "binding_sha256": binding.binding_sha256,
        "session_authority_sha256": binding.session_authority_sha256,
        "intent_sha256": binding.intent_sha256,
        "intent_file_sha256": binding.intent_file_sha256,
        "intent_path_sha256": binding.intent_path_sha256,
        "pre_intent_chain_sha256": binding.pre_intent_chain_sha256,
        "last_ledger_sha256": binding.last_ledger_sha256,
    }


def _intent_chain_binding_from_mapping(value: object) -> IntentChainBinding:
    item = _require_exact_fields(
        value,
        _INTENT_CHAIN_BINDING_FIELDS,
        "JOURNAL_INTENT_CHAIN_BINDING_FIELDS",
    )
    return IntentChainBinding(
        binding_sha256=item["binding_sha256"],  # type: ignore[arg-type]
        session_authority_sha256=item["session_authority_sha256"],  # type: ignore[arg-type]
        intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
        intent_file_sha256=item["intent_file_sha256"],  # type: ignore[arg-type]
        intent_path_sha256=item["intent_path_sha256"],  # type: ignore[arg-type]
        pre_intent_chain_sha256=item["pre_intent_chain_sha256"],  # type: ignore[arg-type]
        last_ledger_sha256=item["last_ledger_sha256"],  # type: ignore[arg-type]
    )


_MUTATION_RESERVATION_FIELDS = frozenset(
    {
        "request_sha256",
        "proof_sha256",
        "logical_request_sha256",
        "kind",
        "purpose",
        "method",
        "path",
        "retry_index",
        "client_id",
        "authorization_id",
        "intent_sha256",
        "generation",
        "deadline_ns",
        "monotonic_sequence",
        "parameters_sha256",
        "ledger_sha256",
        "source_attempt_id",
        "precondition_sha256",
    }
)


def _mutation_reservation_to_mapping(
    proof: MutationReservationProof,
) -> dict[str, object]:
    return {
        "request_sha256": proof.request_sha256,
        "proof_sha256": proof.proof_sha256,
        "logical_request_sha256": proof.logical_request_sha256,
        "kind": proof.kind.value,
        "purpose": proof.purpose.value,
        "method": proof.method,
        "path": proof.path,
        "retry_index": proof.retry_index,
        "client_id": proof.client_id,
        "authorization_id": proof.authorization_id,
        "intent_sha256": proof.intent_sha256,
        "generation": proof.generation,
        "deadline_ns": proof.deadline_ns,
        "monotonic_sequence": proof.monotonic_sequence,
        "parameters_sha256": proof.parameters_sha256,
        "ledger_sha256": proof.ledger_sha256,
        "source_attempt_id": proof.source_attempt_id,
        "precondition_sha256": proof.precondition_sha256,
    }


def _mutation_reservation_from_mapping(value: object) -> MutationReservationProof:
    item = _require_exact_fields(
        value,
        _MUTATION_RESERVATION_FIELDS,
        "JOURNAL_MUTATION_RESERVATION_FIELDS",
    )
    return MutationReservationProof(
        request_sha256=item["request_sha256"],  # type: ignore[arg-type]
        proof_sha256=item["proof_sha256"],  # type: ignore[arg-type]
        logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
        kind=_enum(MutationKind, item["kind"]),  # type: ignore[arg-type]
        purpose=_enum(MutationPurpose, item["purpose"]),  # type: ignore[arg-type]
        method=item["method"],  # type: ignore[arg-type]
        path=item["path"],  # type: ignore[arg-type]
        retry_index=item["retry_index"],  # type: ignore[arg-type]
        client_id=item["client_id"],  # type: ignore[arg-type]
        authorization_id=item["authorization_id"],  # type: ignore[arg-type]
        intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
        monotonic_sequence=item["monotonic_sequence"],  # type: ignore[arg-type]
        parameters_sha256=item["parameters_sha256"],  # type: ignore[arg-type]
        ledger_sha256=item["ledger_sha256"],  # type: ignore[arg-type]
        source_attempt_id=item["source_attempt_id"],  # type: ignore[arg-type]
        precondition_sha256=item["precondition_sha256"],  # type: ignore[arg-type]
    )


_READ_RESERVATION_FIELDS = frozenset(
    {
        "request_sha256",
        "proof_sha256",
        "logical_request_sha256",
        "read_kind",
        "purpose",
        "method",
        "path",
        "retry_index",
        "generation",
        "deadline_ns",
        "monotonic_sequence",
        "parameters_sha256",
        "ledger_sha256",
        "source_attempt_id",
        "client_id",
        "authorization_id",
        "intent_sha256",
    }
)


def _read_reservation_to_mapping(proof: ReadReservationProof) -> dict[str, object]:
    return {
        "request_sha256": proof.request_sha256,
        "proof_sha256": proof.proof_sha256,
        "logical_request_sha256": proof.logical_request_sha256,
        "read_kind": proof.read_kind.value,
        "purpose": proof.purpose.value,
        "method": proof.method,
        "path": proof.path,
        "retry_index": proof.retry_index,
        "generation": proof.generation,
        "deadline_ns": proof.deadline_ns,
        "monotonic_sequence": proof.monotonic_sequence,
        "parameters_sha256": proof.parameters_sha256,
        "ledger_sha256": proof.ledger_sha256,
        "source_attempt_id": proof.source_attempt_id,
        "client_id": proof.client_id,
        "authorization_id": proof.authorization_id,
        "intent_sha256": proof.intent_sha256,
    }


def _read_reservation_from_mapping(value: object) -> ReadReservationProof:
    item = _require_exact_fields(
        value,
        _READ_RESERVATION_FIELDS,
        "JOURNAL_READ_RESERVATION_FIELDS",
    )
    return ReadReservationProof(
        request_sha256=item["request_sha256"],  # type: ignore[arg-type]
        proof_sha256=item["proof_sha256"],  # type: ignore[arg-type]
        logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
        read_kind=_enum(ReadKind, item["read_kind"]),  # type: ignore[arg-type]
        purpose=_enum(ReadPurpose, item["purpose"]),  # type: ignore[arg-type]
        method=item["method"],  # type: ignore[arg-type]
        path=item["path"],  # type: ignore[arg-type]
        retry_index=item["retry_index"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
        monotonic_sequence=item["monotonic_sequence"],  # type: ignore[arg-type]
        parameters_sha256=item["parameters_sha256"],  # type: ignore[arg-type]
        ledger_sha256=item["ledger_sha256"],  # type: ignore[arg-type]
        source_attempt_id=item["source_attempt_id"],  # type: ignore[arg-type]
        client_id=item["client_id"],  # type: ignore[arg-type]
        authorization_id=item["authorization_id"],  # type: ignore[arg-type]
        intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
    )


_READ_RESULT_FIELDS = frozenset(
    {
        "result_proof_sha256",
        "request_sha256",
        "prepared_record_sequence",
        "prepared_record_digest",
        "generation",
        "monotonic_sequence",
        "read_kind",
        "outcome",
        "result_sha256",
        "observed_at_ns",
    }
)


def _read_result_to_mapping(proof: ReadResultProof) -> dict[str, object]:
    return {
        "result_proof_sha256": proof.result_proof_sha256,
        "request_sha256": proof.request_sha256,
        "prepared_record_sequence": proof.prepared_record_sequence,
        "prepared_record_digest": proof.prepared_record_digest,
        "generation": proof.generation,
        "monotonic_sequence": proof.monotonic_sequence,
        "read_kind": proof.read_kind.value,
        "outcome": proof.outcome.value,
        "result_sha256": proof.result_sha256,
        "observed_at_ns": proof.observed_at_ns,
    }


def _read_result_from_mapping(value: object) -> ReadResultProof:
    item = _require_exact_fields(value, _READ_RESULT_FIELDS, "JOURNAL_READ_RESULT_FIELDS")
    return ReadResultProof(
        result_proof_sha256=item["result_proof_sha256"],  # type: ignore[arg-type]
        request_sha256=item["request_sha256"],  # type: ignore[arg-type]
        prepared_record_sequence=item["prepared_record_sequence"],  # type: ignore[arg-type]
        prepared_record_digest=item["prepared_record_digest"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        monotonic_sequence=item["monotonic_sequence"],  # type: ignore[arg-type]
        read_kind=_enum(ReadKind, item["read_kind"]),  # type: ignore[arg-type]
        outcome=_enum(ReadOutcome, item["outcome"]),  # type: ignore[arg-type]
        result_sha256=item["result_sha256"],  # type: ignore[arg-type]
        observed_at_ns=item["observed_at_ns"],  # type: ignore[arg-type]
    )


_READ_REFERENCE_FIELDS = frozenset(
    {
        "result_proof_sha256",
        "record_sequence",
        "record_digest",
        "request_sha256",
        "logical_request_sha256",
        "transport_result_sha256",
        "transport_kind",
    }
)
_OWNED_FILL_CLOSE_FIELDS = frozenset(
    {
        "proof_sha256",
        "source_attempt_id",
        "source_authorization_id",
        "source_intent_sha256",
        "source_runtime_commit",
        "source_session_nonce",
        "source_client_id",
        "generation",
        "residual_quantity",
        "owned_executed_quantity",
        "open_remainder_quantity",
        "other_activity_absent",
        "position_direction",
        "probe_terminal_status",
        "market_close_proof_sha256",
        "owned_position_proof_sha256",
        "filter_snapshot_sha256",
        "filter_contract_sha256",
        "mark_price",
        "mark_price_age_ms",
        "market_observed_elapsed_seconds",
        "observed_elapsed_seconds",
        "observed_after_http_attempt",
        "order_result",
        "trade_result",
        "account_result",
        "symbol_filter_result",
        "mark_price_result",
    }
)


def _read_reference_from_mapping(value: object) -> DurableReadResultReference:
    item = _require_exact_fields(
        value,
        _READ_REFERENCE_FIELDS,
        "JOURNAL_READ_RESULT_REFERENCE_FIELDS",
    )
    return DurableReadResultReference(
        result_proof_sha256=item["result_proof_sha256"],  # type: ignore[arg-type]
        record_sequence=item["record_sequence"],  # type: ignore[arg-type]
        record_digest=item["record_digest"],  # type: ignore[arg-type]
        request_sha256=item["request_sha256"],  # type: ignore[arg-type]
        logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
        transport_result_sha256=item["transport_result_sha256"],  # type: ignore[arg-type]
        transport_kind=item["transport_kind"],  # type: ignore[arg-type]
    )


def _owned_fill_close_to_mapping(proof: OwnedFillCloseProof) -> dict[str, object]:
    return {
        "proof_sha256": proof.proof_sha256,
        "source_attempt_id": proof.source_attempt_id,
        "source_authorization_id": proof.source_authorization_id,
        "source_intent_sha256": proof.source_intent_sha256,
        "source_runtime_commit": proof.source_runtime_commit,
        "source_session_nonce": proof.source_session_nonce,
        "source_client_id": proof.source_client_id,
        "generation": proof.generation,
        "residual_quantity": proof.residual_quantity,
        "owned_executed_quantity": proof.owned_executed_quantity,
        "open_remainder_quantity": proof.open_remainder_quantity,
        "other_activity_absent": proof.other_activity_absent,
        "position_direction": proof.position_direction,
        "probe_terminal_status": proof.probe_terminal_status,
        "market_close_proof_sha256": proof.market_close_proof_sha256,
        "owned_position_proof_sha256": proof.owned_position_proof_sha256,
        "filter_snapshot_sha256": proof.filter_snapshot_sha256,
        "filter_contract_sha256": proof.filter_contract_sha256,
        "mark_price": proof.mark_price,
        "mark_price_age_ms": proof.mark_price_age_ms,
        "market_observed_elapsed_seconds": proof.market_observed_elapsed_seconds,
        "observed_elapsed_seconds": proof.observed_elapsed_seconds,
        "observed_after_http_attempt": proof.observed_after_http_attempt,
        "order_result": _read_reference_material(proof.order_result),
        "trade_result": _read_reference_material(proof.trade_result),
        "account_result": _read_reference_material(proof.account_result),
        "symbol_filter_result": _read_reference_material(proof.symbol_filter_result),
        "mark_price_result": _read_reference_material(proof.mark_price_result),
    }


def _owned_fill_close_from_mapping(value: object) -> OwnedFillCloseProof:
    item = _require_exact_fields(
        value,
        _OWNED_FILL_CLOSE_FIELDS,
        "JOURNAL_OWNED_FILL_CLOSE_FIELDS",
    )
    return OwnedFillCloseProof(
        proof_sha256=item["proof_sha256"],  # type: ignore[arg-type]
        source_attempt_id=item["source_attempt_id"],  # type: ignore[arg-type]
        source_authorization_id=item["source_authorization_id"],  # type: ignore[arg-type]
        source_intent_sha256=item["source_intent_sha256"],  # type: ignore[arg-type]
        source_runtime_commit=item["source_runtime_commit"],  # type: ignore[arg-type]
        source_session_nonce=item["source_session_nonce"],  # type: ignore[arg-type]
        source_client_id=item["source_client_id"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        residual_quantity=item["residual_quantity"],  # type: ignore[arg-type]
        owned_executed_quantity=item["owned_executed_quantity"],  # type: ignore[arg-type]
        open_remainder_quantity=item["open_remainder_quantity"],  # type: ignore[arg-type]
        other_activity_absent=item["other_activity_absent"],  # type: ignore[arg-type]
        position_direction=item["position_direction"],  # type: ignore[arg-type]
        probe_terminal_status=item["probe_terminal_status"],  # type: ignore[arg-type]
        market_close_proof_sha256=item["market_close_proof_sha256"],  # type: ignore[arg-type]
        owned_position_proof_sha256=item["owned_position_proof_sha256"],  # type: ignore[arg-type]
        filter_snapshot_sha256=item["filter_snapshot_sha256"],  # type: ignore[arg-type]
        filter_contract_sha256=item["filter_contract_sha256"],  # type: ignore[arg-type]
        mark_price=item["mark_price"],  # type: ignore[arg-type]
        mark_price_age_ms=item["mark_price_age_ms"],  # type: ignore[arg-type]
        market_observed_elapsed_seconds=item[  # type: ignore[arg-type]
            "market_observed_elapsed_seconds"
        ],
        observed_elapsed_seconds=item["observed_elapsed_seconds"],  # type: ignore[arg-type]
        observed_after_http_attempt=item["observed_after_http_attempt"],  # type: ignore[arg-type]
        order_result=_read_reference_from_mapping(item["order_result"]),
        trade_result=_read_reference_from_mapping(item["trade_result"]),
        account_result=_read_reference_from_mapping(item["account_result"]),
        symbol_filter_result=_read_reference_from_mapping(item["symbol_filter_result"]),
        mark_price_result=_read_reference_from_mapping(item["mark_price_result"]),
    )


_STAGED_GENERATION_RECOVERY_FIELDS = frozenset(
    {
        "proof_sha256",
        "generation",
        "capability",
        "process_identity_sha256",
        "process_staged_record_sequence",
        "process_staged_record_digest",
        "process_reaped_record_sequence",
        "process_reaped_record_digest",
        "reap_attestation_sha256",
        "returncode",
        "signal",
        "local_process_quiesced",
        "venue_mutation_absent_proven",
    }
)


def _staged_generation_recovery_to_mapping(
    proof: StagedGenerationRecoveryProof,
) -> dict[str, object]:
    return {
        "proof_sha256": proof.proof_sha256,
        "generation": proof.generation,
        "capability": proof.capability.value,
        "process_identity_sha256": proof.process_identity_sha256,
        "process_staged_record_sequence": proof.process_staged_record_sequence,
        "process_staged_record_digest": proof.process_staged_record_digest,
        "process_reaped_record_sequence": proof.process_reaped_record_sequence,
        "process_reaped_record_digest": proof.process_reaped_record_digest,
        "reap_attestation_sha256": proof.reap_attestation_sha256,
        "returncode": proof.returncode,
        "signal": proof.signal,
        "local_process_quiesced": proof.local_process_quiesced,
        "venue_mutation_absent_proven": proof.venue_mutation_absent_proven,
    }


def _staged_generation_recovery_from_mapping(
    value: object,
) -> StagedGenerationRecoveryProof:
    item = _require_exact_fields(
        value,
        _STAGED_GENERATION_RECOVERY_FIELDS,
        "JOURNAL_STAGED_GENERATION_RECOVERY_FIELDS",
    )
    return StagedGenerationRecoveryProof(
        proof_sha256=item["proof_sha256"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        capability=_enum(GenerationCapability, item["capability"]),  # type: ignore[arg-type]
        process_identity_sha256=item["process_identity_sha256"],  # type: ignore[arg-type]
        process_staged_record_sequence=item["process_staged_record_sequence"],  # type: ignore[arg-type]
        process_staged_record_digest=item["process_staged_record_digest"],  # type: ignore[arg-type]
        process_reaped_record_sequence=item["process_reaped_record_sequence"],  # type: ignore[arg-type]
        process_reaped_record_digest=item["process_reaped_record_digest"],  # type: ignore[arg-type]
        reap_attestation_sha256=item["reap_attestation_sha256"],  # type: ignore[arg-type]
        returncode=item["returncode"],  # type: ignore[arg-type]
        signal=item["signal"],  # type: ignore[arg-type]
        local_process_quiesced=item["local_process_quiesced"],  # type: ignore[arg-type]
        venue_mutation_absent_proven=item["venue_mutation_absent_proven"],  # type: ignore[arg-type]
    )


def _event_to_mapping(event: _JournalEvent) -> dict[str, object]:
    if type(event) is _JournalCreated:
        return {"type": "JOURNAL_CREATED"}
    if type(event) is _GenerationAdmitted:
        return {
            "type": "GENERATION_ADMITTED",
            "generation": event.generation,
            "capability": event.capability.value,
            "process_identity_sha256": event.process_identity_sha256,
        }
    if type(event) is _StagedGenerationReconciled:
        return {
            "type": "STAGED_GENERATION_RECONCILED",
            **_staged_generation_recovery_to_mapping(event.proof),
        }
    if type(event) is _SessionAuthorityEstablished:
        return {
            "type": "SESSION_AUTHORITY_ESTABLISHED",
            **_session_authority_to_mapping(event.authority),
        }
    if type(event) is _RecoverySessionAuthorityIssued:
        return {
            "type": "RECOVERY_SESSION_AUTHORITY_ISSUED",
            **_recovery_session_authority_to_mapping(event.authority),
        }
    if type(event) is _IntentBoundRecoveryAuthorityIssued:
        return {
            "type": "INTENT_BOUND_RECOVERY_AUTHORITY_ISSUED",
            **_intent_bound_recovery_authority_to_mapping(event.authority),
        }
    if type(event) is _PreIntentReadReserved:
        return {
            "type": "PRE_INTENT_READ_RESERVED",
            **_pre_intent_reservation_to_mapping(event.reservation),
        }
    if type(event) is _PreIntentReadResultRecorded:
        return {
            "type": "PRE_INTENT_READ_RESULT",
            **_pre_intent_result_to_mapping(event.result),
        }
    if type(event) is _PreIntentReadFailureRecorded:
        return {
            "type": "PRE_INTENT_READ_FAILURE",
            **_pre_intent_failure_to_mapping(event.failure),
        }
    if type(event) is _IntentChainBound:
        return {
            "type": "INTENT_CHAIN_BOUND",
            **_intent_chain_binding_to_mapping(event.binding),
        }
    if type(event) is _ExactRequestReserved:
        return {
            "type": "EXACT_REQUEST_RESERVED",
            "authority_sha256": event.authority_sha256,
            "generation": event.generation,
            "deadline_ns": event.deadline_ns,
            "reserved_request": _reserved_request_to_mapping(event.reserved_request),
        }
    if type(event) is _MutationReserved:
        return {
            "type": "MUTATION_RESERVED",
            **_mutation_reservation_to_mapping(event.proof),
        }
    if type(event) is _ReadPrepared:
        return {"type": "READ_PREPARED", **_read_reservation_to_mapping(event.proof)}
    if type(event) is _ReadResultRecorded:
        return {"type": "READ_RESULT", **_read_result_to_mapping(event.proof)}
    if type(event) is _ExactReadFailureRecorded:
        return {
            "type": "EXACT_READ_FAILURE",
            **_exact_read_failure_to_mapping(event.failure),
        }
    if type(event) is _OwnedFillCloseProven:
        return {
            "type": "OWNED_FILL_CLOSE_PROVEN",
            **_owned_fill_close_to_mapping(event.proof),
        }
    if type(event) is _AttemptPrepared:
        return {"type": "ATTEMPT_PREPARED", "attempt": _attempt_to_mapping(event.attempt)}
    if type(event) is _GoDurable:
        return {
            "type": "GO_DURABLE",
            "attempt_id": event.attempt_id,
            "generation": event.generation,
        }
    if type(event) is _AttemptConfirmed:
        return {
            "type": "ATTEMPT_CONFIRMED",
            "attempt_id": event.attempt_id,
            "generation": event.generation,
            "result_sha256": event.result_sha256,
        }
    if type(event) is _GenerationReaped:
        receipt = event.receipt
        return {
            "type": "GENERATION_REAPED",
            "generation": receipt.generation,
            "process_identity_sha256": receipt.process_identity_sha256,
            "admission_record_sequence": receipt.admission_record_sequence,
            "admission_record_digest": receipt.admission_record_digest,
            "returncode": receipt.returncode,
            "signal": receipt.signal,
            "local_process_quiesced": receipt.local_process_quiesced,
            "venue_mutation_absent_proven": receipt.venue_mutation_absent_proven,
        }
    if type(event) is _ReconciliationObserved:
        return {
            "type": "RECONCILIATION_OBSERVED",
            **_observation_to_mapping(event.observation),
        }
    if type(event) is _AttemptResolved:
        return {
            "type": "ATTEMPT_RESOLVED",
            "attempt_id": event.attempt_id,
            "generation": event.generation,
            "state": event.state.value,
            "boundary_result": event.boundary_result.value,
        }
    raise ExecutionJournalError("JOURNAL_EVENT_TYPE")


def _event_from_mapping(value: object) -> _JournalEvent:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ExecutionJournalError("JOURNAL_EVENT_FIELDS")
    event_type = value["type"]
    if event_type == "JOURNAL_CREATED":
        _require_exact_fields(value, frozenset({"type"}), "JOURNAL_EVENT_FIELDS")
        return _JournalCreated()
    if event_type == "GENERATION_ADMITTED":
        item = _require_exact_fields(
            value,
            frozenset({"type", "generation", "capability", "process_identity_sha256"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _GenerationAdmitted(
            generation=item["generation"],  # type: ignore[arg-type]
            capability=_enum(GenerationCapability, item["capability"]),  # type: ignore[arg-type]
            process_identity_sha256=item["process_identity_sha256"],  # type: ignore[arg-type]
        )
    if event_type == "STAGED_GENERATION_RECONCILED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_STAGED_GENERATION_RECOVERY_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        proof_fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _StagedGenerationReconciled(_staged_generation_recovery_from_mapping(proof_fields))
    if event_type == "SESSION_AUTHORITY_ESTABLISHED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_SESSION_AUTHORITY_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _SessionAuthorityEstablished(_session_authority_from_mapping(fields))
    if event_type == "RECOVERY_SESSION_AUTHORITY_ISSUED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_RECOVERY_SESSION_AUTHORITY_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _RecoverySessionAuthorityIssued(_recovery_session_authority_from_mapping(fields))
    if event_type == "INTENT_BOUND_RECOVERY_AUTHORITY_ISSUED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_INTENT_BOUND_RECOVERY_AUTHORITY_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _IntentBoundRecoveryAuthorityIssued(
            _intent_bound_recovery_authority_from_mapping(fields)
        )
    if event_type == "PRE_INTENT_READ_RESERVED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_PRE_INTENT_RESERVATION_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _PreIntentReadReserved(_pre_intent_reservation_from_mapping(fields))
    if event_type == "PRE_INTENT_READ_RESULT":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_PRE_INTENT_RESULT_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _PreIntentReadResultRecorded(_pre_intent_result_from_mapping(fields))
    if event_type == "PRE_INTENT_READ_FAILURE":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_PRE_INTENT_FAILURE_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _PreIntentReadFailureRecorded(_pre_intent_failure_from_mapping(fields))
    if event_type == "INTENT_CHAIN_BOUND":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_INTENT_CHAIN_BINDING_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _IntentChainBound(_intent_chain_binding_from_mapping(fields))
    if event_type == "EXACT_REQUEST_RESERVED":
        item = _require_exact_fields(
            value,
            frozenset(
                {
                    "type",
                    "authority_sha256",
                    "generation",
                    "deadline_ns",
                    "reserved_request",
                }
            ),
            "JOURNAL_EVENT_FIELDS",
        )
        return _ExactRequestReserved(
            authority_sha256=item["authority_sha256"],  # type: ignore[arg-type]
            generation=item["generation"],  # type: ignore[arg-type]
            deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
            reserved_request=_reserved_request_from_mapping(item["reserved_request"]),
        )
    if event_type == "MUTATION_RESERVED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_MUTATION_RESERVATION_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        proof_fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _MutationReserved(_mutation_reservation_from_mapping(proof_fields))
    if event_type == "READ_PREPARED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_READ_RESERVATION_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        proof_fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _ReadPrepared(_read_reservation_from_mapping(proof_fields))
    if event_type == "READ_RESULT":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_READ_RESULT_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        proof_fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _ReadResultRecorded(_read_result_from_mapping(proof_fields))
    if event_type == "EXACT_READ_FAILURE":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_EXACT_READ_FAILURE_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _ExactReadFailureRecorded(_exact_read_failure_from_mapping(fields))
    if event_type == "OWNED_FILL_CLOSE_PROVEN":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_OWNED_FILL_CLOSE_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        proof_fields = {key: field_value for key, field_value in item.items() if key != "type"}
        return _OwnedFillCloseProven(_owned_fill_close_from_mapping(proof_fields))
    if event_type == "ATTEMPT_PREPARED":
        item = _require_exact_fields(
            value,
            frozenset({"type", "attempt"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _AttemptPrepared(_attempt_from_mapping(item["attempt"]))
    if event_type == "GO_DURABLE":
        item = _require_exact_fields(
            value,
            frozenset({"type", "attempt_id", "generation"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _GoDurable(
            attempt_id=item["attempt_id"],  # type: ignore[arg-type]
            generation=item["generation"],  # type: ignore[arg-type]
        )
    if event_type == "ATTEMPT_CONFIRMED":
        item = _require_exact_fields(
            value,
            frozenset({"type", "attempt_id", "generation", "result_sha256"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _AttemptConfirmed(
            attempt_id=item["attempt_id"],  # type: ignore[arg-type]
            generation=item["generation"],  # type: ignore[arg-type]
            result_sha256=item["result_sha256"],  # type: ignore[arg-type]
        )
    if event_type == "GENERATION_REAPED":
        item = _require_exact_fields(
            value,
            frozenset(
                {
                    "type",
                    "generation",
                    "process_identity_sha256",
                    "admission_record_sequence",
                    "admission_record_digest",
                    "returncode",
                    "signal",
                    "local_process_quiesced",
                    "venue_mutation_absent_proven",
                }
            ),
            "JOURNAL_EVENT_FIELDS",
        )
        return _GenerationReaped(
            ProcessReapReceipt(
                generation=item["generation"],  # type: ignore[arg-type]
                process_identity_sha256=item["process_identity_sha256"],  # type: ignore[arg-type]
                admission_record_sequence=item["admission_record_sequence"],  # type: ignore[arg-type]
                admission_record_digest=item["admission_record_digest"],  # type: ignore[arg-type]
                returncode=item["returncode"],  # type: ignore[arg-type]
                signal=item["signal"],  # type: ignore[arg-type]
                local_process_quiesced=item["local_process_quiesced"],  # type: ignore[arg-type]
                venue_mutation_absent_proven=item[  # type: ignore[arg-type]
                    "venue_mutation_absent_proven"
                ],
            )
        )
    if event_type == "RECONCILIATION_OBSERVED":
        item = _require_exact_fields(
            value,
            frozenset({"type", *_OBSERVATION_FIELDS}),
            "JOURNAL_EVENT_FIELDS",
        )
        observation_fields = {
            key: field_value for key, field_value in item.items() if key != "type"
        }
        return _ReconciliationObserved(_observation_from_mapping(observation_fields))
    if event_type == "ATTEMPT_RESOLVED":
        item = _require_exact_fields(
            value,
            frozenset({"type", "attempt_id", "generation", "state", "boundary_result"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _AttemptResolved(
            attempt_id=item["attempt_id"],  # type: ignore[arg-type]
            generation=item["generation"],  # type: ignore[arg-type]
            state=_enum(FrontierState, item["state"]),  # type: ignore[arg-type]
            boundary_result=_enum(BoundaryResult, item["boundary_result"]),  # type: ignore[arg-type]
        )
    raise ExecutionJournalError("JOURNAL_EVENT_TYPE")


def _record_body(
    *,
    sequence: int,
    previous_digest: str,
    event: _JournalEvent,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "event": _event_to_mapping(event),
    }


def _build_record(
    *,
    sequence: int,
    previous_digest: str,
    event: _JournalEvent,
) -> tuple[JournalRecord, bytes]:
    body = _record_body(
        sequence=sequence,
        previous_digest=previous_digest,
        event=event,
    )
    digest = hashlib.sha256(_canonical_json(body)).hexdigest()
    wire = {**body, "digest": digest}
    encoded = _canonical_json(wire) + b"\n"
    if len(encoded) > MAX_RECORD_BYTES:
        raise ExecutionJournalError("JOURNAL_RECORD_OVERSIZED")
    return (
        JournalRecord(
            schema_version=SCHEMA_VERSION,
            sequence=sequence,
            previous_digest=previous_digest,
            digest=digest,
            event=event,
        ),
        encoded,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionJournalError("JOURNAL_MALFORMED")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ExecutionJournalError("JOURNAL_MALFORMED")


def _decode_record(
    raw_line: bytes,
    *,
    expected_sequence: int,
    expected_previous_digest: str,
) -> JournalRecord:
    try:
        text = raw_line[:-1].decode("ascii")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ExecutionJournalError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionJournalError("JOURNAL_MALFORMED") from exc
    item = _require_exact_fields(
        value,
        frozenset({"schema_version", "sequence", "previous_digest", "digest", "event"}),
        "JOURNAL_RECORD_FIELDS",
    )
    if item["schema_version"] != SCHEMA_VERSION:
        raise ExecutionJournalError("JOURNAL_SCHEMA_VERSION")
    if type(item["sequence"]) is not int or item["sequence"] != expected_sequence:
        raise ExecutionJournalError("JOURNAL_SEQUENCE")
    if item["previous_digest"] != expected_previous_digest:
        raise ExecutionJournalError("JOURNAL_PREVIOUS_DIGEST")
    if not _is_sha256(item["digest"]):
        raise ExecutionJournalError("JOURNAL_DIGEST")
    if raw_line != _canonical_json(value) + b"\n":
        raise ExecutionJournalError("JOURNAL_NONCANONICAL")
    body = {key: field_value for key, field_value in item.items() if key != "digest"}
    expected_digest = hashlib.sha256(_canonical_json(body)).hexdigest()
    if item["digest"] != expected_digest:
        raise ExecutionJournalError("JOURNAL_DIGEST")
    event = _event_from_mapping(item["event"])
    return JournalRecord(
        schema_version=SCHEMA_VERSION,
        sequence=item["sequence"],
        previous_digest=item["previous_digest"],  # type: ignore[arg-type]
        digest=item["digest"],  # type: ignore[arg-type]
        event=event,
    )


@dataclass(slots=True)
class _JournalState:
    has_genesis: bool = False
    last_generation: int = 0
    active_generation: int | None = None
    generation_capabilities: dict[int, GenerationCapability] = field(default_factory=dict)
    generation_admissions: dict[int, DurableGenerationAdmission] = field(default_factory=dict)
    generation_admission_records: dict[int, tuple[int, str]] = field(default_factory=dict)
    reaped_generations: set[int] = field(default_factory=set)
    reap_receipts: dict[int, ProcessReapReceipt] = field(default_factory=dict)
    staged_generation_recoveries: dict[int, StagedGenerationRecoveryProof] = field(
        default_factory=dict
    )
    session_authorities: dict[str, SessionAuthority] = field(default_factory=dict)
    recovery_session_authorities: dict[str, RecoverySessionAuthority] = field(default_factory=dict)
    intent_bound_recovery_authorities: dict[str, IntentBoundRecoveryAuthority] = field(
        default_factory=dict
    )
    recovery_authority_by_generation: dict[int, str] = field(default_factory=dict)
    request_ledgers: dict[str, MutationLedger] = field(default_factory=dict)
    intent_chain_bindings: dict[str, IntentChainBinding] = field(default_factory=dict)
    pre_intent_reservations: dict[str, PreIntentReadReservation] = field(default_factory=dict)
    pre_intent_prepared_records: dict[str, tuple[int, str]] = field(default_factory=dict)
    pre_intent_pending: dict[str, str] = field(default_factory=dict)
    pre_intent_results: dict[str, PreIntentReadResult] = field(default_factory=dict)
    pre_intent_failures: dict[str, PreIntentReadFailure] = field(default_factory=dict)
    completed_pre_intent_paths: dict[str, list[str]] = field(default_factory=dict)
    completed_pre_intent_results: dict[str, list[str]] = field(default_factory=dict)
    retryable_logical_requests: dict[str, str] = field(default_factory=dict)
    exact_request_reservations: dict[str, ExactRequestReservation] = field(default_factory=dict)
    exact_pending_requests: dict[str, list[str]] = field(default_factory=dict)
    attempts: dict[str, MutationAttempt] = field(default_factory=dict)
    frontiers: dict[str, FrontierState] = field(default_factory=dict)
    mutation_reservations: dict[str, MutationReservationProof] = field(default_factory=dict)
    consumed_mutation_reservations: set[str] = field(default_factory=set)
    generation_inflight_attempts: dict[int, str] = field(default_factory=dict)
    observations: dict[str, ReconciliationObservation] = field(default_factory=dict)
    consumed_observations: set[str] = field(default_factory=set)
    read_reservations: dict[str, ReadReservationProof] = field(default_factory=dict)
    read_prepared_records: dict[str, tuple[int, str]] = field(default_factory=dict)
    read_results: dict[str, ReadResultProof] = field(default_factory=dict)
    read_result_records: dict[str, tuple[int, str]] = field(default_factory=dict)
    read_result_by_request: dict[str, str] = field(default_factory=dict)
    exact_read_failures: dict[str, ExactReadFailure] = field(default_factory=dict)
    read_failure_by_request: dict[str, str] = field(default_factory=dict)
    last_request_sequence: dict[tuple[str, str], int] = field(default_factory=dict)
    owned_fill_close_proofs: dict[str, OwnedFillCloseProof] = field(default_factory=dict)
    consumed_owned_fill_close_proofs: set[str] = field(default_factory=set)

    def apply(self, record: JournalRecord) -> None:
        event = record.event
        if type(event) is _JournalCreated:
            if self.has_genesis or record.sequence != 1:
                raise ExecutionJournalError("JOURNAL_GENESIS")
            self.has_genesis = True
            return
        if not self.has_genesis:
            raise ExecutionJournalError("JOURNAL_GENESIS")
        if type(event) is _GenerationAdmitted:
            self._apply_generation_admitted(event, record)
        elif type(event) is _StagedGenerationReconciled:
            self._apply_staged_generation_reconciled(event.proof)
        elif type(event) is _SessionAuthorityEstablished:
            self._apply_session_authority(event.authority)
        elif type(event) is _RecoverySessionAuthorityIssued:
            self._apply_recovery_session_authority(event.authority)
        elif type(event) is _IntentBoundRecoveryAuthorityIssued:
            self._apply_intent_bound_recovery_authority(event.authority)
        elif type(event) is _PreIntentReadReserved:
            self._apply_pre_intent_reservation(event.reservation, record)
        elif type(event) is _PreIntentReadResultRecorded:
            self._apply_pre_intent_result(event.result)
        elif type(event) is _PreIntentReadFailureRecorded:
            self._apply_pre_intent_failure(event.failure)
        elif type(event) is _IntentChainBound:
            self._apply_intent_chain_binding(event.binding)
        elif type(event) is _ExactRequestReserved:
            self._apply_exact_request_reservation(event)
        elif type(event) is _MutationReserved:
            self._apply_mutation_reserved(event.proof)
        elif type(event) is _ReadPrepared:
            self._apply_read_prepared(event.proof, record)
        elif type(event) is _ReadResultRecorded:
            self._apply_read_result(event.proof, record)
        elif type(event) is _ExactReadFailureRecorded:
            self._apply_exact_read_failure(event.failure)
        elif type(event) is _OwnedFillCloseProven:
            self._apply_owned_fill_close_proof(event.proof)
        elif type(event) is _AttemptPrepared:
            self._apply_attempt_prepared(event.attempt)
        elif type(event) is _GoDurable:
            self._apply_go(event)
        elif type(event) is _AttemptConfirmed:
            self._apply_confirmation(event)
        elif type(event) is _GenerationReaped:
            self._apply_reap(event)
        elif type(event) is _ReconciliationObserved:
            self._apply_observation(event.observation)
        elif type(event) is _AttemptResolved:
            self._apply_resolution(event)
        else:
            raise ExecutionJournalError("JOURNAL_EVENT_TYPE")

    def _apply_generation_admitted(
        self,
        event: _GenerationAdmitted,
        record: JournalRecord,
    ) -> None:
        if self.active_generation is not None:
            raise ExecutionJournalError("GENERATION_ACTIVE")
        if (
            not _is_positive_int(event.generation)
            or type(event.capability) is not GenerationCapability
        ):
            raise ExecutionJournalError("INVALID_GENERATION")
        if event.generation != self.last_generation + 1:
            raise ExecutionJournalError("GENERATION_SEQUENCE")
        admission = DurableGenerationAdmission(
            generation=event.generation,
            process_identity_sha256=event.process_identity_sha256,
        )
        if event.generation == 1 and event.capability is GenerationCapability.RECOVERY:
            raise ExecutionJournalError("RECOVERY_REQUIRES_REAP")
        if event.generation > 1 and event.capability is GenerationCapability.PRIMARY:
            raise ExecutionJournalError("PRIMARY_ONLY_FIRST_GENERATION")
        if event.capability is GenerationCapability.RECOVERY and (
            event.generation - 1 not in self.reaped_generations
        ):
            raise ExecutionJournalError("RECOVERY_REQUIRES_REAP")
        self.last_generation = event.generation
        self.active_generation = event.generation
        self.generation_capabilities[event.generation] = event.capability
        self.generation_admissions[event.generation] = admission
        self.generation_admission_records[event.generation] = (
            record.sequence,
            record.digest,
        )

    def _apply_staged_generation_reconciled(
        self,
        proof: StagedGenerationRecoveryProof,
    ) -> None:
        if self.active_generation is not None:
            raise ExecutionJournalError("GENERATION_ACTIVE")
        if proof.generation != self.last_generation + 1:
            raise ExecutionJournalError("GENERATION_SEQUENCE")
        if proof.generation > 1 and proof.generation - 1 not in self.reaped_generations:
            raise ExecutionJournalError("RECOVERY_REQUIRES_REAP")
        self.last_generation = proof.generation
        self.generation_capabilities[proof.generation] = proof.capability
        self.reaped_generations.add(proof.generation)
        self.staged_generation_recoveries[proof.generation] = proof

    def _apply_session_authority(self, authority: SessionAuthority) -> None:
        if authority.authority_sha256 in self.session_authorities:
            raise ExecutionJournalError("SESSION_AUTHORITY_ALREADY_EXISTS")
        if self.active_generation != authority.generation:
            raise ExecutionJournalError("SESSION_AUTHORITY_GENERATION_NOT_ACTIVE")
        if self.generation_capabilities[authority.generation] is not GenerationCapability.PRIMARY:
            raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRES_PRIMARY")
        if any(
            previous.generation == authority.generation
            for previous in self.session_authorities.values()
        ):
            raise ExecutionJournalError("SESSION_AUTHORITY_GENERATION_ALREADY_BOUND")
        self.session_authorities[authority.authority_sha256] = authority
        self.request_ledgers[authority.authority_sha256] = MutationLedger()
        self.completed_pre_intent_paths[authority.authority_sha256] = []
        self.completed_pre_intent_results[authority.authority_sha256] = []
        self.exact_pending_requests[authority.authority_sha256] = []

    def _apply_recovery_session_authority(
        self,
        authority: RecoverySessionAuthority,
    ) -> None:
        if authority.authority_sha256 in self.recovery_session_authorities:
            raise ExecutionJournalError("RECOVERY_SESSION_AUTHORITY_ALREADY_EXISTS")
        if authority.generation in self.recovery_authority_by_generation:
            raise ExecutionJournalError("RECOVERY_GENERATION_AUTHORITY_ALREADY_EXISTS")
        if (
            self.active_generation != authority.generation
            or self.generation_capabilities.get(authority.generation)
            is not GenerationCapability.RECOVERY
        ):
            raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_NOT_ACTIVE")
        primary = self.session_authorities.get(authority.primary_authority_sha256)
        binding = self.intent_chain_bindings.get(authority.primary_authority_sha256)
        if primary is None or binding is None:
            raise ExecutionJournalError("PRIMARY_SESSION_AUTHORITY_REQUIRED")
        source = self.attempts.get(authority.source_attempt_id)
        if source is None:
            raise ExecutionJournalError("RECOVERY_SOURCE_ATTEMPT_REQUIRED")
        source_frontier = self.frontiers[source.attempt_id]
        source_recoverable = source_frontier is FrontierState.UNKNOWN or (
            source.kind is MutationKind.CREATE and source_frontier is FrontierState.CONFIRMED
        )
        if (
            not source_recoverable
            or source.generation not in self.reaped_generations
            or source.generation != authority.source_generation
            or source.kind is not authority.source_kind
            or source.authorization_id != authority.source_authorization_id
            or source.intent_sha256 != authority.source_intent_sha256
            or source.runtime_commit != authority.source_runtime_commit
            or source.session_nonce != authority.source_session_nonce
            or source.client_id != authority.source_client_id
            or primary.authorization_id != source.authorization_id
            or primary.runtime_commit != source.runtime_commit
            or primary.session_nonce != source.session_nonce
            or binding.intent_sha256 != source.intent_sha256
        ):
            raise ExecutionJournalError("RECOVERY_SESSION_LINEAGE_MISMATCH")
        self.recovery_session_authorities[authority.authority_sha256] = authority
        self.recovery_authority_by_generation[authority.generation] = authority.authority_sha256

    def _apply_intent_bound_recovery_authority(
        self,
        authority: IntentBoundRecoveryAuthority,
    ) -> None:
        if authority.authority_sha256 in self.intent_bound_recovery_authorities:
            raise ExecutionJournalError("INTENT_BOUND_RECOVERY_AUTHORITY_ALREADY_EXISTS")
        if authority.generation in self.recovery_authority_by_generation:
            raise ExecutionJournalError("RECOVERY_GENERATION_AUTHORITY_ALREADY_EXISTS")
        if (
            self.active_generation != authority.generation
            or self.generation_capabilities.get(authority.generation)
            is not GenerationCapability.RECOVERY
        ):
            raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_NOT_ACTIVE")
        primary = self.session_authorities.get(authority.primary_authority_sha256)
        binding = self.intent_chain_bindings.get(authority.primary_authority_sha256)
        if primary is None or binding is None:
            raise ExecutionJournalError("PRIMARY_SESSION_AUTHORITY_REQUIRED")
        if any(
            attempt.intent_sha256 == binding.intent_sha256
            or attempt.authorization_id == primary.authorization_id
            for attempt in self.attempts.values()
        ):
            raise ExecutionJournalError("INTENT_BOUND_RECOVERY_REQUIRES_NO_ATTEMPT")
        pending = self.exact_pending_requests[primary.authority_sha256]
        pending_mutations = tuple(
            request_sha256
            for request_sha256 in pending
            if self.exact_request_reservations[request_sha256].reserved_request.purpose
            is not RequestPurpose.READ
        )
        abandoned = pending_mutations[0] if len(pending_mutations) == 1 else None
        if pending_mutations and (
            len(pending_mutations) != 1
            or self.exact_request_reservations[pending_mutations[0]].reserved_request.purpose
            is not RequestPurpose.CREATE
        ):
            raise ExecutionJournalError("INTENT_BOUND_RECOVERY_PENDING_REQUEST_MISMATCH")
        if (
            primary.generation not in self.reaped_generations
            or authority.source_generation != primary.generation
            or authority.source_authorization_id != primary.authorization_id
            or authority.source_intent_sha256 != binding.intent_sha256
            or authority.source_runtime_commit != primary.runtime_commit
            or authority.source_session_nonce != primary.session_nonce
            or authority.query_client_id != primary.client_id
            or authority.intent_binding_sha256 != binding.binding_sha256
            or authority.abandoned_create_request_sha256 != abandoned
        ):
            raise ExecutionJournalError("INTENT_BOUND_RECOVERY_LINEAGE_MISMATCH")
        if abandoned is not None:
            pending.remove(abandoned)
        self.intent_bound_recovery_authorities[authority.authority_sha256] = authority
        self.recovery_authority_by_generation[authority.generation] = authority.authority_sha256

    def _apply_pre_intent_reservation(
        self,
        reservation: PreIntentReadReservation,
        record: JournalRecord,
    ) -> None:
        authority_sha256 = reservation.session_authority_sha256
        authority = self.session_authorities.get(authority_sha256)
        if authority is None:
            raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")
        if authority_sha256 in self.intent_chain_bindings:
            raise ExecutionJournalError("PRE_INTENT_CHAIN_ALREADY_BOUND")
        if (
            self.active_generation != reservation.generation
            or reservation.generation != authority.generation
        ):
            raise ExecutionJournalError("PRE_INTENT_READ_GENERATION_NOT_ACTIVE")
        if authority_sha256 in self.pre_intent_pending:
            raise ExecutionJournalError("PRE_INTENT_READ_ALREADY_PENDING")
        if reservation.reservation_sha256 in self.pre_intent_reservations:
            raise ExecutionJournalError("PRE_INTENT_READ_ALREADY_EXISTS")

        completed = self.completed_pre_intent_paths[authority_sha256]
        if len(completed) >= NORMAL_PRE_CREATE_HTTP_REQUESTS:
            raise ExecutionJournalError("PRE_INTENT_READ_BUDGET_EXHAUSTED")
        expected_path = _PRE_INTENT_READ_PATHS[len(completed)]
        if reservation.path != expected_path:
            raise ExecutionJournalError("PRE_INTENT_READ_SEQUENCE_MISMATCH")
        expected_parameters = _expected_pre_intent_parameters(
            reservation.path,
            authority,
        )
        if dict(reservation.parameters) != expected_parameters:
            raise ExecutionJournalError("PRE_INTENT_READ_PARAMETERS_MISMATCH")

        previous = self.request_ledgers[authority_sha256]
        if reservation.elapsed_seconds < previous.last_elapsed_seconds:
            raise ExecutionJournalError("REQUEST_ELAPSED_NOT_MONOTONIC")
        retryable = self.retryable_logical_requests.get(authority_sha256)
        if reservation.retry_index == 0:
            if retryable is not None:
                raise ExecutionJournalError("READ_RETRY_PENDING")
            expected_read_retries = previous.read_retry_requests
        else:
            if previous.read_retry_requests >= MAX_READ_RETRIES:
                raise ExecutionJournalError("READ_RETRY_BUDGET_EXHAUSTED")
            if retryable != reservation.logical_request_sha256:
                raise ExecutionJournalError("READ_RETRY_NOT_PROVEN")
            expected_read_retries = previous.read_retry_requests + 1
        try:
            expected_ledger = replace(
                previous,
                total_http_requests=previous.total_http_requests + 1,
                read_retry_requests=expected_read_retries,
                last_elapsed_seconds=reservation.elapsed_seconds,
                retryable_read_sha256=None,
            )
        except MutationProtocolError as exc:
            raise ExecutionJournalError("PRE_INTENT_READ_BUDGET_EXHAUSTED") from exc
        if reservation.ledger != expected_ledger:
            raise ExecutionJournalError("PRE_INTENT_READ_LEDGER_MISMATCH")

        self.pre_intent_reservations[reservation.reservation_sha256] = reservation
        self.pre_intent_prepared_records[reservation.reservation_sha256] = (
            record.sequence,
            record.digest,
        )
        self.pre_intent_pending[authority_sha256] = reservation.reservation_sha256
        self.request_ledgers[authority_sha256] = reservation.ledger
        self.retryable_logical_requests.pop(authority_sha256, None)

    def _pre_intent_terminal_reservation(
        self,
        *,
        reservation_sha256: str,
        prepared_record_sequence: int,
        prepared_record_digest: str,
        observed_at_ns: int,
    ) -> tuple[str, PreIntentReadReservation]:
        reservation = self.pre_intent_reservations.get(reservation_sha256)
        if reservation is None:
            raise ExecutionJournalError("PRE_INTENT_READ_RESERVATION_REQUIRED")
        authority_sha256 = reservation.session_authority_sha256
        if self.pre_intent_pending.get(authority_sha256) != reservation_sha256:
            raise ExecutionJournalError("PRE_INTENT_READ_NOT_PENDING")
        if self.pre_intent_prepared_records[reservation_sha256] != (
            prepared_record_sequence,
            prepared_record_digest,
        ):
            raise ExecutionJournalError("PRE_INTENT_PREPARED_RECORD_MISMATCH")
        if observed_at_ns > reservation.deadline_ns:
            raise ExecutionJournalError("PRE_INTENT_READ_DEADLINE_EXCEEDED")
        return authority_sha256, reservation

    def _apply_pre_intent_result(self, result: PreIntentReadResult) -> None:
        authority_sha256, reservation = self._pre_intent_terminal_reservation(
            reservation_sha256=result.reservation_sha256,
            prepared_record_sequence=result.prepared_record_sequence,
            prepared_record_digest=result.prepared_record_digest,
            observed_at_ns=result.observed_at_ns,
        )
        if result.result_proof_sha256 in self.pre_intent_results:
            raise ExecutionJournalError("PRE_INTENT_READ_RESULT_ALREADY_EXISTS")
        self.pre_intent_pending.pop(authority_sha256)
        self.pre_intent_results[result.result_proof_sha256] = result
        self.completed_pre_intent_paths[authority_sha256].append(reservation.path)
        self.completed_pre_intent_results[authority_sha256].append(result.result_proof_sha256)

    def _apply_pre_intent_failure(self, failure: PreIntentReadFailure) -> None:
        authority_sha256, reservation = self._pre_intent_terminal_reservation(
            reservation_sha256=failure.reservation_sha256,
            prepared_record_sequence=failure.prepared_record_sequence,
            prepared_record_digest=failure.prepared_record_digest,
            observed_at_ns=failure.observed_at_ns,
        )
        if failure.failure_proof_sha256 in self.pre_intent_failures:
            raise ExecutionJournalError("PRE_INTENT_READ_FAILURE_ALREADY_EXISTS")
        self.pre_intent_pending.pop(authority_sha256)
        self.pre_intent_failures[failure.failure_proof_sha256] = failure
        self.retryable_logical_requests[authority_sha256] = reservation.logical_request_sha256
        self.request_ledgers[authority_sha256] = replace(
            self.request_ledgers[authority_sha256],
            retryable_read_sha256=reservation.logical_request_sha256,
        )

    def _pre_intent_chain_sha256(self, authority_sha256: str) -> str:
        successful_reads: list[dict[str, object]] = []
        for result_proof_sha256 in self.completed_pre_intent_results[authority_sha256]:
            result = self.pre_intent_results[result_proof_sha256]
            reservation = self.pre_intent_reservations[result.reservation_sha256]
            successful_reads.append(
                {
                    "reservation_sha256": reservation.reservation_sha256,
                    "logical_request_sha256": reservation.logical_request_sha256,
                    "path": reservation.path,
                    "ledger_sha256": _ledger_sha256(reservation.ledger),
                    "result_proof_sha256": result.result_proof_sha256,
                    "result_sha256": result.result_sha256,
                }
            )
        material = {
            "session_authority_sha256": authority_sha256,
            "successful_reads": successful_reads,
        }
        return hashlib.sha256(_canonical_json(material)).hexdigest()

    def _apply_intent_chain_binding(self, binding: IntentChainBinding) -> None:
        authority_sha256 = binding.session_authority_sha256
        if authority_sha256 not in self.session_authorities:
            raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")
        if authority_sha256 in self.intent_chain_bindings:
            raise ExecutionJournalError("INTENT_CHAIN_ALREADY_BOUND")
        if authority_sha256 in self.pre_intent_pending:
            raise ExecutionJournalError("PRE_INTENT_READ_PENDING")
        if authority_sha256 in self.retryable_logical_requests:
            raise ExecutionJournalError("PRE_INTENT_READ_RETRY_PENDING")
        if tuple(self.completed_pre_intent_paths[authority_sha256]) != (_PRE_INTENT_READ_PATHS):
            raise ExecutionJournalError("PRE_INTENT_CHAIN_INCOMPLETE")
        if binding.pre_intent_chain_sha256 != self._pre_intent_chain_sha256(
            authority_sha256
        ) or binding.last_ledger_sha256 != _ledger_sha256(self.request_ledgers[authority_sha256]):
            raise ExecutionJournalError("INTENT_CHAIN_BINDING_MISMATCH")
        self.intent_chain_bindings[authority_sha256] = binding

    def _validate_exact_request_ledger(
        self,
        previous: MutationLedger,
        reserved: ReservedRequest,
        *,
        intent_bound_recovery: bool = False,
    ) -> None:
        ledger = reserved.ledger
        if (
            ledger.total_http_requests != previous.total_http_requests + 1
            or reserved.elapsed_seconds < previous.last_elapsed_seconds
            or ledger.last_elapsed_seconds != reserved.elapsed_seconds
            or ledger.retryable_read_sha256 is not None
            or set(dict(reserved.parameters)) - _SAFE_REQUEST_PARAMETER_KEYS
        ):
            raise ExecutionJournalError("EXACT_REQUEST_LEDGER_MISMATCH")
        if (
            previous.create_requests == 0
            and reserved.purpose is not RequestPurpose.CREATE
            and not intent_bound_recovery
        ):
            raise ExecutionJournalError("POST_BIND_CREATE_REQUIRED")
        unchanged = (
            ledger.create_requests == previous.create_requests
            and ledger.cancel_requests == previous.cancel_requests
            and ledger.emergency_close_requests == previous.emergency_close_requests
        )
        if reserved.purpose is RequestPurpose.READ:
            expected_post_reads = previous.post_create_read_requests + (
                1 if previous.create_requests == 1 else 0
            )
            if reserved.retry_index == 0:
                valid_retry = (
                    previous.retryable_read_sha256 is None
                    and ledger.read_retry_requests == previous.read_retry_requests
                )
            else:
                valid_retry = (
                    reserved.retry_index == 1
                    and previous.retryable_read_sha256 == reserved.logical_request_sha256
                    and ledger.read_retry_requests == previous.read_retry_requests + 1
                )
            valid = (
                reserved.method == "GET"
                and reserved.path in _PRE_INTENT_READ_PATHS
                and unchanged
                and ledger.stage is previous.stage
                and ledger.post_create_read_requests == expected_post_reads
                and valid_retry
            )
        elif reserved.purpose is RequestPurpose.CREATE:
            valid = (
                reserved.retry_index == 0
                and reserved.method == "POST"
                and reserved.path == "/fapi/v1/order"
                and previous.stage is RequestStage.CREATE_READY
                and previous.retryable_read_sha256 is None
                and ledger.create_requests == previous.create_requests + 1
                and ledger.cancel_requests == previous.cancel_requests
                and ledger.emergency_close_requests == previous.emergency_close_requests
                and ledger.read_retry_requests == previous.read_retry_requests
                and ledger.post_create_read_requests == previous.post_create_read_requests
                and ledger.stage is RequestStage.CREATE_ATTEMPTED
            )
        elif reserved.purpose is RequestPurpose.CANCEL:
            valid = (
                reserved.retry_index == 0
                and reserved.method == "DELETE"
                and reserved.path == "/fapi/v1/order"
                and ledger.create_requests == previous.create_requests
                and ledger.cancel_requests == previous.cancel_requests + 1
                and ledger.emergency_close_requests == previous.emergency_close_requests
                and ledger.read_retry_requests == previous.read_retry_requests
                and ledger.post_create_read_requests == previous.post_create_read_requests
                and ledger.stage is RequestStage.CANCEL_ATTEMPTED
            )
        else:
            valid = (
                reserved.purpose is RequestPurpose.EMERGENCY_CLOSE
                and reserved.retry_index == 0
                and reserved.method == "POST"
                and reserved.path == "/fapi/v1/order"
                and ledger.create_requests == previous.create_requests
                and ledger.cancel_requests == previous.cancel_requests
                and ledger.emergency_close_requests == previous.emergency_close_requests + 1
                and ledger.read_retry_requests == previous.read_retry_requests
                and ledger.post_create_read_requests == previous.post_create_read_requests
                and ledger.stage is RequestStage.EMERGENCY_CLOSE_ATTEMPTED
            )
        if not valid:
            raise ExecutionJournalError("EXACT_REQUEST_LEDGER_MISMATCH")

    def _primary_authority_sha256(self, authority_sha256: str) -> str:
        if authority_sha256 in self.session_authorities:
            return authority_sha256
        recovery = self.recovery_session_authorities.get(authority_sha256)
        if recovery is not None:
            return recovery.primary_authority_sha256
        intent_bound = self.intent_bound_recovery_authorities.get(authority_sha256)
        if intent_bound is not None:
            return intent_bound.primary_authority_sha256
        raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")

    def _resolve_exact_request_authority(
        self,
        authority_sha256: str,
        generation: int,
    ) -> tuple[SessionAuthority, str, bool, bool]:
        primary = self.session_authorities.get(authority_sha256)
        if primary is not None:
            if generation != primary.generation:
                raise ExecutionJournalError("RECOVERY_SESSION_AUTHORITY_REQUIRED")
            return primary, primary.authority_sha256, False, False
        recovery = self.recovery_session_authorities.get(authority_sha256)
        if recovery is not None:
            if recovery.generation != generation:
                raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_MISMATCH")
            primary = self.session_authorities[recovery.primary_authority_sha256]
            return primary, primary.authority_sha256, True, False
        intent_bound = self.intent_bound_recovery_authorities.get(authority_sha256)
        if intent_bound is None:
            raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")
        if intent_bound.generation != generation:
            raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_MISMATCH")
        primary = self.session_authorities[intent_bound.primary_authority_sha256]
        return primary, primary.authority_sha256, True, True

    def _apply_exact_request_reservation(self, event: _ExactRequestReserved) -> None:
        (
            authority,
            ledger_authority_sha256,
            is_recovery,
            is_intent_bound_recovery,
        ) = self._resolve_exact_request_authority(
            event.authority_sha256,
            event.generation,
        )
        binding = self.intent_chain_bindings.get(ledger_authority_sha256)
        if binding is None:
            raise ExecutionJournalError("INTENT_CHAIN_BINDING_REQUIRED")
        reserved = event.reserved_request
        if (
            self.active_generation != event.generation
            or reserved.intent_sha256 != binding.intent_sha256
        ):
            raise ExecutionJournalError("EXACT_REQUEST_BINDING_MISMATCH")
        if is_intent_bound_recovery and reserved.purpose is not RequestPurpose.READ:
            raise ExecutionJournalError("INTENT_BOUND_RECOVERY_MUTATION_FORBIDDEN")
        if is_recovery and reserved.purpose is RequestPurpose.CREATE:
            raise ExecutionJournalError("RECOVERY_CREATE_FORBIDDEN")
        if is_intent_bound_recovery:
            expected_parameters = _expected_intent_bound_recovery_parameters(
                reserved.path,
                authority,
            )
            if reserved.method != "GET" or reserved.parameters != tuple(
                sorted(expected_parameters.items())
            ):
                raise ExecutionJournalError("INTENT_BOUND_RECOVERY_READ_BINDING_MISMATCH")
        if reserved.request_sha256 in self.exact_request_reservations:
            raise ExecutionJournalError("EXACT_REQUEST_ALREADY_EXISTS")
        pending_requests = self.exact_pending_requests[ledger_authority_sha256]
        pending_mutation = any(
            self.exact_request_reservations[request_sha].reserved_request.purpose
            is not RequestPurpose.READ
            for request_sha in pending_requests
        )
        if pending_mutation or (reserved.purpose is not RequestPurpose.READ and pending_requests):
            raise ExecutionJournalError("EXACT_REQUEST_ALREADY_PENDING")
        previous = self.request_ledgers[ledger_authority_sha256]
        self._validate_exact_request_ledger(
            previous,
            reserved,
            intent_bound_recovery=is_intent_bound_recovery,
        )
        exact = ExactRequestReservation(
            authority_sha256=event.authority_sha256,
            generation=event.generation,
            deadline_ns=event.deadline_ns,
            reserved_request=reserved,
        )
        self.exact_request_reservations[reserved.request_sha256] = exact
        self.exact_pending_requests[ledger_authority_sha256].append(reserved.request_sha256)
        self.request_ledgers[ledger_authority_sha256] = reserved.ledger
        if previous.retryable_read_sha256 is not None and reserved.retry_index == 1:
            self.retryable_logical_requests.pop(ledger_authority_sha256, None)

    def _exact_request_for_proof(
        self,
        request_sha256: str,
    ) -> ExactRequestReservation:
        exact = self.exact_request_reservations.get(request_sha256)
        if exact is None:
            raise ExecutionJournalError("EXACT_REQUEST_RESERVATION_REQUIRED")
        ledger_authority_sha256 = self._primary_authority_sha256(exact.authority_sha256)
        if request_sha256 not in self.exact_pending_requests[ledger_authority_sha256]:
            raise ExecutionJournalError("EXACT_REQUEST_NOT_PENDING")
        return exact

    def _validate_exact_mutation_proof(
        self,
        proof: MutationReservationProof,
    ) -> None:
        exact = self._exact_request_for_proof(proof.request_sha256)
        ledger_authority_sha256 = self._primary_authority_sha256(exact.authority_sha256)
        authority = self.session_authorities[ledger_authority_sha256]
        binding = self.intent_chain_bindings[ledger_authority_sha256]
        try:
            expected = MutationReservationProof.from_reserved_request(
                exact.reserved_request,
                purpose=proof.purpose,
                generation=exact.generation,
                deadline_ns=exact.deadline_ns,
                client_id=proof.client_id,
                authorization_id=proof.authorization_id,
                source_attempt_id=proof.source_attempt_id,
                precondition_sha256=proof.precondition_sha256,
            )
        except ExecutionJournalError as exc:
            raise ExecutionJournalError("EXACT_REQUEST_PROOF_MISMATCH") from exc
        if (
            proof != expected
            or proof.authorization_id != authority.authorization_id
            or proof.intent_sha256 != binding.intent_sha256
        ):
            raise ExecutionJournalError("EXACT_REQUEST_PROOF_MISMATCH")

    def _validate_exact_read_proof(self, proof: ReadReservationProof) -> None:
        exact = self._exact_request_for_proof(proof.request_sha256)
        if exact.authority_sha256 in self.intent_bound_recovery_authorities:
            expected_kind = _INTENT_BOUND_RECOVERY_READ_KINDS.get(exact.reserved_request.path)
            if (
                proof.purpose is not ReadPurpose.EVIDENCE
                or proof.read_kind is not expected_kind
                or proof.source_attempt_id is not None
                or proof.client_id is not None
            ):
                raise ExecutionJournalError("INTENT_BOUND_RECOVERY_READ_PROOF_MISMATCH")
        ledger_authority_sha256 = self._primary_authority_sha256(exact.authority_sha256)
        authority = self.session_authorities[ledger_authority_sha256]
        binding = self.intent_chain_bindings[ledger_authority_sha256]
        try:
            expected = ReadReservationProof.from_reserved_request(
                exact.reserved_request,
                read_kind=proof.read_kind,
                purpose=proof.purpose,
                generation=exact.generation,
                deadline_ns=exact.deadline_ns,
                source_attempt_id=proof.source_attempt_id,
                client_id=proof.client_id,
                authorization_id=proof.authorization_id,
            )
        except ExecutionJournalError as exc:
            raise ExecutionJournalError("EXACT_REQUEST_PROOF_MISMATCH") from exc
        if (
            proof != expected
            or proof.authorization_id != authority.authorization_id
            or proof.intent_sha256 != binding.intent_sha256
        ):
            raise ExecutionJournalError("EXACT_REQUEST_PROOF_MISMATCH")

    def _complete_exact_request(self, request_sha256: str) -> None:
        exact = self.exact_request_reservations.get(request_sha256)
        if exact is None:
            return
        pending = self.exact_pending_requests[
            self._primary_authority_sha256(exact.authority_sha256)
        ]
        if request_sha256 not in pending:
            raise ExecutionJournalError("EXACT_REQUEST_NOT_PENDING")
        pending.remove(request_sha256)

    def request_ledger_snapshot(self, authority_sha256: str) -> RequestLedgerSnapshot:
        authority = self.session_authorities.get(authority_sha256)
        if authority is None:
            raise ExecutionJournalError("SESSION_AUTHORITY_NOT_FOUND")
        binding = self.intent_chain_bindings.get(authority_sha256)
        pending_pre_intent = ()
        pending_sha = self.pre_intent_pending.get(authority_sha256)
        if pending_sha is not None:
            pending_pre_intent = (self.pre_intent_reservations[pending_sha],)
        exact_requests = tuple(
            exact.reserved_request
            for exact in self.exact_request_reservations.values()
            if self._primary_authority_sha256(exact.authority_sha256) == authority_sha256
        )
        pending_requests = tuple(
            self.exact_request_reservations[request_sha].reserved_request
            for request_sha in self.exact_pending_requests[authority_sha256]
        )
        return RequestLedgerSnapshot(
            authority=authority,
            bound_intent_sha256=(binding.intent_sha256 if binding else None),
            last_ledger=self.request_ledgers[authority_sha256],
            pending_pre_intent_reads=pending_pre_intent,
            pending_requests=pending_requests,
            completed_pre_intent_paths=tuple(self.completed_pre_intent_paths[authority_sha256]),
            retryable_logical_request_sha256=(
                self.retryable_logical_requests.get(authority_sha256)
            ),
            exact_reserved_requests=exact_requests,
        )

    def _apply_mutation_reserved(self, proof: MutationReservationProof) -> None:
        if proof.request_sha256 in self.mutation_reservations:
            raise ExecutionJournalError("MUTATION_RESERVATION_ALREADY_EXISTS")
        if self.active_generation != proof.generation:
            raise ExecutionJournalError("MUTATION_RESERVATION_GENERATION_NOT_ACTIVE")
        self._validate_exact_mutation_proof(proof)
        sequence_key = (proof.authorization_id, proof.intent_sha256)
        previous_sequence = self.last_request_sequence.get(sequence_key)
        if previous_sequence is not None and proof.monotonic_sequence <= previous_sequence:
            raise ExecutionJournalError("REQUEST_SEQUENCE_NOT_MONOTONIC")
        capability = self.generation_capabilities[proof.generation]
        primary_purposes = {
            MutationPurpose.PRIMARY_CREATE,
            MutationPurpose.PRIMARY_CANCEL,
            MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
        }
        recovery_purposes = {
            MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
            MutationPurpose.RECOVERY_OWNED_FILL_CLOSE,
        }
        if (
            capability is GenerationCapability.PRIMARY and proof.purpose not in primary_purposes
        ) or (
            capability is GenerationCapability.RECOVERY and proof.purpose not in recovery_purposes
        ):
            raise ExecutionJournalError("MUTATION_RESERVATION_PURPOSE_MISMATCH")
        if proof.kind is not MutationKind.CREATE:
            source = self.attempts.get(proof.source_attempt_id or "")
            if source is None:
                raise ExecutionJournalError("MUTATION_RESERVATION_SOURCE_REQUIRED")
            if (
                proof.authorization_id != source.authorization_id
                or proof.intent_sha256 != source.intent_sha256
            ):
                raise ExecutionJournalError("MUTATION_RESERVATION_LINEAGE_MISMATCH")
            if proof.kind is MutationKind.CANCEL:
                observation = self.observations.get(proof.precondition_sha256 or "")
                if observation is None:
                    raise ExecutionJournalError("FRESH_OPEN_OBSERVATION_REQUIRED")
                if (
                    observation.source_attempt_id != source.attempt_id
                    or observation.source_authorization_id != source.authorization_id
                    or observation.source_client_id != source.client_id
                    or observation.generation != proof.generation
                    or proof.client_id != source.client_id
                ):
                    raise ExecutionJournalError("MUTATION_RESERVATION_OBSERVATION_MISMATCH")
            else:
                owned_proof = self.owned_fill_close_proofs.get(proof.precondition_sha256 or "")
                if owned_proof is None:
                    raise ExecutionJournalError("OWNED_FILL_CLOSE_PROOF_REQUIRED")
                expected_close_id = build_emergency_client_order_id(
                    source.runtime_commit,
                    source.session_nonce,
                )
                if (
                    owned_proof.source_attempt_id != source.attempt_id
                    or owned_proof.source_authorization_id != source.authorization_id
                    or owned_proof.source_intent_sha256 != source.intent_sha256
                    or owned_proof.source_runtime_commit != source.runtime_commit
                    or owned_proof.source_session_nonce != source.session_nonce
                    or owned_proof.generation != proof.generation
                    or proof.client_id != expected_close_id
                    or proof.parameters_sha256
                    != owned_close_parameters_sha256(
                        quantity=owned_proof.residual_quantity,
                        client_id=expected_close_id,
                    )
                ):
                    raise ExecutionJournalError("MUTATION_RESERVATION_OWNED_FILL_MISMATCH")
        self.mutation_reservations[proof.request_sha256] = proof
        self.last_request_sequence[sequence_key] = proof.monotonic_sequence

    def _apply_read_prepared(
        self,
        proof: ReadReservationProof,
        record: JournalRecord,
    ) -> None:
        if proof.request_sha256 in self.read_reservations:
            raise ExecutionJournalError("READ_RESERVATION_ALREADY_EXISTS")
        if self.active_generation != proof.generation:
            raise ExecutionJournalError("READ_GENERATION_NOT_ACTIVE")
        self._validate_exact_read_proof(proof)
        sequence_key = (proof.authorization_id, proof.intent_sha256)
        previous_sequence = self.last_request_sequence.get(sequence_key)
        if previous_sequence is not None and proof.monotonic_sequence <= previous_sequence:
            raise ExecutionJournalError("REQUEST_SEQUENCE_NOT_MONOTONIC")
        if proof.source_attempt_id is not None:
            source = self._find_attempt(proof.source_attempt_id)
            if (
                proof.client_id != source.client_id
                or proof.authorization_id != source.authorization_id
                or proof.intent_sha256 != source.intent_sha256
            ):
                raise ExecutionJournalError("READ_SOURCE_LINEAGE_MISMATCH")
        self.read_reservations[proof.request_sha256] = proof
        self.read_prepared_records[proof.request_sha256] = (
            record.sequence,
            record.digest,
        )
        self.last_request_sequence[sequence_key] = proof.monotonic_sequence

    def _apply_read_result(
        self,
        proof: ReadResultProof,
        record: JournalRecord,
    ) -> None:
        reservation = self.read_reservations.get(proof.request_sha256)
        if reservation is None:
            raise ExecutionJournalError("READ_PREPARED_REQUIRED")
        prepared_record = self.read_prepared_records[proof.request_sha256]
        if prepared_record != (
            proof.prepared_record_sequence,
            proof.prepared_record_digest,
        ):
            raise ExecutionJournalError("READ_PREPARED_RECORD_MISMATCH")
        if (
            proof.generation != reservation.generation
            or proof.monotonic_sequence != reservation.monotonic_sequence
            or proof.read_kind is not reservation.read_kind
            or proof.observed_at_ns > reservation.deadline_ns
        ):
            raise ExecutionJournalError("READ_RESULT_BINDING_MISMATCH")
        if self.active_generation != proof.generation:
            raise ExecutionJournalError("READ_GENERATION_NOT_ACTIVE")
        if (
            proof.request_sha256 in self.read_result_by_request
            or proof.request_sha256 in self.read_failure_by_request
        ):
            raise ExecutionJournalError("READ_RESULT_ALREADY_EXISTS")
        self.read_results[proof.result_proof_sha256] = proof
        self.read_result_records[proof.result_proof_sha256] = (
            record.sequence,
            record.digest,
        )
        self.read_result_by_request[proof.request_sha256] = proof.result_proof_sha256
        self._complete_exact_request(proof.request_sha256)

    def _apply_exact_read_failure(self, failure: ExactReadFailure) -> None:
        reservation = self.read_reservations.get(failure.request_sha256)
        if reservation is None:
            raise ExecutionJournalError("READ_PREPARED_REQUIRED")
        if (
            reservation.proof_sha256 != failure.read_proof_sha256
            or self.read_prepared_records[failure.request_sha256]
            != (
                failure.prepared_record_sequence,
                failure.prepared_record_digest,
            )
            or reservation.generation != failure.generation
            or reservation.monotonic_sequence != failure.monotonic_sequence
        ):
            raise ExecutionJournalError("EXACT_READ_FAILURE_BINDING_MISMATCH")
        exact = self.exact_request_reservations.get(failure.request_sha256)
        if exact is None:
            raise ExecutionJournalError("EXACT_REQUEST_RESERVATION_REQUIRED")
        if (
            self.active_generation != failure.generation
            or failure.observed_at_ns > exact.deadline_ns
        ):
            raise ExecutionJournalError("EXACT_READ_FAILURE_DEADLINE_OR_GENERATION")
        if (
            failure.request_sha256 in self.read_result_by_request
            or failure.request_sha256 in self.read_failure_by_request
        ):
            raise ExecutionJournalError("EXACT_READ_FAILURE_ALREADY_EXISTS")
        ledger_authority_sha256 = self._primary_authority_sha256(exact.authority_sha256)
        if self.request_ledgers[ledger_authority_sha256] != exact.reserved_request.ledger:
            raise ExecutionJournalError("EXACT_READ_FAILURE_NOT_LATEST")
        self._complete_exact_request(failure.request_sha256)
        self.request_ledgers[ledger_authority_sha256] = replace(
            self.request_ledgers[ledger_authority_sha256],
            retryable_read_sha256=exact.reserved_request.logical_request_sha256,
        )
        self.retryable_logical_requests[ledger_authority_sha256] = (
            exact.reserved_request.logical_request_sha256
        )
        self.exact_read_failures[failure.failure_proof_sha256] = failure
        self.read_failure_by_request[failure.request_sha256] = failure.failure_proof_sha256

    def _apply_owned_fill_close_proof(self, proof: OwnedFillCloseProof) -> None:
        if proof.proof_sha256 in self.owned_fill_close_proofs:
            raise ExecutionJournalError("OWNED_FILL_CLOSE_PROOF_ALREADY_EXISTS")
        if self.active_generation != proof.generation:
            raise ExecutionJournalError("OWNED_FILL_CLOSE_GENERATION_NOT_ACTIVE")
        source = self._find_attempt(proof.source_attempt_id)
        capability = self.generation_capabilities[proof.generation]
        source_frontier = self.frontiers[source.attempt_id]
        if capability is GenerationCapability.PRIMARY:
            source_allowed = (
                source.generation == proof.generation
                and source.kind in {MutationKind.CREATE, MutationKind.CANCEL}
                and source_frontier is FrontierState.CONFIRMED
            )
        else:
            source_allowed = (
                source.generation in self.reaped_generations
                and source.kind in {MutationKind.CREATE, MutationKind.CANCEL}
                and source_frontier in {FrontierState.UNKNOWN, FrontierState.CONFIRMED}
            )
        if not source_allowed:
            raise ExecutionJournalError("OWNED_FILL_CLOSE_SOURCE_NOT_AUTHORIZED")
        if (
            proof.source_authorization_id != source.authorization_id
            or proof.source_intent_sha256 != source.intent_sha256
            or proof.source_runtime_commit != source.runtime_commit
            or proof.source_session_nonce != source.session_nonce
            or proof.source_client_id != source.client_id
        ):
            raise ExecutionJournalError("OWNED_FILL_CLOSE_LINEAGE_MISMATCH")
        expected_evidence = (
            (
                proof.order_result,
                ReadKind.ORDER,
                ReadOutcome.OWNED_ORDER_FILL_CONFIRMED,
                "/fapi/v1/order",
                "ORDER_OBSERVATION",
            ),
            (
                proof.trade_result,
                ReadKind.TRADE,
                ReadOutcome.OWNED_TRADE_FILL_CONFIRMED,
                "/fapi/v1/userTrades",
                "USER_TRADES",
            ),
            (
                proof.account_result,
                ReadKind.ACCOUNT,
                ReadOutcome.OWNED_ACCOUNT_POSITION_CONFIRMED,
                "/fapi/v2/account",
                "ACCOUNT",
            ),
            (
                proof.symbol_filter_result,
                ReadKind.SYMBOL_FILTER,
                ReadOutcome.FILTER_SNAPSHOT_CONFIRMED,
                "/fapi/v1/exchangeInfo",
                "EXCHANGE_INFO",
            ),
            (
                proof.mark_price_result,
                ReadKind.MARK_PRICE,
                ReadOutcome.MARK_PRICE_CONFIRMED,
                "/fapi/v1/premiumIndex",
                "MARK_PRICE",
            ),
        )
        request_sequences: list[int] = []
        for (
            reference,
            expected_kind,
            expected_outcome,
            expected_path,
            expected_transport_kind,
        ) in expected_evidence:
            result = self.read_results.get(reference.result_proof_sha256)
            if result is None:
                raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_RESULT_REQUIRED")
            if self.read_result_records[reference.result_proof_sha256] != (
                reference.record_sequence,
                reference.record_digest,
            ):
                raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_RECORD_MISMATCH")
            reservation = self.read_reservations[result.request_sha256]
            exact = self.exact_request_reservations.get(result.request_sha256)
            if (
                result.read_kind is not expected_kind
                or result.outcome is not expected_outcome
                or reference.request_sha256 != result.request_sha256
                or reference.transport_result_sha256 != result.result_sha256
                or reference.logical_request_sha256 != reservation.logical_request_sha256
                or reference.transport_kind != expected_transport_kind
                or exact is None
                or exact.reserved_request.logical_request_sha256 != reference.logical_request_sha256
                or result.generation != proof.generation
                or reservation.purpose is not ReadPurpose.OWNED_FILL_CLOSE
                or reservation.path != expected_path
                or reservation.source_attempt_id != source.attempt_id
                or reservation.client_id != source.client_id
                or reservation.authorization_id != source.authorization_id
                or reservation.intent_sha256 != source.intent_sha256
                or reservation.generation != proof.generation
            ):
                raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_BINDING_MISMATCH")
            request_sequences.append(reservation.monotonic_sequence)
        if proof.observed_after_http_attempt != max(request_sequences):
            raise ExecutionJournalError("OWNED_FILL_CLOSE_SEQUENCE_MISMATCH")
        self.owned_fill_close_proofs[proof.proof_sha256] = proof

    def _apply_attempt_prepared(self, attempt: MutationAttempt) -> None:
        if attempt.attempt_id in self.attempts:
            raise ExecutionJournalError("ATTEMPT_ALREADY_EXISTS")
        if self.active_generation != attempt.generation:
            raise ExecutionJournalError("ATTEMPT_GENERATION_NOT_ACTIVE")
        if (
            self.generation_capabilities[attempt.generation] is GenerationCapability.RECOVERY
            and attempt.kind is MutationKind.CREATE
        ):
            raise ExecutionJournalError("RECOVERY_MUTATION_FORBIDDEN")
        proof = self.mutation_reservations.get(attempt.reservation_sha256)
        if proof is None:
            raise ExecutionJournalError("MUTATION_RESERVATION_REQUIRED")
        exact = self.exact_request_reservations.get(attempt.reservation_sha256)
        if exact is None:
            raise ExecutionJournalError("EXACT_REQUEST_RESERVATION_REQUIRED")
        if (
            attempt.reservation_sha256
            not in self.exact_pending_requests[
                self._primary_authority_sha256(exact.authority_sha256)
            ]
        ):
            raise ExecutionJournalError("EXACT_REQUEST_NOT_PENDING")
        if attempt.reservation_sha256 in self.consumed_mutation_reservations:
            raise ExecutionJournalError("MUTATION_RESERVATION_CONSUMED")
        if (
            proof.kind is not attempt.kind
            or proof.generation != attempt.generation
            or proof.retry_index != attempt.retry_index
            or attempt.deadline_ns > proof.deadline_ns
            or proof.client_id != attempt.client_id
            or proof.authorization_id != attempt.authorization_id
            or proof.intent_sha256 != attempt.intent_sha256
            or (
                attempt.kind is MutationKind.CANCEL
                and proof.precondition_sha256 != attempt.fresh_open_proof_sha256
            )
        ):
            raise ExecutionJournalError("MUTATION_RESERVATION_MISMATCH")
        if attempt.generation in self.generation_inflight_attempts:
            raise ExecutionJournalError("MUTATION_ALREADY_IN_FLIGHT")
        lineage = (
            attempt.authorization_id,
            attempt.intent_sha256,
            attempt.runtime_commit,
            attempt.session_nonce,
        )
        budget = {
            MutationKind.CREATE: 1,
            MutationKind.CANCEL: 2,
            MutationKind.EMERGENCY_CLOSE: 1,
        }[attempt.kind]
        used = sum(
            1
            for previous in self.attempts.values()
            if previous.kind is attempt.kind
            and (
                previous.authorization_id,
                previous.intent_sha256,
                previous.runtime_commit,
                previous.session_nonce,
            )
            == lineage
        )
        if used >= budget:
            raise ExecutionJournalError("MUTATION_BUDGET_EXHAUSTED")
        capability = self.generation_capabilities[attempt.generation]
        source = self.attempts.get(proof.source_attempt_id or "")
        if capability is GenerationCapability.RECOVERY:
            if attempt.kind is MutationKind.CREATE:
                raise ExecutionJournalError("RECOVERY_MUTATION_FORBIDDEN")
            source_id = attempt.recovery_of_attempt_id
            source = self.attempts.get(source_id or "")
            source_frontier = self.frontiers.get(source_id or "")
            source_allows_cleanup = (
                source is not None
                and source.kind
                in {
                    MutationKind.CREATE,
                    MutationKind.CANCEL,
                }
                and source_frontier in {FrontierState.UNKNOWN, FrontierState.CONFIRMED}
            )
            if source_id is None or not source_allows_cleanup:
                raise ExecutionJournalError("RECOVERY_MUTATION_NOT_AUTHORIZED")
            if (
                attempt.authorization_id != source.authorization_id
                or attempt.intent_sha256 != source.intent_sha256
                or attempt.runtime_commit != source.runtime_commit
                or attempt.session_nonce != source.session_nonce
                or proof.source_attempt_id != source.attempt_id
                or (attempt.kind is MutationKind.CANCEL and attempt.client_id != source.client_id)
            ):
                raise ExecutionJournalError("RECOVERY_MUTATION_LINEAGE_MISMATCH")
        elif attempt.recovery_of_attempt_id is not None:
            raise ExecutionJournalError("RECOVERY_LINK_FORBIDDEN")

        if attempt.kind is MutationKind.CANCEL:
            if source is None:
                raise ExecutionJournalError("MUTATION_RESERVATION_SOURCE_REQUIRED")
            observation = self.observations.get(attempt.fresh_open_proof_sha256 or "")
            if observation is None:
                raise ExecutionJournalError("FRESH_OPEN_OBSERVATION_REQUIRED")
            if observation.observation_sha256 in self.consumed_observations:
                raise ExecutionJournalError("FRESH_OPEN_OBSERVATION_ALREADY_CONSUMED")
            if (
                observation.source_attempt_id != source.attempt_id
                or observation.source_authorization_id != attempt.authorization_id
                or observation.source_client_id != attempt.client_id
                or observation.generation != attempt.generation
            ):
                raise ExecutionJournalError("MUTATION_OBSERVATION_MISMATCH")
            self.consumed_observations.add(observation.observation_sha256)
        elif attempt.kind is MutationKind.EMERGENCY_CLOSE:
            owned_proof = self.owned_fill_close_proofs.get(proof.precondition_sha256 or "")
            if owned_proof is None:
                raise ExecutionJournalError("OWNED_FILL_CLOSE_PROOF_REQUIRED")
            if owned_proof.proof_sha256 in self.consumed_owned_fill_close_proofs:
                raise ExecutionJournalError("OWNED_FILL_CLOSE_PROOF_ALREADY_CONSUMED")
            self.consumed_owned_fill_close_proofs.add(owned_proof.proof_sha256)
        self.consumed_mutation_reservations.add(attempt.reservation_sha256)
        self.attempts[attempt.attempt_id] = attempt
        self.frontiers[attempt.attempt_id] = FrontierState.PREPARED
        self.generation_inflight_attempts[attempt.generation] = attempt.attempt_id

    def _find_attempt(self, attempt_id: str) -> MutationAttempt:
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            raise ExecutionJournalError("ATTEMPT_NOT_FOUND")
        return attempt

    def _apply_go(self, event: _GoDurable) -> None:
        attempt = self._find_attempt(event.attempt_id)
        if (
            event.generation != attempt.generation
            or self.active_generation != attempt.generation
            or self.frontiers[event.attempt_id] is not FrontierState.PREPARED
        ):
            raise ExecutionJournalError("GO_REQUIRES_PREPARED")
        self.frontiers[event.attempt_id] = FrontierState.GO_DURABLE

    def _apply_confirmation(self, event: _AttemptConfirmed) -> None:
        attempt = self._find_attempt(event.attempt_id)
        if (
            event.generation != attempt.generation
            or not _is_sha256(event.result_sha256)
            or self.frontiers[event.attempt_id] is not FrontierState.GO_DURABLE
        ):
            raise ExecutionJournalError("CONFIRMATION_REQUIRES_GO")
        self.frontiers[event.attempt_id] = FrontierState.CONFIRMED
        self.generation_inflight_attempts.pop(attempt.generation, None)
        self._complete_exact_request(attempt.reservation_sha256)

    def _apply_reap(self, event: _GenerationReaped) -> None:
        receipt = event.receipt
        if self.active_generation != receipt.generation:
            raise ExecutionJournalError("REAP_GENERATION_MISMATCH")
        admission = self.generation_admissions[receipt.generation]
        if receipt.process_identity_sha256 != admission.process_identity_sha256:
            raise ExecutionJournalError("REAP_IDENTITY_MISMATCH")
        if self.generation_admission_records[receipt.generation] != (
            receipt.admission_record_sequence,
            receipt.admission_record_digest,
        ):
            raise ExecutionJournalError("REAP_ADMISSION_RECORD_MISMATCH")
        self.reaped_generations.add(receipt.generation)
        self.reap_receipts[receipt.generation] = receipt
        self.active_generation = None

    def _apply_observation(self, observation: ReconciliationObservation) -> None:
        if observation.observation_sha256 in self.observations:
            raise ExecutionJournalError("OBSERVATION_ALREADY_EXISTS")
        if self.active_generation != observation.generation:
            raise ExecutionJournalError("OBSERVATION_GENERATION_NOT_ACTIVE")
        source = self._find_attempt(observation.source_attempt_id)
        source_frontier = self.frontiers[source.attempt_id]
        capability = self.generation_capabilities[observation.generation]
        source_allowed = (
            capability is GenerationCapability.PRIMARY
            and source.kind in {MutationKind.CREATE, MutationKind.CANCEL}
            and source_frontier is FrontierState.CONFIRMED
            and source.generation == observation.generation
        ) or (
            capability is GenerationCapability.RECOVERY
            and (
                (source.kind is MutationKind.CANCEL and source_frontier is FrontierState.UNKNOWN)
                or (
                    source.kind is MutationKind.CREATE
                    and source_frontier in {FrontierState.UNKNOWN, FrontierState.CONFIRMED}
                    and source.generation in self.reaped_generations
                )
            )
        )
        if not source_allowed:
            raise ExecutionJournalError("OBSERVATION_SOURCE_NOT_RECOVERABLE")
        if (
            observation.source_authorization_id != source.authorization_id
            or observation.source_client_id != source.client_id
        ):
            raise ExecutionJournalError("OBSERVATION_SOURCE_LINEAGE_MISMATCH")
        result = self.read_results.get(observation.read_result_proof_sha256)
        if result is None:
            raise ExecutionJournalError("READ_RESULT_REQUIRED")
        result_record = self.read_result_records[observation.read_result_proof_sha256]
        reservation = self.read_reservations[result.request_sha256]
        expected_status = {
            ReadOutcome.ORDER_NEW: ReconciledOrderStatus.NEW,
            ReadOutcome.ORDER_PARTIALLY_FILLED: ReconciledOrderStatus.PARTIALLY_FILLED,
        }.get(result.outcome)
        if expected_status is None:
            raise ExecutionJournalError("FRESH_OPEN_RESULT_REQUIRED")
        if (
            observation.order_status is not expected_status
            or observation.read_reservation_sha256 != result.request_sha256
            or result_record
            != (
                observation.read_result_record_sequence,
                observation.read_result_record_digest,
            )
            or reservation.purpose is not ReadPurpose.ORDER_RECONCILIATION
            or reservation.source_attempt_id != source.attempt_id
            or reservation.client_id != source.client_id
            or reservation.authorization_id != source.authorization_id
            or reservation.intent_sha256 != source.intent_sha256
            or reservation.generation != observation.generation
        ):
            raise ExecutionJournalError("OBSERVATION_READ_RESULT_MISMATCH")
        self.observations[observation.observation_sha256] = observation

    def _apply_resolution(self, event: _AttemptResolved) -> None:
        attempt = self._find_attempt(event.attempt_id)
        if (
            event.generation != attempt.generation
            or attempt.generation not in self.reaped_generations
        ):
            raise ExecutionJournalError("OUTCOME_REQUIRES_REAP")
        if event.boundary_result is BoundaryResult.CONFIRMED:
            raise ExecutionJournalError("INVALID_BOUNDARY_RESOLUTION")
        current = self.frontiers[event.attempt_id]
        if current is FrontierState.PREPARED:
            if event.state is not FrontierState.NOT_DISPATCHED:
                raise ExecutionJournalError("UNKNOWN_REQUIRES_GO")
        elif current is FrontierState.GO_DURABLE:
            if event.state is not FrontierState.UNKNOWN:
                raise ExecutionJournalError("NOT_DISPATCHED_REQUIRES_NO_GO")
        else:
            raise ExecutionJournalError("OUTCOME_ALREADY_TERMINAL")
        self.frontiers[event.attempt_id] = event.state
        self.generation_inflight_attempts.pop(attempt.generation, None)
        self._complete_exact_request(attempt.reservation_sha256)


def _state_from_records(records: tuple[JournalRecord, ...]) -> _JournalState:
    state = _JournalState()
    for record in records:
        state.apply(record)
    if not state.has_genesis:
        raise ExecutionJournalError("JOURNAL_TRUNCATED")
    return state


def _read_records(fd: int) -> tuple[JournalRecord, ...]:
    os.lseek(fd, 0, os.SEEK_SET)
    records: list[JournalRecord] = []
    expected_previous = ZERO_DIGEST
    with os.fdopen(os.dup(fd), "rb") as stream:
        while True:
            raw_line = stream.readline(MAX_RECORD_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > MAX_RECORD_BYTES:
                raise ExecutionJournalError("JOURNAL_RECORD_OVERSIZED")
            if not raw_line.endswith(b"\n"):
                raise ExecutionJournalError("JOURNAL_TRUNCATED")
            record = _decode_record(
                raw_line,
                expected_sequence=len(records) + 1,
                expected_previous_digest=expected_previous,
            )
            records.append(record)
            expected_previous = record.digest
    result = tuple(records)
    _state_from_records(result)
    return result


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short journal write")
        remaining = remaining[written:]


def _append_and_fsync(fd: int, data: bytes) -> None:
    try:
        _write_all(fd, data)
    except OSError as exc:
        raise ExecutionJournalError("JOURNAL_APPEND_FAILED") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ExecutionJournalError("JOURNAL_FSYNC_FAILED") from exc


def _validate_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise ExecutionJournalError("JOURNAL_NOT_SAFE_REGULAR_FILE")
    if file_stat.st_uid != os.getuid():
        raise ExecutionJournalError("JOURNAL_NOT_OWNER")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise ExecutionJournalError("JOURNAL_INSECURE_MODE")


@dataclass(frozen=True, slots=True)
class _JournalHead:
    sequence: int
    digest: str


def _head_bytes(sequence: int, digest: str) -> bytes:
    if not _is_positive_int(sequence) or not _is_sha256(digest):
        raise ExecutionJournalError("JOURNAL_ANCHOR_VALUE")
    return (
        _canonical_json(
            {
                "schema_version": HEAD_SCHEMA_VERSION,
                "sequence": sequence,
                "digest": digest,
            }
        )
        + b"\n"
    )


def _decode_head(raw: bytes) -> _JournalHead:
    if len(raw) > MAX_RECORD_BYTES:
        raise ExecutionJournalError("JOURNAL_ANCHOR_OVERSIZED")
    if not raw.endswith(b"\n"):
        raise ExecutionJournalError("JOURNAL_ANCHOR_TRUNCATED")
    try:
        value = json.loads(
            raw[:-1].decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ExecutionJournalError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionJournalError("JOURNAL_ANCHOR_MALFORMED") from exc
    item = _require_exact_fields(
        value,
        frozenset({"schema_version", "sequence", "digest"}),
        "JOURNAL_ANCHOR_FIELDS",
    )
    if item["schema_version"] != HEAD_SCHEMA_VERSION:
        raise ExecutionJournalError("JOURNAL_ANCHOR_SCHEMA_VERSION")
    if not _is_positive_int(item["sequence"]):
        raise ExecutionJournalError("JOURNAL_ANCHOR_SEQUENCE")
    if not _is_sha256(item["digest"]):
        raise ExecutionJournalError("JOURNAL_ANCHOR_DIGEST")
    if raw != _canonical_json(value) + b"\n":
        raise ExecutionJournalError("JOURNAL_ANCHOR_NONCANONICAL")
    return _JournalHead(
        sequence=item["sequence"],  # type: ignore[arg-type]
        digest=item["digest"],  # type: ignore[arg-type]
    )


def _read_head(parent_fd: int, name: str) -> _JournalHead:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_file_stat(entry_stat)
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise ExecutionJournalError("JOURNAL_ANCHOR_MISSING") from exc
    except ExecutionJournalError:
        raise
    except OSError as exc:
        raise ExecutionJournalError("JOURNAL_ANCHOR_OPEN_FAILED") from exc
    try:
        opened_stat = os.fstat(fd)
        _validate_file_stat(opened_stat)
        if (entry_stat.st_dev, entry_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise ExecutionJournalError("JOURNAL_ANCHOR_PATH_RACE")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        return _decode_head(raw)
    finally:
        os.close(fd)


def _write_head(parent_fd: int, name: str, head: _JournalHead) -> None:
    encoded = _head_bytes(head.sequence, head.digest)
    temporary = f".{name}.{os.getpid()}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd: int | None = None
    try:
        try:
            fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_ANCHOR_CREATE_FAILED") from exc
        os.fchmod(fd, 0o600)
        _validate_file_stat(os.fstat(fd))
        try:
            _write_all(fd, encoded)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_ANCHOR_WRITE_FAILED") from exc
        try:
            os.fsync(fd)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_ANCHOR_FSYNC_FAILED") from exc
        os.close(fd)
        fd = None
        try:
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_ANCHOR_REPLACE_FAILED") from exc
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_DIRECTORY_FSYNC_FAILED") from exc
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def _validate_or_repair_head(
    parent_fd: int,
    name: str,
    records: tuple[JournalRecord, ...],
) -> None:
    head = _read_head(parent_fd, name)
    if head.sequence > len(records):
        raise ExecutionJournalError("JOURNAL_ANCHOR_AHEAD")
    anchored_record = records[head.sequence - 1]
    if anchored_record.digest != head.digest:
        raise ExecutionJournalError("JOURNAL_ANCHOR_DIGEST_MISMATCH")
    latest = records[-1]
    if head.sequence < latest.sequence:
        _write_head(
            parent_fd,
            name,
            _JournalHead(sequence=latest.sequence, digest=latest.digest),
        )


T = TypeVar("T")


class ExecutionJournal:
    """Owner-only canonical JSONL journal and validated frontier state machine."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        if not self._path.name or self._path.name in {".", ".."}:
            raise ExecutionJournalError("INVALID_JOURNAL_PATH")
        self._ensure_created_or_valid()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def anchor_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.head")

    def records(self) -> tuple[JournalRecord, ...]:
        with self._locked_existing_fd() as fd:
            return self._read_validated_records(fd)

    def admit_generation(
        self,
        admission: DurableGenerationAdmission,
        capability: GenerationCapability,
    ) -> JournalRecord:
        if type(admission) is not DurableGenerationAdmission:
            raise ExecutionJournalError("INVALID_GENERATION_ADMISSION")
        return self._append_event(
            lambda _state: _GenerationAdmitted(
                generation=admission.generation,
                capability=capability,
                process_identity_sha256=admission.process_identity_sha256,
            )
        )

    def reconcile_staged_generation(
        self,
        proof: StagedGenerationRecoveryProof,
    ) -> JournalRecord:
        """Fill a cross-WAL generation gap from externally verified process anchors."""

        if type(proof) is not StagedGenerationRecoveryProof:
            raise ExecutionJournalError("INVALID_STAGED_GENERATION_RECOVERY")
        return self._append_event(lambda _state: _StagedGenerationReconciled(proof))

    def establish_session_authority(
        self,
        authority: SessionAuthority,
    ) -> JournalRecord:
        """Durably establish non-secret authority for the eleven pre-intent reads."""

        if type(authority) is not SessionAuthority:
            raise ExecutionJournalError("INVALID_SESSION_AUTHORITY")
        return self._append_event(lambda _state: _SessionAuthorityEstablished(authority))

    def issue_recovery_session_authority(
        self,
        *,
        primary_authority_sha256: str,
        source_attempt_id: str,
    ) -> JournalRecord:
        """Issue one durable read/cleanup-only authority for the active recovery."""

        if not _is_sha256(primary_authority_sha256) or not _is_sha256(source_attempt_id):
            raise ExecutionJournalError("INVALID_RECOVERY_SESSION_AUTHORITY")

        def event_for(state: _JournalState) -> _RecoverySessionAuthorityIssued:
            generation = state.active_generation
            if generation is None:
                raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_NOT_ACTIVE")
            source = state._find_attempt(source_attempt_id)
            authority = RecoverySessionAuthority.build(
                primary_authority_sha256=primary_authority_sha256,
                source_attempt=source,
                generation=generation,
            )
            return _RecoverySessionAuthorityIssued(authority)

        return self._append_event(event_for)

    def issue_intent_bound_recovery_authority(
        self,
        *,
        primary_authority_sha256: str,
    ) -> JournalRecord:
        """Issue read-only recovery for a durable intent with no attempt."""

        if not _is_sha256(primary_authority_sha256):
            raise ExecutionJournalError("INVALID_INTENT_BOUND_RECOVERY_AUTHORITY")

        def event_for(state: _JournalState) -> _IntentBoundRecoveryAuthorityIssued:
            generation = state.active_generation
            if generation is None:
                raise ExecutionJournalError("RECOVERY_SESSION_GENERATION_NOT_ACTIVE")
            primary = state.session_authorities.get(primary_authority_sha256)
            binding = state.intent_chain_bindings.get(primary_authority_sha256)
            if primary is None or binding is None:
                raise ExecutionJournalError("PRIMARY_SESSION_AUTHORITY_REQUIRED")
            if any(
                attempt.intent_sha256 == binding.intent_sha256
                or attempt.authorization_id == primary.authorization_id
                for attempt in state.attempts.values()
            ):
                raise ExecutionJournalError("INTENT_BOUND_RECOVERY_REQUIRES_NO_ATTEMPT")
            pending_mutations = tuple(
                request_sha256
                for request_sha256 in state.exact_pending_requests[primary_authority_sha256]
                if state.exact_request_reservations[request_sha256].reserved_request.purpose
                is not RequestPurpose.READ
            )
            if len(pending_mutations) > 1:
                raise ExecutionJournalError("INTENT_BOUND_RECOVERY_PENDING_REQUEST_MISMATCH")
            authority = IntentBoundRecoveryAuthority.build(
                primary_authority=primary,
                intent_binding=binding,
                generation=generation,
                abandoned_create_request_sha256=(
                    pending_mutations[0] if pending_mutations else None
                ),
            )
            return _IntentBoundRecoveryAuthorityIssued(authority)

        return self._append_event(event_for)

    def reserve_pre_intent_read(
        self,
        *,
        authority_sha256: str,
        path: str,
        parameters: Mapping[str, object],
        elapsed_seconds: Decimal,
        deadline_ns: int,
        retry_index: int,
    ) -> PreparedPreIntentRead:
        """Reserve and fsync one exact GET without inventing a final intent hash."""

        if (
            not _is_sha256(authority_sha256)
            or type(path) is not str
            or not isinstance(parameters, Mapping)
            or any(
                type(key) is not str or type(value) is not str for key, value in parameters.items()
            )
            or type(elapsed_seconds) is not Decimal
            or not elapsed_seconds.is_finite()
            or elapsed_seconds < 0
            or not _is_positive_int(deadline_ns)
            or type(retry_index) is not int
            or retry_index not in {0, 1}
        ):
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_REQUEST")

        def event_for(state: _JournalState) -> _PreIntentReadReserved:
            authority = state.session_authorities.get(authority_sha256)
            if authority is None:
                raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")
            previous = state.request_ledgers[authority_sha256]
            if retry_index == 1:
                if previous.read_retry_requests >= MAX_READ_RETRIES:
                    raise ExecutionJournalError("READ_RETRY_BUDGET_EXHAUSTED")
                read_retry_requests = previous.read_retry_requests + 1
            else:
                read_retry_requests = previous.read_retry_requests
            try:
                ledger = replace(
                    previous,
                    total_http_requests=previous.total_http_requests + 1,
                    read_retry_requests=read_retry_requests,
                    last_elapsed_seconds=elapsed_seconds,
                    retryable_read_sha256=None,
                )
            except MutationProtocolError as exc:
                raise ExecutionJournalError("PRE_INTENT_READ_BUDGET_EXHAUSTED") from exc
            reservation = PreIntentReadReservation.build(
                session_authority_sha256=authority_sha256,
                generation=authority.generation,
                deadline_ns=deadline_ns,
                path=path,
                parameters=parameters,
                ledger=ledger,
                elapsed_seconds=elapsed_seconds,
                retry_index=retry_index,
            )
            return _PreIntentReadReserved(reservation)

        record = self._append_event(event_for)
        event = record.event
        if type(event) is not _PreIntentReadReserved:  # pragma: no cover - typed append
            raise ExecutionJournalError("JOURNAL_EVENT_TYPE")
        return PreparedPreIntentRead(
            reservation=event.reservation,
            record_sequence=record.sequence,
            record_digest=record.digest,
        )

    def record_pre_intent_read_result(
        self,
        *,
        reservation_sha256: str,
        result_sha256: str,
        observed_at_ns: int,
    ) -> JournalRecord:
        """Durably terminate a pre-intent reservation with a sanitized digest."""

        def event_for(state: _JournalState) -> _PreIntentReadResultRecorded:
            prepared = state.pre_intent_prepared_records.get(reservation_sha256)
            if prepared is None:
                raise ExecutionJournalError("PRE_INTENT_READ_RESERVATION_REQUIRED")
            return _PreIntentReadResultRecorded(
                PreIntentReadResult.build(
                    reservation_sha256=reservation_sha256,
                    prepared_record_sequence=prepared[0],
                    prepared_record_digest=prepared[1],
                    result_sha256=result_sha256,
                    observed_at_ns=observed_at_ns,
                )
            )

        return self._append_event(event_for)

    def record_pre_intent_read_failure(
        self,
        *,
        reservation_sha256: str,
        failure: ReadFailureKind,
        io_may_have_occurred: bool,
        observed_at_ns: int,
    ) -> JournalRecord:
        """Durably record one typed failure and mint only its exact retry token."""

        if type(failure) is not ReadFailureKind:
            raise ExecutionJournalError("INVALID_PRE_INTENT_READ_FAILURE")

        def event_for(state: _JournalState) -> _PreIntentReadFailureRecorded:
            prepared = state.pre_intent_prepared_records.get(reservation_sha256)
            if prepared is None:
                raise ExecutionJournalError("PRE_INTENT_READ_RESERVATION_REQUIRED")
            return _PreIntentReadFailureRecorded(
                PreIntentReadFailure.build(
                    reservation_sha256=reservation_sha256,
                    prepared_record_sequence=prepared[0],
                    prepared_record_digest=prepared[1],
                    failure=failure,
                    io_may_have_occurred=io_may_have_occurred,
                    observed_at_ns=observed_at_ns,
                )
            )

        return self._append_event(event_for)

    def bind_persisted_intent(
        self,
        authority_sha256: str,
        persisted_intent: PersistedIntent,
    ) -> JournalRecord:
        """One-way bind the complete pre-intent chain to an exact replayed intent."""

        if not _is_sha256(authority_sha256) or type(persisted_intent) is not PersistedIntent:
            raise ExecutionJournalError("INVALID_PERSISTED_INTENT")
        try:
            replayed = load_persisted_intent(persisted_intent.path)
        except DurableIntentError as exc:
            raise ExecutionJournalError("PERSISTED_INTENT_REPLAY_FAILED") from exc
        if replayed != persisted_intent:
            raise ExecutionJournalError("PERSISTED_INTENT_REPLAY_MISMATCH")
        intent_path_sha256 = hashlib.sha256(
            _canonical_json({"path": str(replayed.path.absolute())})
        ).hexdigest()

        def event_for(state: _JournalState) -> _IntentChainBound:
            authority = state.session_authorities.get(authority_sha256)
            if authority is None:
                raise ExecutionJournalError("SESSION_AUTHORITY_REQUIRED")
            intent = replayed.intent
            if (
                intent.authorization_id != authority.authorization_id
                or intent.runtime_commit != authority.runtime_commit
                or intent.session_nonce != authority.session_nonce
                or intent.client_order_id != authority.client_id
            ):
                raise ExecutionJournalError("PERSISTED_INTENT_LINEAGE_MISMATCH")
            binding = IntentChainBinding.build(
                session_authority_sha256=authority_sha256,
                intent_sha256=intent.intent_sha256,
                intent_file_sha256=replayed.file_sha256,
                intent_path_sha256=intent_path_sha256,
                pre_intent_chain_sha256=state._pre_intent_chain_sha256(authority_sha256),
                last_ledger_sha256=_ledger_sha256(state.request_ledgers[authority_sha256]),
            )
            return _IntentChainBound(binding)

        return self._append_event(event_for)

    def record_exact_request_reservation(
        self,
        *,
        authority_sha256: str,
        generation: int,
        deadline_ns: int,
        reserved_request: ReservedRequest,
    ) -> JournalRecord:
        """Persist every exact unsigned ReservedRequest field before transport."""

        if (
            not _is_sha256(authority_sha256)
            or not _is_positive_int(generation)
            or not _is_positive_int(deadline_ns)
            or type(reserved_request) is not ReservedRequest
        ):
            raise ExecutionJournalError("INVALID_EXACT_REQUEST_RESERVATION")
        return self._append_event(
            lambda _state: _ExactRequestReserved(
                authority_sha256=authority_sha256,
                generation=generation,
                deadline_ns=deadline_ns,
                reserved_request=reserved_request,
            )
        )

    def request_ledger_snapshot(
        self,
        authority_sha256: str,
    ) -> RequestLedgerSnapshot:
        """Rebuild exact stage, counters, retry token, and pending reservations."""

        if not _is_sha256(authority_sha256):
            raise ExecutionJournalError("INVALID_SESSION_AUTHORITY_SHA256")
        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            return state.request_ledger_snapshot(authority_sha256)

    def verify_child_economic_binding(
        self,
        *,
        attempt: MutationAttempt,
        reservation_proof: MutationReservationProof,
        reserved_request: ReservedRequest,
        persisted_intent_path: Path,
    ) -> VerifiedMutationDispatchReceipt:
        """Reopen journal and intent, then return the sole request safe for I/O."""

        if (
            type(attempt) is not MutationAttempt
            or type(reservation_proof) is not MutationReservationProof
            or type(reserved_request) is not ReservedRequest
            or not isinstance(persisted_intent_path, Path)
        ):
            raise ExecutionJournalError("INVALID_MUTATION_DISPATCH_VERIFICATION")
        try:
            persisted = load_persisted_intent(persisted_intent_path)
        except DurableIntentError as exc:
            raise ExecutionJournalError("MUTATION_DISPATCH_INTENT_REPLAY_FAILED") from exc
        intent_path_sha256 = hashlib.sha256(
            _canonical_json({"path": str(persisted.path.absolute())})
        ).hexdigest()

        with self._locked_existing_fd() as fd:
            records = self._read_validated_records(fd)
            state = _state_from_records(records)
            journal_attempt = state.attempts.get(attempt.attempt_id)
            exact = state.exact_request_reservations.get(reserved_request.request_sha256)
            journal_proof = state.mutation_reservations.get(reserved_request.request_sha256)
            if (
                journal_attempt != attempt
                or exact is None
                or exact.reserved_request != reserved_request
                or journal_proof != reservation_proof
                or attempt.reservation_sha256 != reserved_request.request_sha256
                or state.frontiers.get(attempt.attempt_id) is not FrontierState.GO_DURABLE
                or state.active_generation != attempt.generation
                or state.generation_inflight_attempts.get(attempt.generation) != attempt.attempt_id
                or reserved_request.request_sha256 not in state.consumed_mutation_reservations
            ):
                raise ExecutionJournalError("MUTATION_DISPATCH_JOURNAL_MISMATCH")
            ledger_authority_sha256 = state._primary_authority_sha256(exact.authority_sha256)
            if (
                reserved_request.request_sha256
                not in state.exact_pending_requests[ledger_authority_sha256]
            ):
                raise ExecutionJournalError("MUTATION_DISPATCH_JOURNAL_MISMATCH")
            authority = state.session_authorities[ledger_authority_sha256]
            binding = state.intent_chain_bindings.get(ledger_authority_sha256)
            intent = persisted.intent
            if (
                binding is None
                or binding.session_authority_sha256 != ledger_authority_sha256
                or binding.intent_sha256 != intent.intent_sha256
                or binding.intent_file_sha256 != persisted.file_sha256
                or binding.intent_path_sha256 != intent_path_sha256
                or authority.authorization_id != intent.authorization_id
                or authority.runtime_commit != intent.runtime_commit
                or authority.session_nonce != intent.session_nonce
                or authority.client_id != intent.client_order_id
                or attempt.authorization_id != intent.authorization_id
                or attempt.runtime_commit != intent.runtime_commit
                or attempt.session_nonce != intent.session_nonce
                or attempt.intent_sha256 != intent.intent_sha256
                or reservation_proof.authorization_id != intent.authorization_id
                or reservation_proof.intent_sha256 != intent.intent_sha256
                or reserved_request.intent_sha256 != intent.intent_sha256
            ):
                raise ExecutionJournalError("MUTATION_DISPATCH_INTENT_MISMATCH")
            try:
                reservation_proof.validate_dispatch_binding(
                    reserved_request,
                    attempt,
                )
            except ExecutionJournalError as exc:
                raise ExecutionJournalError("MUTATION_DISPATCH_JOURNAL_MISMATCH") from exc

            precondition_sha256 = reservation_proof.precondition_sha256
            if attempt.kind is MutationKind.CREATE:
                expected_parameters = intent.probe_payload
                precondition_valid = (
                    reservation_proof.purpose is MutationPurpose.PRIMARY_CREATE
                    and reservation_proof.source_attempt_id is None
                    and precondition_sha256 is None
                    and attempt.client_id == intent.client_order_id
                )
            elif attempt.kind is MutationKind.CANCEL:
                expected_parameters = intent.cancel_parameters
                observation = state.observations.get(precondition_sha256 or "")
                source = state.attempts.get(reservation_proof.source_attempt_id or "")
                precondition_valid = (
                    observation is not None
                    and source is not None
                    and precondition_sha256 == attempt.fresh_open_proof_sha256
                    and precondition_sha256 in state.consumed_observations
                    and observation.source_attempt_id == source.attempt_id
                    and observation.source_authorization_id == intent.authorization_id
                    and observation.source_client_id == intent.client_order_id
                    and observation.generation == attempt.generation
                    and source.intent_sha256 == intent.intent_sha256
                    and source.runtime_commit == intent.runtime_commit
                    and source.session_nonce == intent.session_nonce
                    and attempt.client_id == intent.client_order_id
                )
            else:
                owned = state.owned_fill_close_proofs.get(precondition_sha256 or "")
                source = state.attempts.get(reservation_proof.source_attempt_id or "")
                precondition_valid = (
                    owned is not None
                    and source is not None
                    and precondition_sha256 in state.consumed_owned_fill_close_proofs
                    and owned.source_attempt_id == source.attempt_id
                    and owned.source_authorization_id == intent.authorization_id
                    and owned.source_intent_sha256 == intent.intent_sha256
                    and owned.source_runtime_commit == intent.runtime_commit
                    and owned.source_session_nonce == intent.session_nonce
                    and owned.source_client_id == intent.client_order_id
                    and owned.generation == attempt.generation
                    and owned.filter_snapshot_sha256 == intent.filter_snapshot_sha256
                    and attempt.client_id == intent.emergency_client_order_id
                )
                try:
                    expected_parameters = intent.emergency_close_payload(
                        Decimal(owned.residual_quantity) if owned is not None else Decimal(0)
                    )
                except MutationProtocolError:
                    precondition_valid = False
                    expected_parameters = {}
            if not precondition_valid or reserved_request.parameters != tuple(
                sorted(expected_parameters.items())
            ):
                raise ExecutionJournalError("MUTATION_DISPATCH_ECONOMIC_MISMATCH")

            head = records[-1]
            return VerifiedMutationDispatchReceipt._from_verified_replay(
                attempt=attempt,
                reservation_proof=reservation_proof,
                reserved_request=reserved_request,
                intent_binding=binding,
                precondition_sha256=precondition_sha256,
                journal_head_sequence=head.sequence,
                journal_head_digest=head.digest,
                _token=_VERIFIED_DISPATCH_RECEIPT_TOKEN,
            )

    def record_mutation_reservation(
        self,
        proof: MutationReservationProof,
    ) -> JournalRecord:
        if type(proof) is not MutationReservationProof:
            raise ExecutionJournalError("INVALID_MUTATION_RESERVATION")
        return self._append_event(lambda _state: _MutationReserved(proof))

    def record_read_prepared(self, proof: ReadReservationProof) -> JournalRecord:
        if type(proof) is not ReadReservationProof:
            raise ExecutionJournalError("INVALID_READ_RESERVATION")
        return self._append_event(lambda _state: _ReadPrepared(proof))

    def record_read_result(self, proof: ReadResultProof) -> JournalRecord:
        if type(proof) is not ReadResultProof:
            raise ExecutionJournalError("INVALID_READ_RESULT")
        return self._append_event(lambda _state: _ReadResultRecorded(proof))

    def record_exact_read_failure(
        self,
        *,
        request_sha256: str,
        failure: ReadFailureKind,
        io_may_have_occurred: bool,
        observed_at_ns: int,
    ) -> JournalRecord:
        """Durably fail one exact GET and preserve its sole aggregate retry token."""

        if (
            not _is_sha256(request_sha256)
            or type(failure) is not ReadFailureKind
            or type(io_may_have_occurred) is not bool
            or not _is_positive_int(observed_at_ns)
        ):
            raise ExecutionJournalError("INVALID_EXACT_READ_FAILURE")

        def event_for(state: _JournalState) -> _ExactReadFailureRecorded:
            proof = state.read_reservations.get(request_sha256)
            prepared = state.read_prepared_records.get(request_sha256)
            if proof is None or prepared is None:
                raise ExecutionJournalError("READ_PREPARED_REQUIRED")
            return _ExactReadFailureRecorded(
                ExactReadFailure.build(
                    request_sha256=request_sha256,
                    read_proof_sha256=proof.proof_sha256,
                    prepared_record_sequence=prepared[0],
                    prepared_record_digest=prepared[1],
                    generation=proof.generation,
                    monotonic_sequence=proof.monotonic_sequence,
                    failure=failure,
                    io_may_have_occurred=io_may_have_occurred,
                    observed_at_ns=observed_at_ns,
                )
            )

        return self._append_event(event_for)

    def record_owned_fill_close_proof(self, proof: OwnedFillCloseProof) -> JournalRecord:
        if type(proof) is not OwnedFillCloseProof:
            raise ExecutionJournalError("INVALID_OWNED_FILL_CLOSE_PROOF")
        return self._append_event(lambda _state: _OwnedFillCloseProven(proof))

    def prepare_attempt(self, attempt: MutationAttempt) -> JournalRecord:
        if type(attempt) is not MutationAttempt:
            raise ExecutionJournalError("INVALID_ATTEMPT")
        return self._append_event(lambda _state: _AttemptPrepared(attempt))

    def record_go(self, attempt_id: str) -> JournalRecord:
        def event_for(state: _JournalState) -> _GoDurable:
            attempt = state._find_attempt(attempt_id)
            return _GoDurable(attempt_id=attempt_id, generation=attempt.generation)

        return self._append_event(event_for)

    def record_confirmed(self, attempt_id: str, result_sha256: str) -> JournalRecord:
        def event_for(state: _JournalState) -> _AttemptConfirmed:
            attempt = state._find_attempt(attempt_id)
            return _AttemptConfirmed(
                attempt_id=attempt_id,
                generation=attempt.generation,
                result_sha256=result_sha256,
            )

        return self._append_event(event_for)

    def reap_generation(self, receipt: ProcessReapReceipt) -> JournalRecord:
        if type(receipt) is not ProcessReapReceipt:
            raise ExecutionJournalError("INVALID_REAP_RECEIPT")
        return self._append_event(lambda _state: _GenerationReaped(receipt))

    def record_reconciliation_observation(
        self,
        observation: ReconciliationObservation,
    ) -> JournalRecord:
        if type(observation) is not ReconciliationObservation:
            raise ExecutionJournalError("INVALID_RECONCILIATION_OBSERVATION")
        return self._append_event(lambda _state: _ReconciliationObserved(observation))

    def new_reconciliation_observation(
        self,
        *,
        source_attempt_id: str,
        read_result_proof_sha256: str,
    ) -> ReconciliationObservation:
        """Derive fresh-OPEN evidence only from an exact durable read result."""

        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            source = state._find_attempt(source_attempt_id)
            result = state.read_results.get(read_result_proof_sha256)
            if result is None:
                raise ExecutionJournalError("READ_RESULT_REQUIRED")
            result_record = state.read_result_records[read_result_proof_sha256]
            reservation = state.read_reservations[result.request_sha256]
            order_status = {
                ReadOutcome.ORDER_NEW: ReconciledOrderStatus.NEW,
                ReadOutcome.ORDER_PARTIALLY_FILLED: ReconciledOrderStatus.PARTIALLY_FILLED,
            }.get(result.outcome)
            if order_status is None:
                raise ExecutionJournalError("FRESH_OPEN_RESULT_REQUIRED")
            if (
                reservation.purpose is not ReadPurpose.ORDER_RECONCILIATION
                or reservation.source_attempt_id != source.attempt_id
                or reservation.client_id != source.client_id
                or reservation.authorization_id != source.authorization_id
                or reservation.intent_sha256 != source.intent_sha256
            ):
                raise ExecutionJournalError("OBSERVATION_READ_RESULT_MISMATCH")
            return ReconciliationObservation.build(
                source_attempt_id=source.attempt_id,
                source_authorization_id=source.authorization_id,
                source_client_id=source.client_id,
                generation=result.generation,
                order_status=order_status,
                read_reservation_sha256=result.request_sha256,
                read_result_proof_sha256=result.result_proof_sha256,
                read_result_record_sequence=result_record[0],
                read_result_record_digest=result_record[1],
            )

    def new_owned_fill_close_proof(
        self,
        *,
        source_attempt_id: str,
        owned_position_proof: OwnedPositionProof,
        order_transport_result: TransportResult,
        trade_transport_result: TransportResult,
        account_transport_result: TransportResult,
        symbol_filter_transport_result: TransportResult,
        mark_price_transport_result: TransportResult,
    ) -> OwnedFillCloseProof:
        """Derive one close proof from five already-durable typed results."""

        from global_quant.gate1b.credential_transport import ResponseKind, TransportResult

        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            source = state._find_attempt(source_attempt_id)
            generation = state.active_generation
            if generation is None:
                raise ExecutionJournalError("OWNED_FILL_CLOSE_GENERATION_NOT_ACTIVE")
            request_sha_by_path: dict[str, str] = {}
            request_sequences: list[int] = []
            exact_by_kind: dict[ReadKind, ReservedRequest] = {}
            results_by_kind: dict[ReadKind, ReadResultProof] = {}

            def reference(
                transport_result: TransportResult,
                expected_kind: ReadKind,
                expected_outcome: ReadOutcome,
                expected_path: str,
                expected_transport_kind: ResponseKind,
            ) -> DurableReadResultReference:
                if type(transport_result) is not TransportResult:
                    raise ExecutionJournalError("TYPED_OWNED_FILL_TRANSPORT_REQUIRED")
                result_proof_sha256 = state.read_result_by_request.get(
                    transport_result.request_sha256
                )
                if result_proof_sha256 is None:
                    raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_RESULT_REQUIRED")
                result = state.read_results.get(result_proof_sha256)
                if result is None:
                    raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_RESULT_REQUIRED")
                reservation = state.read_reservations[result.request_sha256]
                exact = state.exact_request_reservations.get(result.request_sha256)
                if (
                    result.result_sha256 != transport_result.result_sha256
                    or result.request_sha256 != transport_result.request_sha256
                ):
                    raise ExecutionJournalError("OWNED_FILL_TRANSPORT_RESULT_MISMATCH")
                if (
                    exact is None
                    or result.generation != generation
                    or result.read_kind is not expected_kind
                    or result.outcome is not expected_outcome
                    or transport_result.logical_request_sha256 != reservation.logical_request_sha256
                    or transport_result.kind is not expected_transport_kind
                    or exact.reserved_request.logical_request_sha256
                    != transport_result.logical_request_sha256
                    or reservation.read_kind is not expected_kind
                    or reservation.purpose is not ReadPurpose.OWNED_FILL_CLOSE
                    or reservation.path != expected_path
                    or reservation.source_attempt_id != source.attempt_id
                    or reservation.client_id != source.client_id
                    or reservation.authorization_id != source.authorization_id
                    or reservation.intent_sha256 != source.intent_sha256
                    or reservation.generation != generation
                ):
                    raise ExecutionJournalError("OWNED_FILL_CLOSE_READ_BINDING_MISMATCH")
                request_sha_by_path[expected_path] = reservation.request_sha256
                request_sequences.append(reservation.monotonic_sequence)
                exact_by_kind[expected_kind] = exact.reserved_request
                results_by_kind[expected_kind] = result
                record_sequence, record_digest = state.read_result_records[result_proof_sha256]
                return DurableReadResultReference(
                    result_proof_sha256=result_proof_sha256,
                    record_sequence=record_sequence,
                    record_digest=record_digest,
                    request_sha256=transport_result.request_sha256,
                    logical_request_sha256=transport_result.logical_request_sha256,
                    transport_result_sha256=transport_result.result_sha256,
                    transport_kind=transport_result.kind.value,
                )

            order_result = reference(
                order_transport_result,
                ReadKind.ORDER,
                ReadOutcome.OWNED_ORDER_FILL_CONFIRMED,
                "/fapi/v1/order",
                ResponseKind.ORDER_OBSERVATION,
            )
            trade_result = reference(
                trade_transport_result,
                ReadKind.TRADE,
                ReadOutcome.OWNED_TRADE_FILL_CONFIRMED,
                "/fapi/v1/userTrades",
                ResponseKind.USER_TRADES,
            )
            account_result = reference(
                account_transport_result,
                ReadKind.ACCOUNT,
                ReadOutcome.OWNED_ACCOUNT_POSITION_CONFIRMED,
                "/fapi/v2/account",
                ResponseKind.ACCOUNT,
            )
            symbol_filter_result = reference(
                symbol_filter_transport_result,
                ReadKind.SYMBOL_FILTER,
                ReadOutcome.FILTER_SNAPSHOT_CONFIRMED,
                "/fapi/v1/exchangeInfo",
                ResponseKind.EXCHANGE_INFO,
            )
            mark_price_result = reference(
                mark_price_transport_result,
                ReadKind.MARK_PRICE,
                ReadOutcome.MARK_PRICE_CONFIRMED,
                "/fapi/v1/premiumIndex",
                ResponseKind.MARK_PRICE,
            )
            _validate_owned_fill_transport_semantics(
                source=source,
                owned_position_proof=owned_position_proof,
                order_transport_result=order_transport_result,
                trade_transport_result=trade_transport_result,
                account_transport_result=account_transport_result,
                symbol_filter_transport_result=symbol_filter_transport_result,
                mark_price_transport_result=mark_price_transport_result,
                request_sha_by_path=request_sha_by_path,
                request_sequences=tuple(request_sequences),
                exact_by_kind=exact_by_kind,
                results_by_kind=results_by_kind,
            )
            market_proof = owned_position_proof.market_close_proof
            return OwnedFillCloseProof.build(
                source_attempt_id=source.attempt_id,
                source_authorization_id=source.authorization_id,
                source_intent_sha256=source.intent_sha256,
                source_runtime_commit=source.runtime_commit,
                source_session_nonce=source.session_nonce,
                source_client_id=source.client_id,
                generation=generation,
                residual_quantity=format(owned_position_proof.residual_quantity, "f"),
                owned_executed_quantity=format(
                    owned_position_proof.owned_executed_quantity,
                    "f",
                ),
                open_remainder_quantity=format(
                    owned_position_proof.open_remainder_quantity,
                    "f",
                ),
                other_activity_absent=owned_position_proof.other_activity_absent,
                position_direction=owned_position_proof.position_direction,
                probe_terminal_status=owned_position_proof.probe_terminal_status,
                market_close_proof_sha256=market_proof.canonical_sha256,
                filter_snapshot_sha256=market_proof.filter_snapshot_sha256,
                filter_contract_sha256=market_proof.filter_contract_sha256,
                mark_price=format(market_proof.mark_price, "f"),
                mark_price_age_ms=format(market_proof.mark_price_age_ms, "f"),
                market_observed_elapsed_seconds=format(
                    market_proof.observed_elapsed_seconds,
                    "f",
                ),
                observed_elapsed_seconds=format(
                    owned_position_proof.observed_elapsed_seconds,
                    "f",
                ),
                observed_after_http_attempt=(owned_position_proof.observed_after_http_attempt),
                order_result=order_result,
                trade_result=trade_result,
                account_result=account_result,
                symbol_filter_result=symbol_filter_result,
                mark_price_result=mark_price_result,
            )

    def new_conditional_cleanup_cancel(
        self,
        *,
        source_attempt_id: str,
        observation_sha256: str,
        deadline_ns: int,
        reservation_sha256: str,
    ) -> MutationAttempt:
        """Build a cleanup CANCEL only from a durable typed fresh-OPEN record."""

        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            source = state._find_attempt(source_attempt_id)
            if source.kind not in {MutationKind.CREATE, MutationKind.CANCEL}:
                raise ExecutionJournalError("CONDITIONAL_CANCEL_NOT_ALLOWED")
            observation = state.observations.get(observation_sha256)
            if observation is None:
                raise ExecutionJournalError("FRESH_OPEN_OBSERVATION_REQUIRED")
            generation = state.active_generation
            if (
                generation is None
                or state.generation_capabilities.get(generation)
                is not GenerationCapability.RECOVERY
                or observation.generation != generation
                or observation.source_attempt_id != source.attempt_id
                or observation.source_authorization_id != source.authorization_id
                or observation.source_client_id != source.client_id
            ):
                raise ExecutionJournalError("RECOVERY_OBSERVATION_MISMATCH")
            return MutationAttempt.build(
                kind=MutationKind.CANCEL,
                generation=generation,
                retry_index=0,
                deadline_ns=deadline_ns,
                reservation_sha256=reservation_sha256,
                authorization_id=source.authorization_id,
                intent_sha256=source.intent_sha256,
                runtime_commit=source.runtime_commit,
                session_nonce=source.session_nonce,
                fresh_open_proof_sha256=observation.observation_sha256,
                recovery_of_attempt_id=source.attempt_id,
            )

    def resolve_after_reap(
        self,
        attempt_id: str,
        boundary_result: BoundaryResult,
        *,
        result_sha256: str | None = None,
    ) -> FrontierState:
        if type(boundary_result) is not BoundaryResult:
            raise ExecutionJournalError("INVALID_BOUNDARY_RESULT")

        resolved_state: FrontierState | None = None

        def event_for(state: _JournalState) -> _JournalEvent:
            nonlocal resolved_state
            attempt = state._find_attempt(attempt_id)
            if attempt.generation not in state.reaped_generations:
                raise ExecutionJournalError("OUTCOME_REQUIRES_REAP")
            if boundary_result is BoundaryResult.CONFIRMED:
                if not _is_sha256(result_sha256):
                    raise ExecutionJournalError("INVALID_RESULT_DIGEST")
                resolved_state = FrontierState.CONFIRMED
                return _AttemptConfirmed(
                    attempt_id=attempt_id,
                    generation=attempt.generation,
                    result_sha256=result_sha256,
                )
            if result_sha256 is not None:
                raise ExecutionJournalError("UNEXPECTED_RESULT_DIGEST")
            current = state.frontiers[attempt_id]
            if current is FrontierState.PREPARED:
                resolved_state = FrontierState.NOT_DISPATCHED
            elif current is FrontierState.GO_DURABLE:
                resolved_state = FrontierState.UNKNOWN
            else:
                raise ExecutionJournalError("OUTCOME_ALREADY_TERMINAL")
            return _AttemptResolved(
                attempt_id=attempt_id,
                generation=attempt.generation,
                state=resolved_state,
                boundary_result=boundary_result,
            )

        self._append_event(event_for)
        if resolved_state is None:
            raise ExecutionJournalError("INVALID_BOUNDARY_RESOLUTION")
        return resolved_state

    def frontier(self, attempt_id: str) -> FrontierState:
        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            state._find_attempt(attempt_id)
            return state.frontiers[attempt_id]

    def recovery_directive(self, attempt_id: str) -> RecoveryDirective:
        with self._locked_existing_fd() as fd:
            state = _state_from_records(self._read_validated_records(fd))
            attempt = state._find_attempt(attempt_id)
            frontier = state.frontiers[attempt_id]
            confirmed_create_after_reap = (
                attempt.kind is MutationKind.CREATE
                and frontier is FrontierState.CONFIRMED
                and attempt.generation in state.reaped_generations
            )
            if frontier is not FrontierState.UNKNOWN and not confirmed_create_after_reap:
                raise ExecutionJournalError("RECOVERY_REQUIRES_UNKNOWN")
            mode = {
                MutationKind.CREATE: RecoveryMode.QUERY_PROBE_CLIENT_ID,
                MutationKind.CANCEL: RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL,
                MutationKind.EMERGENCY_CLOSE: RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
            }[attempt.kind]
            return RecoveryDirective(
                source_attempt_id=attempt.attempt_id,
                source_generation=attempt.generation,
                kind=attempt.kind,
                mode=mode,
                query_client_id=attempt.reconciliation_key.client_id,
                source_authorization_id=attempt.authorization_id,
                source_intent_sha256=attempt.intent_sha256,
                source_runtime_commit=attempt.runtime_commit,
                source_session_nonce=attempt.session_nonce,
            )

    def _append_event(
        self,
        event_factory: Callable[[_JournalState], _JournalEvent],
    ) -> JournalRecord:
        with self._locked_existing_fd() as fd:
            records = self._read_validated_records(fd)
            state = _state_from_records(records)
            event = event_factory(state)
            previous_digest = records[-1].digest
            record, encoded = _build_record(
                sequence=len(records) + 1,
                previous_digest=previous_digest,
                event=event,
            )
            state.apply(record)
            _append_and_fsync(fd, encoded)
            self._write_anchor(record)
            return record

    def _ensure_created_or_valid(self) -> None:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_PARENT_UNAVAILABLE") from exc
        try:
            create_flags = (
                os.O_RDWR
                | os.O_APPEND
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                fd = os.open(self._path.name, create_flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                with self._locked_existing_fd() as existing_fd:
                    self._read_validated_records(existing_fd)
                    return
            except OSError as exc:
                raise ExecutionJournalError("JOURNAL_CREATE_FAILED") from exc
            try:
                os.fchmod(fd, 0o600)
                _validate_file_stat(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX)
                record, encoded = _build_record(
                    sequence=1,
                    previous_digest=ZERO_DIGEST,
                    event=_JournalCreated(),
                )
                _append_and_fsync(fd, encoded)
                _write_head(
                    parent_fd,
                    self.anchor_path.name,
                    _JournalHead(sequence=record.sequence, digest=record.digest),
                )
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def _read_validated_records(self, fd: int) -> tuple[JournalRecord, ...]:
        records = _read_records(fd)
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_PARENT_UNAVAILABLE") from exc
        try:
            _validate_or_repair_head(parent_fd, self.anchor_path.name, records)
        finally:
            os.close(parent_fd)
        return records

    def _write_anchor(self, record: JournalRecord) -> None:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_PARENT_UNAVAILABLE") from exc
        try:
            _write_head(
                parent_fd,
                self.anchor_path.name,
                _JournalHead(sequence=record.sequence, digest=record.digest),
            )
        finally:
            os.close(parent_fd)

    @contextmanager
    def _locked_existing_fd(self):
        try:
            entry_stat = os.stat(self._path, follow_symlinks=False)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_OPEN_FAILED") from exc
        _validate_file_stat(entry_stat)
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self._path, flags)
        except OSError as exc:
            raise ExecutionJournalError("JOURNAL_OPEN_FAILED") from exc
        try:
            opened_stat = os.fstat(fd)
            _validate_file_stat(opened_stat)
            if (entry_stat.st_dev, entry_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
                raise ExecutionJournalError("JOURNAL_PATH_RACE")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)
