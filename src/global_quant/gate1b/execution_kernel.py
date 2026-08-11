"""Credential-free dispatch frontier over the journal and process boundary.

Only fixed control records cross IPC.  Actual mutation I/O is injected inside
the child and cannot be called until an exact, deadline-bounded GO validates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from global_quant.gate1b.credential_transport import (
    CredentialTransportError,
    ResponseKind,
    TransportResult,
)
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ReconciliationKey,
    ReconciliationKeyKind,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    MutationLedger,
    MutationProtocolError,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
)
from global_quant.gate1b.process_boundary import (
    IPC_VERSION,
    AbsoluteDeadline,
    IPCMessage,
    PhaseDeadlinePermit,
    ProcessBoundaryError,
    ProcessIdentity,
    ProcessLifecycleJournal,
    ReapAttestation,
)

KERNEL_SCHEMA_VERSION = "gate1b.execution-kernel.v1"
_NS_PER_SECOND = 1_000_000_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DispatchKernelError(RuntimeError):
    """The credential-free dispatch contract could not be proven."""


class KernelFaultPoint(StrEnum):
    """Deterministic fault boundaries around each durable dispatch phase."""

    PREPARE = "PREPARE"
    PREPARED_FSYNC = "PREPARED_FSYNC"
    GO = "GO"
    GO_FSYNC = "GO_FSYNC"
    SEND = "SEND"
    SENT = "SENT"


class DispatchFailure(StrEnum):
    """Sanitized failure observations; none claims venue non-dispatch."""

    FAULT = "FAULT"
    TIMEOUT = "TIMEOUT"
    KILLED = "KILLED"
    EOF = "EOF"
    CORRUPT = "CORRUPT"
    TRUNCATED = "TRUNCATED"
    OVERSIZED = "OVERSIZED"
    VERSION = "VERSION"
    SEQUENCE = "SEQUENCE"
    DIGEST = "DIGEST"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    WRITE_LOSS = "WRITE_LOSS"
    PARSE = "PARSE"
    RESULT_DURABILITY = "RESULT_DURABILITY"


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DispatchKernelError("CONTROL_VALUE_NOT_CANONICAL") from exc


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    error: str,
) -> Mapping[str, object]:
    if type(value) is not dict or frozenset(value) != fields:
        raise DispatchKernelError(error)
    return value


def _enum(enum_type: type[StrEnum], value: object, error: str) -> StrEnum:
    if type(value) is not str:
        raise DispatchKernelError(error)
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DispatchKernelError(error) from exc


_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_id",
        "client_id",
        "deadline_ns",
        "fresh_open_proof_sha256",
        "generation",
        "intent_sha256",
        "kind",
        "permit_id",
        "reconciliation_key",
        "recovery_of_attempt_id",
        "reservation_sha256",
        "retry_index",
        "runtime_commit",
        "session_nonce",
    }
)


def _attempt_to_payload(attempt: MutationAttempt) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "client_id": attempt.client_id,
        "deadline_ns": attempt.deadline_ns,
        "fresh_open_proof_sha256": attempt.fresh_open_proof_sha256,
        "generation": attempt.generation,
        "intent_sha256": attempt.intent_sha256,
        "kind": attempt.kind.value,
        "permit_id": attempt.authorization_id,
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


def _attempt_from_payload(value: object) -> MutationAttempt:
    item = _exact_mapping(value, _ATTEMPT_FIELDS, "ATTEMPT_FIELDS_INVALID")
    key = _exact_mapping(
        item["reconciliation_key"],
        frozenset({"client_id", "kind"}),
        "RECONCILIATION_FIELDS_INVALID",
    )
    return MutationAttempt(
        attempt_id=item["attempt_id"],  # type: ignore[arg-type]
        kind=_enum(MutationKind, item["kind"], "ATTEMPT_KIND_INVALID"),  # type: ignore[arg-type]
        client_id=item["client_id"],  # type: ignore[arg-type]
        reconciliation_key=ReconciliationKey(
            kind=_enum(
                ReconciliationKeyKind,
                key["kind"],
                "RECONCILIATION_KIND_INVALID",
            ),  # type: ignore[arg-type]
            client_id=key["client_id"],  # type: ignore[arg-type]
        ),
        retry_index=item["retry_index"],  # type: ignore[arg-type]
        deadline_ns=item["deadline_ns"],  # type: ignore[arg-type]
        generation=item["generation"],  # type: ignore[arg-type]
        reservation_sha256=item["reservation_sha256"],  # type: ignore[arg-type]
        authorization_id=item["permit_id"],  # type: ignore[arg-type]
        intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
        runtime_commit=item["runtime_commit"],  # type: ignore[arg-type]
        session_nonce=item["session_nonce"],  # type: ignore[arg-type]
        fresh_open_proof_sha256=item["fresh_open_proof_sha256"],  # type: ignore[arg-type]
        recovery_of_attempt_id=item["recovery_of_attempt_id"],  # type: ignore[arg-type]
    )


_LEDGER_FIELDS = frozenset(
    {
        "total_http_requests",
        "create_requests",
        "cancel_requests",
        "emergency_close_requests",
        "read_retry_requests",
        "post_create_read_requests",
        "stage",
        "last_elapsed_seconds",
        "retryable_read_sha256",
    }
)
_RESERVED_REQUEST_FIELDS = frozenset(
    {
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
_RESERVATION_PROOF_FIELDS = frozenset(
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
_CREATE_PARAMETER_FIELDS = frozenset(
    {
        "symbol",
        "side",
        "type",
        "timeInForce",
        "quantity",
        "price",
        "positionSide",
        "reduceOnly",
        "newClientOrderId",
        "newOrderRespType",
        "recvWindow",
    }
)
_CANCEL_PARAMETER_FIELDS = frozenset({"symbol", "origClientOrderId", "recvWindow"})
_CLOSE_PARAMETER_FIELDS = frozenset(
    {
        "symbol",
        "side",
        "type",
        "quantity",
        "positionSide",
        "reduceOnly",
        "newClientOrderId",
        "newOrderRespType",
        "recvWindow",
    }
)


def _decimal_from_payload(value: object, error: str) -> Decimal:
    if type(value) is not str:
        raise DispatchKernelError(error)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise DispatchKernelError(error) from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise DispatchKernelError(error)
    return parsed


def _ledger_to_payload(ledger: MutationLedger) -> dict[str, object]:
    return {
        "total_http_requests": ledger.total_http_requests,
        "create_requests": ledger.create_requests,
        "cancel_requests": ledger.cancel_requests,
        "emergency_close_requests": ledger.emergency_close_requests,
        "read_retry_requests": ledger.read_retry_requests,
        "post_create_read_requests": ledger.post_create_read_requests,
        "stage": ledger.stage.value,
        "last_elapsed_seconds": format(ledger.last_elapsed_seconds, "f"),
        "retryable_read_sha256": ledger.retryable_read_sha256,
    }


def _ledger_from_payload(value: object) -> MutationLedger:
    item = _exact_mapping(value, _LEDGER_FIELDS, "REQUEST_LEDGER_FIELDS_INVALID")
    try:
        return MutationLedger(
            total_http_requests=item["total_http_requests"],  # type: ignore[arg-type]
            create_requests=item["create_requests"],  # type: ignore[arg-type]
            cancel_requests=item["cancel_requests"],  # type: ignore[arg-type]
            emergency_close_requests=item["emergency_close_requests"],  # type: ignore[arg-type]
            read_retry_requests=item["read_retry_requests"],  # type: ignore[arg-type]
            post_create_read_requests=item["post_create_read_requests"],  # type: ignore[arg-type]
            stage=_enum(
                RequestStage,
                item["stage"],
                "REQUEST_LEDGER_STAGE_INVALID",
            ),  # type: ignore[arg-type]
            last_elapsed_seconds=_decimal_from_payload(
                item["last_elapsed_seconds"],
                "REQUEST_LEDGER_ELAPSED_INVALID",
            ),
            retryable_read_sha256=item["retryable_read_sha256"],  # type: ignore[arg-type]
        )
    except MutationProtocolError as exc:
        raise DispatchKernelError("REQUEST_LEDGER_INVALID") from exc


def _reserved_request_to_payload(reserved: ReservedRequest) -> dict[str, object]:
    return {
        "request_sha256": reserved.request_sha256,
        "logical_request_sha256": reserved.logical_request_sha256,
        "ledger": _ledger_to_payload(reserved.ledger),
        "intent_sha256": reserved.intent_sha256,
        "origin": reserved.origin,
        "method": reserved.method,
        "path": reserved.path,
        "purpose": reserved.purpose.value,
        "parameters": [[key, value] for key, value in reserved.parameters],
        "elapsed_seconds": format(reserved.elapsed_seconds, "f"),
        "retry_index": reserved.retry_index,
    }


def _reserved_request_from_payload(value: object) -> ReservedRequest:
    item = _exact_mapping(
        value,
        _RESERVED_REQUEST_FIELDS,
        "RESERVED_REQUEST_FIELDS_INVALID",
    )
    raw_parameters = item["parameters"]
    if type(raw_parameters) is not list or any(
        type(pair) is not list
        or len(pair) != 2
        or type(pair[0]) is not str
        or type(pair[1]) is not str
        for pair in raw_parameters
    ):
        raise DispatchKernelError("RESERVED_REQUEST_PARAMETERS_INVALID")
    parameters = tuple((pair[0], pair[1]) for pair in raw_parameters)
    try:
        reserved = ReservedRequest(
            ledger=_ledger_from_payload(item["ledger"]),
            intent_sha256=item["intent_sha256"],  # type: ignore[arg-type]
            origin=item["origin"],  # type: ignore[arg-type]
            method=item["method"],  # type: ignore[arg-type]
            path=item["path"],  # type: ignore[arg-type]
            purpose=_enum(
                RequestPurpose,
                item["purpose"],
                "RESERVED_REQUEST_PURPOSE_INVALID",
            ),  # type: ignore[arg-type]
            parameters=parameters,
            elapsed_seconds=_decimal_from_payload(
                item["elapsed_seconds"],
                "RESERVED_REQUEST_ELAPSED_INVALID",
            ),
            retry_index=item["retry_index"],  # type: ignore[arg-type]
        )
    except MutationProtocolError as exc:
        raise DispatchKernelError("RESERVED_REQUEST_INVALID") from exc
    if (
        item["request_sha256"] != reserved.request_sha256
        or item["logical_request_sha256"] != reserved.logical_request_sha256
    ):
        raise DispatchKernelError("RESERVED_REQUEST_DIGEST_MISMATCH")
    _validate_unsigned_mutation_request(reserved)
    return reserved


def _reservation_proof_to_payload(
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


def _reservation_proof_from_payload(value: object) -> MutationReservationProof:
    item = _exact_mapping(
        value,
        _RESERVATION_PROOF_FIELDS,
        "RESERVATION_PROOF_FIELDS_INVALID",
    )
    try:
        return MutationReservationProof(
            request_sha256=item["request_sha256"],  # type: ignore[arg-type]
            proof_sha256=item["proof_sha256"],  # type: ignore[arg-type]
            logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
            kind=_enum(
                MutationKind,
                item["kind"],
                "RESERVATION_PROOF_KIND_INVALID",
            ),  # type: ignore[arg-type]
            purpose=_enum(
                MutationPurpose,
                item["purpose"],
                "RESERVATION_PROOF_PURPOSE_INVALID",
            ),  # type: ignore[arg-type]
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
    except ExecutionJournalError as exc:
        raise DispatchKernelError("RESERVATION_PROOF_INVALID") from exc


def _validate_unsigned_mutation_request(reserved: ReservedRequest) -> None:
    if (
        type(reserved) is not ReservedRequest
        or reserved.origin != DEMO_HTTP_ORIGIN
        or reserved.path != "/fapi/v1/order"
        or reserved.retry_index != 0
    ):
        raise DispatchKernelError("UNSIGNED_MUTATION_REQUEST_INVALID")
    parameters = dict(reserved.parameters)
    expected_fields = {
        RequestPurpose.CREATE: _CREATE_PARAMETER_FIELDS,
        RequestPurpose.CANCEL: _CANCEL_PARAMETER_FIELDS,
        RequestPurpose.EMERGENCY_CLOSE: _CLOSE_PARAMETER_FIELDS,
    }.get(reserved.purpose)
    expected_method = {
        RequestPurpose.CREATE: "POST",
        RequestPurpose.CANCEL: "DELETE",
        RequestPurpose.EMERGENCY_CLOSE: "POST",
    }.get(reserved.purpose)
    if expected_fields is None or frozenset(parameters) != expected_fields:
        raise DispatchKernelError("UNSIGNED_MUTATION_PARAMETERS_INVALID")
    if (
        reserved.method != expected_method
        or parameters["symbol"] != SYMBOL
        or parameters["recvWindow"] != str(RECEIVE_WINDOW_MS)
    ):
        raise DispatchKernelError("UNSIGNED_MUTATION_REQUEST_INVALID")
    if reserved.purpose is RequestPurpose.CANCEL:
        return
    if parameters["positionSide"] != "BOTH" or parameters["newOrderRespType"] != "ACK":
        raise DispatchKernelError("UNSIGNED_MUTATION_PARAMETERS_INVALID")
    quantity = _decimal_from_payload(
        parameters["quantity"],
        "UNSIGNED_MUTATION_PARAMETERS_INVALID",
    )
    if quantity <= 0:
        raise DispatchKernelError("UNSIGNED_MUTATION_PARAMETERS_INVALID")
    if reserved.purpose is RequestPurpose.CREATE:
        price = _decimal_from_payload(
            parameters["price"],
            "UNSIGNED_MUTATION_PARAMETERS_INVALID",
        )
        if (
            price <= 0
            or parameters["side"] != "BUY"
            or parameters["type"] != "LIMIT"
            or parameters["timeInForce"] != "GTX"
            or parameters["reduceOnly"] != "false"
        ):
            raise DispatchKernelError("UNSIGNED_MUTATION_PARAMETERS_INVALID")
    elif (
        parameters["side"] != "SELL"
        or parameters["type"] != "MARKET"
        or parameters["reduceOnly"] != "true"
    ):
        raise DispatchKernelError("UNSIGNED_MUTATION_PARAMETERS_INVALID")


def _validate_dispatch_binding(
    proof: MutationReservationProof,
    reserved: ReservedRequest,
    attempt: MutationAttempt,
) -> None:
    try:
        _validate_unsigned_mutation_request(reserved)
        proof.validate_dispatch_binding(reserved, attempt)
    except (ExecutionJournalError, MutationProtocolError) as exc:
        raise DispatchKernelError("GO_RESERVATION_BINDING_INVALID") from exc


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    """Controller-local proof that PREPARED returned after its journal fsync."""

    attempt: MutationAttempt
    lifecycle_deadline_ns: int
    local_deadline_ns: int
    prepared_record_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.attempt) is not MutationAttempt
            or not _positive_int(self.lifecycle_deadline_ns)
            or not _positive_int(self.local_deadline_ns)
            or self.local_deadline_ns > self.lifecycle_deadline_ns
            or not _is_sha256(self.prepared_record_digest)
        ):
            raise DispatchKernelError("PREPARED_DISPATCH_INVALID")


_GO_FIELDS = frozenset(
    {
        "schema_version",
        "attempt",
        "reserved_request",
        "reservation_proof",
        "generation",
        "lifecycle_deadline_ns",
        "local_deadline_ns",
        "go_deadline_ns",
        "phase_permit_sequence",
        "phase_permit_digest",
        "prepared_record_digest",
        "go_record_digest",
    }
)


@dataclass(frozen=True, slots=True)
class GoCommand:
    """Exact child authority carrying one unsigned request-ledger reservation."""

    attempt: MutationAttempt
    reserved_request: ReservedRequest
    reservation_proof: MutationReservationProof
    generation: int
    lifecycle_deadline_ns: int
    local_deadline_ns: int
    go_deadline_ns: int
    phase_permit_sequence: int
    phase_permit_digest: str
    prepared_record_digest: str
    go_record_digest: str

    def __post_init__(self) -> None:
        self._validate()

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": KERNEL_SCHEMA_VERSION,
            "attempt": _attempt_to_payload(self.attempt),
            "reserved_request": _reserved_request_to_payload(self.reserved_request),
            "reservation_proof": _reservation_proof_to_payload(self.reservation_proof),
            "generation": self.generation,
            "lifecycle_deadline_ns": self.lifecycle_deadline_ns,
            "local_deadline_ns": self.local_deadline_ns,
            "go_deadline_ns": self.go_deadline_ns,
            "phase_permit_sequence": self.phase_permit_sequence,
            "phase_permit_digest": self.phase_permit_digest,
            "prepared_record_digest": self.prepared_record_digest,
            "go_record_digest": self.go_record_digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GoCommand:
        item = _exact_mapping(payload, _GO_FIELDS, "GO_FIELDS_INVALID")
        if item["schema_version"] != KERNEL_SCHEMA_VERSION:
            raise DispatchKernelError("GO_VERSION_MISMATCH")
        command = cls(
            attempt=_attempt_from_payload(item["attempt"]),
            reserved_request=_reserved_request_from_payload(item["reserved_request"]),
            reservation_proof=_reservation_proof_from_payload(item["reservation_proof"]),
            generation=item["generation"],  # type: ignore[arg-type]
            lifecycle_deadline_ns=item["lifecycle_deadline_ns"],  # type: ignore[arg-type]
            local_deadline_ns=item["local_deadline_ns"],  # type: ignore[arg-type]
            go_deadline_ns=item["go_deadline_ns"],  # type: ignore[arg-type]
            phase_permit_sequence=item["phase_permit_sequence"],  # type: ignore[arg-type]
            phase_permit_digest=item["phase_permit_digest"],  # type: ignore[arg-type]
            prepared_record_digest=item["prepared_record_digest"],  # type: ignore[arg-type]
            go_record_digest=item["go_record_digest"],  # type: ignore[arg-type]
        )
        return command

    def _validate(self) -> None:
        if (
            type(self.attempt) is not MutationAttempt
            or type(self.reserved_request) is not ReservedRequest
            or type(self.reservation_proof) is not MutationReservationProof
            or not _positive_int(self.generation)
            or self.generation != self.attempt.generation
            or not _positive_int(self.lifecycle_deadline_ns)
            or not _positive_int(self.local_deadline_ns)
            or not _positive_int(self.go_deadline_ns)
            or self.local_deadline_ns > self.lifecycle_deadline_ns
            or self.go_deadline_ns > self.local_deadline_ns
            or self.go_deadline_ns > self.lifecycle_deadline_ns
            or self.go_deadline_ns > self.attempt.deadline_ns
            or type(self.phase_permit_sequence) is not int
            or self.phase_permit_sequence < 0
            or not _is_sha256(self.phase_permit_digest)
            or not _is_sha256(self.prepared_record_digest)
            or not _is_sha256(self.go_record_digest)
        ):
            raise DispatchKernelError("GO_COMMAND_INVALID")
        _validate_dispatch_binding(
            self.reservation_proof,
            self.reserved_request,
            self.attempt,
        )


@dataclass(frozen=True, slots=True)
class ConfirmedIO:
    """Exact sanitized mutation result returned by the child I/O callback."""

    transport_result: TransportResult

    def __post_init__(self) -> None:
        if type(self.transport_result) is not TransportResult:
            raise DispatchKernelError("IO_CONFIRMATION_INVALID")
        _validate_mutation_transport_result(self.transport_result)

    @property
    def evidence_sha256(self) -> str:
        return self.transport_result.result_sha256


_TRANSPORT_RESULT_FIELDS = frozenset(
    {
        "request_sha256",
        "logical_request_sha256",
        "result_sha256",
        "kind",
        "fields",
    }
)
_MUTATION_RESULT_FIELDS = frozenset({"clientOrderId", "orderIdSha256", "status"})
_MUTATION_RESULT_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
)


def _validate_mutation_transport_result(result: TransportResult) -> None:
    if type(result) is not TransportResult or result.kind is not ResponseKind.MUTATION_ACK:
        raise DispatchKernelError("MUTATION_TRANSPORT_RESULT_INVALID")
    fields = dict(result.fields)
    field_names = frozenset(fields)
    if (
        not frozenset({"clientOrderId", "status"}).issubset(field_names)
        or not field_names.issubset(_MUTATION_RESULT_FIELDS)
        or type(fields["clientOrderId"]) is not str
        or not fields["clientOrderId"]
        or type(fields["status"]) is not str
        or fields["status"] not in _MUTATION_RESULT_STATUSES
        or ("orderIdSha256" in fields and not _is_sha256(fields["orderIdSha256"]))
    ):
        raise DispatchKernelError("MUTATION_TRANSPORT_RESULT_INVALID")


def _transport_result_to_payload(result: TransportResult) -> dict[str, object]:
    _validate_mutation_transport_result(result)
    return {
        "request_sha256": result.request_sha256,
        "logical_request_sha256": result.logical_request_sha256,
        "result_sha256": result.result_sha256,
        "kind": result.kind.value,
        "fields": [[name, value] for name, value in result.fields],
    }


def _transport_result_from_payload(value: object) -> TransportResult:
    item = _exact_mapping(
        value,
        _TRANSPORT_RESULT_FIELDS,
        "MUTATION_TRANSPORT_RESULT_FIELDS_INVALID",
    )
    raw_fields = item["fields"]
    if type(raw_fields) is not list or any(
        type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str for pair in raw_fields
    ):
        raise DispatchKernelError("MUTATION_TRANSPORT_RESULT_INVALID")
    try:
        result = TransportResult(
            request_sha256=item["request_sha256"],  # type: ignore[arg-type]
            logical_request_sha256=item["logical_request_sha256"],  # type: ignore[arg-type]
            result_sha256=item["result_sha256"],  # type: ignore[arg-type]
            kind=_enum(
                ResponseKind,
                item["kind"],
                "MUTATION_TRANSPORT_RESULT_KIND_INVALID",
            ),  # type: ignore[arg-type]
            fields=tuple((pair[0], pair[1]) for pair in raw_fields),
        )
    except CredentialTransportError as exc:
        raise DispatchKernelError("MUTATION_TRANSPORT_RESULT_INVALID") from exc
    _validate_mutation_transport_result(result)
    return result


_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "attempt_id",
        "generation",
        "kind",
        "client_id",
        "evidence_sha256",
        "transport_result",
        "digest",
    }
)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Exact typed result whose canonical digest alone is journaled."""

    attempt_id: str
    generation: int
    kind: MutationKind
    client_id: str
    evidence_sha256: str
    transport_result: TransportResult
    digest: str

    @classmethod
    def build(
        cls,
        attempt: MutationAttempt,
        *,
        transport_result: TransportResult,
    ) -> DispatchResult:
        _validate_mutation_transport_result(transport_result)
        body = cls._body(
            attempt_id=attempt.attempt_id,
            generation=attempt.generation,
            kind=attempt.kind,
            client_id=attempt.client_id,
            evidence_sha256=transport_result.result_sha256,
            transport_result=transport_result,
        )
        return cls(
            attempt_id=attempt.attempt_id,
            generation=attempt.generation,
            kind=attempt.kind,
            client_id=attempt.client_id,
            evidence_sha256=transport_result.result_sha256,
            transport_result=transport_result,
            digest=hashlib.sha256(_canonical(body)).hexdigest(),
        )

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.attempt_id)
            or not _positive_int(self.generation)
            or type(self.kind) is not MutationKind
            or type(self.client_id) is not str
            or not self.client_id
            or not _is_sha256(self.evidence_sha256)
            or type(self.transport_result) is not TransportResult
            or self.evidence_sha256 != self.transport_result.result_sha256
            or not _is_sha256(self.digest)
        ):
            raise DispatchKernelError("RESULT_INVALID")
        expected = hashlib.sha256(
            _canonical(
                self._body(
                    attempt_id=self.attempt_id,
                    generation=self.generation,
                    kind=self.kind,
                    client_id=self.client_id,
                    evidence_sha256=self.evidence_sha256,
                    transport_result=self.transport_result,
                )
            )
        ).hexdigest()
        if not hmac.compare_digest(self.digest, expected):
            raise DispatchKernelError("RESULT_DIGEST_MISMATCH")

    @staticmethod
    def _body(
        *,
        attempt_id: str,
        generation: int,
        kind: MutationKind,
        client_id: str,
        evidence_sha256: str,
        transport_result: TransportResult,
    ) -> dict[str, object]:
        return {
            "schema_version": KERNEL_SCHEMA_VERSION,
            "status": "CONFIRMED",
            "attempt_id": attempt_id,
            "generation": generation,
            "kind": kind.value,
            "client_id": client_id,
            "evidence_sha256": evidence_sha256,
            "transport_result": _transport_result_to_payload(transport_result),
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._body(
                attempt_id=self.attempt_id,
                generation=self.generation,
                kind=self.kind,
                client_id=self.client_id,
                evidence_sha256=self.evidence_sha256,
                transport_result=self.transport_result,
            ),
            "digest": self.digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DispatchResult:
        item = _exact_mapping(payload, _RESULT_FIELDS, "RESULT_FIELDS_INVALID")
        if item["schema_version"] != KERNEL_SCHEMA_VERSION or item["status"] != "CONFIRMED":
            raise DispatchKernelError("RESULT_VERSION_OR_STATUS_INVALID")
        return cls(
            attempt_id=item["attempt_id"],  # type: ignore[arg-type]
            generation=item["generation"],  # type: ignore[arg-type]
            kind=_enum(MutationKind, item["kind"], "RESULT_KIND_INVALID"),  # type: ignore[arg-type]
            client_id=item["client_id"],  # type: ignore[arg-type]
            evidence_sha256=item["evidence_sha256"],  # type: ignore[arg-type]
            transport_result=_transport_result_from_payload(item["transport_result"]),
            digest=item["digest"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ExactReap:
    """Exact generation paired with the process layer's real reap attestation."""

    generation: int
    attestation: ReapAttestation

    @classmethod
    def from_attestation(
        cls,
        *,
        generation: int,
        attestation: ReapAttestation,
    ) -> ExactReap:
        return cls(generation=generation, attestation=attestation)

    def __post_init__(self) -> None:
        if (
            not _positive_int(self.generation)
            or type(self.attestation) is not ReapAttestation
            or self.attestation.local_process_quiesced is not True
            or self.attestation.venue_mutation_absent_proven is not False
        ):
            raise DispatchKernelError("EXACT_REAP_INVALID")


class _ControlChannel(Protocol):
    def send(self, kind: str, payload: Mapping[str, Any]) -> None: ...

    def receive(self) -> IPCMessage: ...


def _seconds_to_ns(value: float) -> int:
    if not math.isfinite(value) or value <= 0:
        raise DispatchKernelError("DEADLINE_INVALID")
    return int(value * _NS_PER_SECOND)


def _process_identity_sha256(identity: ProcessIdentity) -> str:
    if type(identity) is not ProcessIdentity:
        raise DispatchKernelError("PROCESS_IDENTITY_REQUIRED")
    payload = identity.to_payload()
    if (
        any(type(payload[name]) is not int for name in ("pid", "ppid", "pgid", "sid"))
        or any(payload[name] <= 0 for name in ("pid", "pgid", "sid"))
        or payload["ppid"] < 0
        or type(payload["start_token"]) is not str
        or not payload["start_token"]
    ):
        raise DispatchKernelError("PROCESS_IDENTITY_INVALID")
    return hashlib.sha256(_canonical(payload)).hexdigest()


class DispatchKernel:
    """Credential-free controller for journal-before-GO mutation dispatch."""

    def __init__(
        self,
        *,
        journal: ExecutionJournal,
        process_journal_path: str | Path,
        channel: _ControlChannel,
        lifecycle_deadline: AbsoluteDeadline,
        fault_hook: Callable[[KernelFaultPoint], None] | None = None,
    ) -> None:
        if (
            type(journal) is not ExecutionJournal
            or type(lifecycle_deadline) is not AbsoluteDeadline
        ):
            raise DispatchKernelError("KERNEL_BINDING_INVALID")
        self._journal = journal
        self._channel = channel
        self._lifecycle_deadline = lifecycle_deadline
        self._fault_hook = fault_hook or (lambda _point: None)
        try:
            self._process_journal = ProcessLifecycleJournal.restore(process_journal_path)
        except (OSError, ProcessBoundaryError, ValueError, TypeError) as exc:
            raise DispatchKernelError("PROCESS_JOURNAL_REQUIRED") from exc
        if (
            self._process_journal.execution_journal_path.resolve() != self._journal.path.resolve()
            or self._process_journal.lifecycle_deadline != self._lifecycle_deadline.at
        ):
            raise DispatchKernelError("PROCESS_JOURNAL_BINDING_MISMATCH")
        self._prepared: dict[str, PreparedDispatch] = {}
        self._commands: dict[str, GoCommand] = {}
        self._reservation_bindings: dict[
            str,
            tuple[MutationReservationProof, ReservedRequest],
        ] = {}
        self._phase_permits: dict[str, PhaseDeadlinePermit] = {}
        self._generation_identity_sha256: dict[int, str] = {}
        self._generation_admission_records: dict[int, tuple[int, str]] = {}
        self._go_record_digests: dict[str, str] = {}
        self._restore_execution_state()

    def prepare(
        self,
        attempt: MutationAttempt,
        *,
        reserved_request: ReservedRequest,
        phase_permit: PhaseDeadlinePermit,
    ) -> PreparedDispatch:
        if (
            type(attempt) is not MutationAttempt
            or type(reserved_request) is not ReservedRequest
            or type(phase_permit) is not PhaseDeadlinePermit
        ):
            raise DispatchKernelError("ATTEMPT_INVALID")
        self._validate_recovery_lineage(attempt)
        exact_reserved_request = self._find_exact_reserved_request(attempt.reservation_sha256)
        if exact_reserved_request != reserved_request:
            raise DispatchKernelError("DURABLE_EXACT_REQUEST_MISMATCH")
        reservation_proof = self._find_reservation_proof(attempt.reservation_sha256)
        _validate_dispatch_binding(reservation_proof, reserved_request, attempt)
        self._fault_hook(KernelFaultPoint.PREPARE)
        now = self._lifecycle_deadline.clock()
        lifecycle_ns = _seconds_to_ns(self._lifecycle_deadline.at)
        if (
            phase_permit.generation != attempt.generation
            or _seconds_to_ns(phase_permit.lifecycle_deadline) != lifecycle_ns
        ):
            raise DispatchKernelError("PHASE_PERMIT_BINDING_MISMATCH")
        local_ns = min(_seconds_to_ns(phase_permit.absolute_deadline), lifecycle_ns)
        if min(local_ns, lifecycle_ns, attempt.deadline_ns) <= _seconds_to_ns(now):
            raise DispatchKernelError("GO_DEADLINE_EXPIRED")
        record = self._journal.prepare_attempt(attempt)
        prepared = PreparedDispatch(
            attempt=attempt,
            lifecycle_deadline_ns=lifecycle_ns,
            local_deadline_ns=local_ns,
            prepared_record_digest=record.digest,
        )
        self._prepared[attempt.attempt_id] = prepared
        self._reservation_bindings[attempt.attempt_id] = (
            reservation_proof,
            reserved_request,
        )
        self._phase_permits[attempt.attempt_id] = phase_permit
        self._fault_hook(KernelFaultPoint.PREPARED_FSYNC)
        return prepared

    def authorize_go(self, prepared: PreparedDispatch) -> GoCommand:
        if (
            type(prepared) is not PreparedDispatch
            or self._prepared.get(prepared.attempt.attempt_id) != prepared
            or prepared.attempt.attempt_id not in self._reservation_bindings
            or prepared.attempt.attempt_id not in self._phase_permits
        ):
            raise DispatchKernelError("PREPARED_PROOF_REQUIRED")
        reservation_proof, reserved_request = self._reservation_bindings[
            prepared.attempt.attempt_id
        ]
        phase_permit = self._phase_permits[prepared.attempt.attempt_id]
        _validate_dispatch_binding(reservation_proof, reserved_request, prepared.attempt)
        self._fault_hook(KernelFaultPoint.GO)
        record = self._journal.record_go(prepared.attempt.attempt_id)
        go_deadline_ns = min(
            prepared.local_deadline_ns,
            prepared.lifecycle_deadline_ns,
            prepared.attempt.deadline_ns,
        )
        if go_deadline_ns <= _seconds_to_ns(self._lifecycle_deadline.clock()):
            raise DispatchKernelError("GO_DEADLINE_EXPIRED")
        command = GoCommand(
            attempt=prepared.attempt,
            reserved_request=reserved_request,
            reservation_proof=reservation_proof,
            generation=prepared.attempt.generation,
            lifecycle_deadline_ns=prepared.lifecycle_deadline_ns,
            local_deadline_ns=prepared.local_deadline_ns,
            go_deadline_ns=go_deadline_ns,
            phase_permit_sequence=phase_permit.sequence,
            phase_permit_digest=phase_permit.digest,
            prepared_record_digest=prepared.prepared_record_digest,
            go_record_digest=record.digest,
        )
        self._commands[command.attempt.attempt_id] = command
        self._go_record_digests[command.attempt.attempt_id] = record.digest
        self._fault_hook(KernelFaultPoint.GO_FSYNC)
        return command

    def send_go(self, command: GoCommand) -> None:
        if (
            type(command) is not GoCommand
            or self._commands.get(command.attempt.attempt_id) != command
            or self._go_record_digests.get(command.attempt.attempt_id) != command.go_record_digest
            or self._journal.frontier(command.attempt.attempt_id) is not FrontierState.GO_DURABLE
        ):
            raise DispatchKernelError("DURABLE_GO_REQUIRED")
        _validate_dispatch_binding(
            command.reservation_proof,
            command.reserved_request,
            command.attempt,
        )
        self._fault_hook(KernelFaultPoint.SEND)
        self._channel.send("GO", command.to_payload())
        self._fault_hook(KernelFaultPoint.SENT)

    def dispatch(
        self,
        attempt: MutationAttempt,
        *,
        reserved_request: ReservedRequest,
        phase_permit: PhaseDeadlinePermit,
    ) -> GoCommand:
        prepared = self.prepare(
            attempt,
            reserved_request=reserved_request,
            phase_permit=phase_permit,
        )
        command = self.authorize_go(prepared)
        self.send_go(command)
        return command

    def confirm_result(self, command: GoCommand, message: IPCMessage) -> FrontierState:
        if (
            type(command) is not GoCommand
            or self._commands.get(command.attempt.attempt_id) != command
        ):
            raise DispatchKernelError("EXACT_GO_COMMAND_REQUIRED")
        if (
            type(message) is not IPCMessage
            or message.version != IPC_VERSION
            or message.kind != "RESULT"
        ):
            raise DispatchKernelError("RESULT_MESSAGE_INVALID")
        result = DispatchResult.from_payload(message.payload)
        attempt = command.attempt
        if (
            result.attempt_id != attempt.attempt_id
            or result.generation != attempt.generation
            or result.kind is not attempt.kind
            or result.client_id != attempt.client_id
            or result.transport_result.request_sha256 != command.reserved_request.request_sha256
            or result.transport_result.logical_request_sha256
            != command.reserved_request.logical_request_sha256
            or result.transport_result.field("clientOrderId") != attempt.client_id
        ):
            raise DispatchKernelError("RESULT_BINDING_MISMATCH")
        self._journal.record_confirmed(attempt.attempt_id, result.digest)
        return self._journal.frontier(attempt.attempt_id)

    def settle_failure(
        self,
        attempt: MutationAttempt,
        *,
        failure: DispatchFailure,
        reap_attestation: ReapAttestation,
    ) -> FrontierState:
        if (
            type(attempt) is not MutationAttempt
            or self._prepared.get(attempt.attempt_id) is None
            or self._prepared[attempt.attempt_id].attempt != attempt
        ):
            raise DispatchKernelError("CALLBACK_GATE_NOT_PROVEN")
        if type(failure) is not DispatchFailure:
            raise DispatchKernelError("FAILURE_KIND_INVALID")
        if type(reap_attestation) is not ReapAttestation:
            raise DispatchKernelError("REAL_REAP_ATTESTATION_REQUIRED")
        if reap_attestation.generation != attempt.generation:
            raise DispatchKernelError("EXACT_REAP_GENERATION_MISMATCH")
        identity_sha256 = _process_identity_sha256(reap_attestation.identity)
        if self._generation_identity_sha256.get(attempt.generation) != identity_sha256:
            raise DispatchKernelError("EXACT_REAP_IDENTITY_MISMATCH")
        if attempt.generation not in self._generation_admission_records:
            raise DispatchKernelError("EXECUTION_ADMISSION_ANCHOR_REQUIRED")
        try:
            process_journal = ProcessLifecycleJournal.restore(self._process_journal.path)
            if process_journal.execution_journal_path.resolve() != self._journal.path.resolve():
                raise DispatchKernelError("PROCESS_JOURNAL_BINDING_MISMATCH")
            process_journal.verify_reap_attestation(reap_attestation)
        except DispatchKernelError:
            raise
        except (OSError, ProcessBoundaryError, ValueError, TypeError) as exc:
            raise DispatchKernelError("PROCESS_REAP_ATTESTATION_NOT_DURABLE") from exc
        self._process_journal = process_journal
        boundary = self.boundary_result_for_failure(failure)
        return self._journal.resolve_after_reap(attempt.attempt_id, boundary)

    @staticmethod
    def boundary_result_for_failure(failure: DispatchFailure) -> BoundaryResult:
        if type(failure) is not DispatchFailure:
            raise DispatchKernelError("FAILURE_KIND_INVALID")
        return {
            DispatchFailure.FAULT: BoundaryResult.CORRUPT,
            DispatchFailure.TIMEOUT: BoundaryResult.TIMEOUT,
            DispatchFailure.KILLED: BoundaryResult.KILLED,
            DispatchFailure.EOF: BoundaryResult.EOF,
            DispatchFailure.CORRUPT: BoundaryResult.CORRUPT,
            DispatchFailure.TRUNCATED: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.OVERSIZED: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.VERSION: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.SEQUENCE: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.DIGEST: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.PARTIAL_RESULT: BoundaryResult.PARTIAL_WRITE,
            DispatchFailure.WRITE_LOSS: BoundaryResult.RESPONSE_LOSS,
            DispatchFailure.PARSE: BoundaryResult.DECODE_FAILURE,
            DispatchFailure.RESULT_DURABILITY: (BoundaryResult.RESULT_DURABILITY_FAILURE),
        }[failure]

    def _validate_recovery_lineage(self, attempt: MutationAttempt) -> None:
        source_id = attempt.recovery_of_attempt_id
        if source_id is None:
            return
        source = self._find_attempt(source_id)
        if (
            attempt.kind is not MutationKind.CANCEL
            or attempt.authorization_id != source.authorization_id
            or attempt.intent_sha256 != source.intent_sha256
            or attempt.runtime_commit != source.runtime_commit
            or attempt.session_nonce != source.session_nonce
            or attempt.client_id != source.client_id
        ):
            raise DispatchKernelError("RECOVERY_LINEAGE_MISMATCH")

    def _restore_execution_state(self) -> None:
        """Rebuild only facts reproducible from the validated durable chain."""

        lifecycle_ns = _seconds_to_ns(self._lifecycle_deadline.at)
        records = self._journal.records()
        exact_requests: dict[str, ReservedRequest] = {}
        reservation_proofs: dict[str, MutationReservationProof] = {}
        for record in records:
            event = record.event
            reserved_request = getattr(event, "reserved_request", None)
            if type(reserved_request) is ReservedRequest:
                exact_requests[reserved_request.request_sha256] = reserved_request
            proof = getattr(event, "proof", None)
            if type(proof) is MutationReservationProof:
                reservation_proofs[proof.request_sha256] = proof
        for record in records:
            event = record.event
            event_name = type(event).__name__
            if event_name == "_GenerationAdmitted":
                self._generation_identity_sha256[event.generation] = event.process_identity_sha256
                self._generation_admission_records[event.generation] = (
                    record.sequence,
                    record.digest,
                )
            elif event_name == "_AttemptPrepared":
                attempt = event.attempt
                reserved_request = exact_requests.get(attempt.reservation_sha256)
                reservation_proof = reservation_proofs.get(attempt.reservation_sha256)
                if reserved_request is None or reservation_proof is None:
                    raise DispatchKernelError("DURABLE_EXACT_REQUEST_REQUIRED")
                _validate_dispatch_binding(reservation_proof, reserved_request, attempt)
                self._prepared[attempt.attempt_id] = PreparedDispatch(
                    attempt=attempt,
                    lifecycle_deadline_ns=lifecycle_ns,
                    local_deadline_ns=min(lifecycle_ns, attempt.deadline_ns),
                    prepared_record_digest=record.digest,
                )
                self._reservation_bindings[attempt.attempt_id] = (
                    reservation_proof,
                    reserved_request,
                )
            elif event_name == "_GoDurable":
                self._go_record_digests[event.attempt_id] = record.digest

    def replayed_exact_request(
        self,
        attempt_id: str,
    ) -> tuple[ReservedRequest, MutationReservationProof]:
        """Return only the exact request/proof pair restored from durable replay."""

        if not _is_sha256(attempt_id):
            raise DispatchKernelError("ATTEMPT_ID_INVALID")
        binding = self._reservation_bindings.get(attempt_id)
        if binding is None:
            raise DispatchKernelError("DURABLE_EXACT_REQUEST_REQUIRED")
        return binding[1], binding[0]

    def _find_exact_reserved_request(self, request_sha256: str) -> ReservedRequest:
        for record in self._journal.records():
            reserved = getattr(record.event, "reserved_request", None)
            if type(reserved) is ReservedRequest and hmac.compare_digest(
                reserved.request_sha256,
                request_sha256,
            ):
                return reserved
        raise DispatchKernelError("DURABLE_EXACT_REQUEST_REQUIRED")

    def _find_reservation_proof(self, request_sha256: str) -> MutationReservationProof:
        for record in self._journal.records():
            proof = getattr(record.event, "proof", None)
            if type(proof) is MutationReservationProof and proof.request_sha256 == request_sha256:
                return proof
        raise DispatchKernelError("DURABLE_MUTATION_RESERVATION_REQUIRED")

    def _known_attempts(self) -> tuple[MutationAttempt, ...]:
        attempts: dict[str, MutationAttempt] = {}
        for record in self._journal.records():
            attempt = getattr(record.event, "attempt", None)
            if type(attempt) is MutationAttempt:
                attempts[attempt.attempt_id] = attempt
        return tuple(attempts.values())

    def _find_attempt(self, attempt_id: str) -> MutationAttempt:
        for attempt in self._known_attempts():
            if hmac.compare_digest(attempt.attempt_id, attempt_id):
                return attempt
        raise DispatchKernelError("SOURCE_ATTEMPT_NOT_FOUND")


class ChildDispatcher:
    """Child-only gate which calls mutation I/O only after an exact valid GO."""

    def __init__(
        self,
        *,
        channel: _ControlChannel,
        generation: int,
        lifecycle_deadline: AbsoluteDeadline,
        hard_deadline: Any,
    ) -> None:
        if not _positive_int(generation) or type(lifecycle_deadline) is not AbsoluteDeadline:
            raise DispatchKernelError("CHILD_DISPATCH_BINDING_INVALID")
        self._channel = channel
        self._generation = generation
        self._lifecycle_deadline = lifecycle_deadline
        self._hard_deadline = hard_deadline

    def dispatch_once(
        self,
        io_callback: Callable[[ReservedRequest], ConfirmedIO],
        *,
        phase_permit: PhaseDeadlinePermit,
    ) -> DispatchResult:
        if type(phase_permit) is not PhaseDeadlinePermit:
            raise DispatchKernelError("PHASE_PERMIT_REQUIRED")
        message = self._channel.receive()
        if message.version != IPC_VERSION or message.kind != "GO":
            raise DispatchKernelError("EXACT_GO_REQUIRED")
        command = GoCommand.from_payload(message.payload)
        lifecycle_ns = _seconds_to_ns(self._lifecycle_deadline.at)
        now_ns = _seconds_to_ns(self._lifecycle_deadline.clock())
        if (
            command.generation != self._generation
            or command.lifecycle_deadline_ns != lifecycle_ns
            or command.go_deadline_ns <= now_ns
            or phase_permit.generation != self._generation
            or _seconds_to_ns(phase_permit.lifecycle_deadline) != lifecycle_ns
            or command.local_deadline_ns != _seconds_to_ns(phase_permit.absolute_deadline)
            or command.phase_permit_sequence != phase_permit.sequence
            or not hmac.compare_digest(command.phase_permit_digest, phase_permit.digest)
            or command.go_deadline_ns != _seconds_to_ns(phase_permit.absolute_deadline)
            or command.go_deadline_ns
            != min(
                command.local_deadline_ns,
                command.lifecycle_deadline_ns,
                command.attempt.deadline_ns,
            )
        ):
            raise DispatchKernelError("EXACT_GO_REQUIRED")
        _validate_dispatch_binding(
            command.reservation_proof,
            command.reserved_request,
            command.attempt,
        )
        self._hard_deadline.assert_intact()
        confirmation = io_callback(command.reserved_request)
        if type(confirmation) is not ConfirmedIO:
            raise DispatchKernelError("TYPED_IO_CONFIRMATION_REQUIRED")
        result = DispatchResult.build(
            command.attempt,
            transport_result=confirmation.transport_result,
        )
        self._channel.send("RESULT", result.to_payload())
        return result
