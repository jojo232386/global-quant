"""Sanitized command router for one credential-bearing process generation.

The channel carries only exact, versioned control records and unsigned request
reservations.  Credentials and signed request material remain encapsulated by
``ProcessBoundCredentialTransport`` inside the child.  Every network action is
bound to the already accepted supervisor phase permit; this module never mints
or extends a deadline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from global_quant.gate1b.credential_transport import (
    CredentialTransportError,
    ProcessBoundCredentialTransport,
    ResponseKind,
    TransportResult,
)
from global_quant.gate1b.durable_intent import (
    DurableIntentError,
    PersistedIntent,
    load_persisted_intent,
)
from global_quant.gate1b.execution_journal import (
    ExecutionJournal,
    ExecutionJournalError,
    GenerationCapability,
    IntentBoundRecoveryAuthority,
    IntentChainBinding,
    MutationKind,
    PreIntentReadReservation,
    ReadKind,
    ReadPurpose,
    ReadReservationProof,
    RecoverySessionAuthority,
    SessionAuthority,
    project_read_failure_kind,
)
from global_quant.gate1b.execution_journal import (
    ReadFailureKind as JournalReadFailureKind,
)
from global_quant.gate1b.execution_kernel import (
    ChildDispatcher,
    ConfirmedIO,
    DispatchKernelError,
    GoCommand,
)
from global_quant.gate1b.mutation_protocol import (
    MutationLedger,
    MutationProtocolError,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
)
from global_quant.gate1b.process_boundary import (
    IPC_VERSION,
    ChildBootstrap,
    CredentialBoundaryError,
    IPCMessage,
    PhaseDeadlinePermit,
    ProcessBoundaryError,
)

SESSION_SCHEMA_VERSION = "gate1b.credential-execution-session.v1"
_NS_PER_SECOND = 1_000_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORIZATION_ID = re.compile(r"g1b16-[0-9a-f]{16}\Z")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SESSION_NONCE = re.compile(r"[0-9a-f]{16}\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class CredentialExecutionSessionError(RuntimeError):
    """A sanitized fail-closed child session error."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReadBindingKind(StrEnum):
    PRE_INTENT = "PRE_INTENT"
    INTENT_BOUND = "INTENT_BOUND"


ReadFailureKind = JournalReadFailureKind


class SessionFinalState(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CredentialExecutionSessionError("SESSION_VALUE_NOT_CANONICAL") from exc


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    reason: str,
) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        raise CredentialExecutionSessionError(reason)
    return value


def _parse_decimal(value: object, reason: str) -> Decimal:
    if type(value) is not str:
        raise CredentialExecutionSessionError(reason)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CredentialExecutionSessionError(reason) from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise CredentialExecutionSessionError(reason)
    return parsed


def _seconds_to_ns(value: float) -> int:
    if type(value) not in {int, float} or not math.isfinite(float(value)) or value <= 0:
        raise CredentialExecutionSessionError("SESSION_DEADLINE_INVALID")
    return int(value * _NS_PER_SECOND)


_PRIMARY_AUTHORITY_FIELDS = frozenset(
    {
        "authority_kind",
        "authority_sha256",
        "authorization_id",
        "client_id",
        "generation",
        "runtime_commit",
        "session_nonce",
    }
)
_ATTEMPT_RECOVERY_AUTHORITY_FIELDS = frozenset(
    {
        "authority_kind",
        "authority_sha256",
        "primary_authority_sha256",
        "source_attempt_id",
        "source_generation",
        "source_kind",
        "authorization_id",
        "source_intent_sha256",
        "source_runtime_commit",
        "source_session_nonce",
        "source_client_id",
        "generation",
    }
)
_INTENT_RECOVERY_AUTHORITY_FIELDS = frozenset(
    {
        "authority_kind",
        "authority_sha256",
        "primary_authority_sha256",
        "intent_binding_sha256",
        "source_generation",
        "authorization_id",
        "source_intent_sha256",
        "source_runtime_commit",
        "source_session_nonce",
        "query_client_id",
        "generation",
        "abandoned_create_request_sha256",
    }
)


def _authority_to_payload(
    authority: (SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority),
) -> dict[str, object]:
    if type(authority) is SessionAuthority:
        return {
            "authority_kind": "PRIMARY",
            "authority_sha256": authority.authority_sha256,
            "authorization_id": authority.authorization_id,
            "client_id": authority.client_id,
            "generation": authority.generation,
            "runtime_commit": authority.runtime_commit,
            "session_nonce": authority.session_nonce,
        }
    if type(authority) is RecoverySessionAuthority:
        return {
            "authority_kind": "ATTEMPT_RECOVERY",
            "authority_sha256": authority.authority_sha256,
            "primary_authority_sha256": authority.primary_authority_sha256,
            "source_attempt_id": authority.source_attempt_id,
            "source_generation": authority.source_generation,
            "source_kind": authority.source_kind.value,
            "authorization_id": authority.source_authorization_id,
            "source_intent_sha256": authority.source_intent_sha256,
            "source_runtime_commit": authority.source_runtime_commit,
            "source_session_nonce": authority.source_session_nonce,
            "source_client_id": authority.source_client_id,
            "generation": authority.generation,
        }
    if type(authority) is IntentBoundRecoveryAuthority:
        return {
            "authority_kind": "INTENT_BOUND_RECOVERY",
            "authority_sha256": authority.authority_sha256,
            "primary_authority_sha256": authority.primary_authority_sha256,
            "intent_binding_sha256": authority.intent_binding_sha256,
            "source_generation": authority.source_generation,
            "authorization_id": authority.source_authorization_id,
            "source_intent_sha256": authority.source_intent_sha256,
            "source_runtime_commit": authority.source_runtime_commit,
            "source_session_nonce": authority.source_session_nonce,
            "query_client_id": authority.query_client_id,
            "generation": authority.generation,
            "abandoned_create_request_sha256": (authority.abandoned_create_request_sha256),
        }
    raise CredentialExecutionSessionError("SESSION_AUTHORITY_INVALID")


def _authority_from_payload(
    value: object,
) -> SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority:
    if type(value) is not dict:
        raise CredentialExecutionSessionError("SESSION_AUTHORITY_FIELDS_INVALID")
    authority_kind = value.get("authority_kind")
    try:
        if authority_kind == "PRIMARY":
            item = _exact_mapping(
                value,
                _PRIMARY_AUTHORITY_FIELDS,
                "SESSION_AUTHORITY_FIELDS_INVALID",
            )
            return SessionAuthority(
                authority_sha256=item["authority_sha256"],
                authorization_id=item["authorization_id"],
                runtime_commit=item["runtime_commit"],
                session_nonce=item["session_nonce"],
                generation=item["generation"],
                client_id=item["client_id"],
            )
        if authority_kind == "ATTEMPT_RECOVERY":
            item = _exact_mapping(
                value,
                _ATTEMPT_RECOVERY_AUTHORITY_FIELDS,
                "SESSION_AUTHORITY_FIELDS_INVALID",
            )
            source_kind = item["source_kind"]
            if type(source_kind) is not str:
                raise TypeError
            return RecoverySessionAuthority(
                authority_sha256=item["authority_sha256"],
                primary_authority_sha256=item["primary_authority_sha256"],
                source_attempt_id=item["source_attempt_id"],
                source_generation=item["source_generation"],
                source_kind=MutationKind(source_kind),
                source_authorization_id=item["authorization_id"],
                source_intent_sha256=item["source_intent_sha256"],
                source_runtime_commit=item["source_runtime_commit"],
                source_session_nonce=item["source_session_nonce"],
                source_client_id=item["source_client_id"],
                generation=item["generation"],
            )
        if authority_kind == "INTENT_BOUND_RECOVERY":
            item = _exact_mapping(
                value,
                _INTENT_RECOVERY_AUTHORITY_FIELDS,
                "SESSION_AUTHORITY_FIELDS_INVALID",
            )
            return IntentBoundRecoveryAuthority(
                authority_sha256=item["authority_sha256"],
                primary_authority_sha256=item["primary_authority_sha256"],
                intent_binding_sha256=item["intent_binding_sha256"],
                source_generation=item["source_generation"],
                source_authorization_id=item["authorization_id"],
                source_intent_sha256=item["source_intent_sha256"],
                source_runtime_commit=item["source_runtime_commit"],
                source_session_nonce=item["source_session_nonce"],
                query_client_id=item["query_client_id"],
                generation=item["generation"],
                abandoned_create_request_sha256=(item["abandoned_create_request_sha256"]),
            )
        raise TypeError
    except (TypeError, ValueError) as exc:
        raise CredentialExecutionSessionError("SESSION_AUTHORITY_INVALID") from exc


_SESSION_INIT_FIELDS = frozenset(
    {
        "schema_version",
        "capability",
        "authority",
        "execution_journal_path",
        "recovery_reference",
    }
)


@dataclass(frozen=True, slots=True)
class SessionInitCommand:
    authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority
    capability: GenerationCapability
    execution_journal_path: Path
    recovery_reference: IntentBindingReference | None = None

    def __post_init__(self) -> None:
        if (
            type(self.capability) is not GenerationCapability
            or not isinstance(self.execution_journal_path, Path)
            or not self.execution_journal_path.is_absolute()
            or str(self.execution_journal_path.absolute()) != str(self.execution_journal_path)
            or "\0" in str(self.execution_journal_path)
            or len(str(self.execution_journal_path)) > 4096
            or (
                self.capability is GenerationCapability.PRIMARY
                and (
                    type(self.authority) is not SessionAuthority
                    or self.recovery_reference is not None
                )
            )
            or (
                self.capability is GenerationCapability.RECOVERY
                and (
                    type(self.authority)
                    not in {RecoverySessionAuthority, IntentBoundRecoveryAuthority}
                    or type(self.recovery_reference) is not IntentBindingReference
                    or self.recovery_reference.generation != self.authority.generation
                    or self.recovery_reference.binding.session_authority_sha256
                    != self.authority.primary_authority_sha256
                    or self.recovery_reference.binding.intent_sha256
                    != self.authority.source_intent_sha256
                    or (
                        type(self.authority) is IntentBoundRecoveryAuthority
                        and self.recovery_reference.binding.binding_sha256
                        != self.authority.intent_binding_sha256
                    )
                )
            )
        ):
            raise CredentialExecutionSessionError("SESSION_INIT_INVALID")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "capability": self.capability.value,
            "authority": _authority_to_payload(self.authority),
            "execution_journal_path": str(self.execution_journal_path),
            "recovery_reference": (
                self.recovery_reference.to_payload()
                if self.recovery_reference is not None
                else None
            ),
        }

    @classmethod
    def from_payload(cls, value: object) -> SessionInitCommand:
        item = _exact_mapping(value, _SESSION_INIT_FIELDS, "SESSION_INIT_FIELDS_INVALID")
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        try:
            capability = GenerationCapability(item["capability"])
        except (TypeError, ValueError) as exc:
            raise CredentialExecutionSessionError("SESSION_CAPABILITY_INVALID") from exc
        path_value = item["execution_journal_path"]
        if type(path_value) is not str:
            raise CredentialExecutionSessionError("SESSION_JOURNAL_PATH_INVALID")
        recovery_value = item["recovery_reference"]
        recovery_reference = (
            None if recovery_value is None else IntentBindingReference.from_payload(recovery_value)
        )
        return cls(
            authority=_authority_from_payload(item["authority"]),
            capability=capability,
            execution_journal_path=Path(path_value),
            recovery_reference=recovery_reference,
        )


_INTENT_REFERENCE_FIELDS = frozenset(
    {
        "schema_version",
        "binding_sha256",
        "session_authority_sha256",
        "intent_sha256",
        "intent_file_sha256",
        "intent_path_sha256",
        "pre_intent_chain_sha256",
        "last_ledger_sha256",
        "intent_path",
        "generation",
    }
)


@dataclass(frozen=True, slots=True)
class IntentBindingReference:
    """Exact non-secret journal binding plus independently replayable path."""

    binding: IntentChainBinding
    intent_path: Path
    generation: int

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not IntentChainBinding
            or not isinstance(self.intent_path, Path)
            or not self.intent_path.is_absolute()
            or str(self.intent_path.absolute()) != str(self.intent_path)
            or "\0" in str(self.intent_path)
            or len(str(self.intent_path)) > 4096
            or not _positive_int(self.generation)
        ):
            raise CredentialExecutionSessionError("INTENT_REFERENCE_INVALID")
        expected_path_sha256 = hashlib.sha256(
            _canonical_json({"path": str(self.intent_path.absolute())})
        ).hexdigest()
        if not hmac.compare_digest(
            expected_path_sha256,
            self.binding.intent_path_sha256,
        ):
            raise CredentialExecutionSessionError("INTENT_PATH_BINDING_MISMATCH")

    @classmethod
    def from_binding(
        cls,
        binding: IntentChainBinding,
        *,
        intent_path: Path,
        generation: int,
    ) -> IntentBindingReference:
        return cls(
            binding=binding,
            intent_path=intent_path.absolute(),
            generation=generation,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "binding_sha256": self.binding.binding_sha256,
            "session_authority_sha256": self.binding.session_authority_sha256,
            "intent_sha256": self.binding.intent_sha256,
            "intent_file_sha256": self.binding.intent_file_sha256,
            "intent_path_sha256": self.binding.intent_path_sha256,
            "pre_intent_chain_sha256": self.binding.pre_intent_chain_sha256,
            "last_ledger_sha256": self.binding.last_ledger_sha256,
            "intent_path": str(self.intent_path),
            "generation": self.generation,
        }

    @classmethod
    def from_payload(cls, value: object) -> IntentBindingReference:
        item = _exact_mapping(
            value,
            _INTENT_REFERENCE_FIELDS,
            "INTENT_REFERENCE_FIELDS_INVALID",
        )
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        try:
            binding = IntentChainBinding(
                binding_sha256=item["binding_sha256"],
                session_authority_sha256=item["session_authority_sha256"],
                intent_sha256=item["intent_sha256"],
                intent_file_sha256=item["intent_file_sha256"],
                intent_path_sha256=item["intent_path_sha256"],
                pre_intent_chain_sha256=item["pre_intent_chain_sha256"],
                last_ledger_sha256=item["last_ledger_sha256"],
            )
            path_value = item["intent_path"]
            if type(path_value) is not str:
                raise TypeError
            return cls(
                binding=binding,
                intent_path=Path(path_value),
                generation=item["generation"],
            )
        except (TypeError, ValueError) as exc:
            raise CredentialExecutionSessionError("INTENT_REFERENCE_INVALID") from exc


_BIND_COMMAND_FIELDS = frozenset(
    {
        "schema_version",
        "reference",
        "phase_permit_sequence",
        "phase_permit_digest",
        "deadline_ns",
    }
)


@dataclass(frozen=True, slots=True)
class BindIntentCommand:
    reference: IntentBindingReference
    phase_permit: PhaseDeadlinePermit

    def __post_init__(self) -> None:
        if (
            type(self.reference) is not IntentBindingReference
            or type(self.phase_permit) is not PhaseDeadlinePermit
            or self.reference.generation != self.phase_permit.generation
        ):
            raise CredentialExecutionSessionError("BIND_INTENT_INVALID")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "reference": self.reference.to_payload(),
            "phase_permit_sequence": self.phase_permit.sequence,
            "phase_permit_digest": self.phase_permit.digest,
            "deadline_ns": _seconds_to_ns(self.phase_permit.absolute_deadline),
        }

    @classmethod
    def from_payload(
        cls,
        value: object,
        *,
        phase_permit: PhaseDeadlinePermit,
    ) -> BindIntentCommand:
        item = _exact_mapping(value, _BIND_COMMAND_FIELDS, "BIND_INTENT_FIELDS_INVALID")
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        command = cls(
            reference=IntentBindingReference.from_payload(item["reference"]),
            phase_permit=phase_permit,
        )
        if (
            item["phase_permit_sequence"] != phase_permit.sequence
            or item["phase_permit_digest"] != phase_permit.digest
            or item["deadline_ns"] != _seconds_to_ns(phase_permit.absolute_deadline)
        ):
            raise CredentialExecutionSessionError("PHASE_PERMIT_BINDING_MISMATCH")
        return command


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
    item = _exact_mapping(value, _LEDGER_FIELDS, "READ_LEDGER_FIELDS_INVALID")
    try:
        stage_value = item["stage"]
        if type(stage_value) is not str:
            raise TypeError
        return MutationLedger(
            total_http_requests=item["total_http_requests"],
            create_requests=item["create_requests"],
            cancel_requests=item["cancel_requests"],
            emergency_close_requests=item["emergency_close_requests"],
            read_retry_requests=item["read_retry_requests"],
            post_create_read_requests=item["post_create_read_requests"],
            stage=RequestStage(stage_value),
            last_elapsed_seconds=_parse_decimal(
                item["last_elapsed_seconds"],
                "READ_LEDGER_ELAPSED_INVALID",
            ),
            retryable_read_sha256=item["retryable_read_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise CredentialExecutionSessionError("READ_LEDGER_INVALID") from exc


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


def _parameters_to_payload(parameters: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return [[key, value] for key, value in parameters]


def _parameters_from_payload(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not list:
        raise CredentialExecutionSessionError("READ_PARAMETERS_INVALID")
    pairs: list[tuple[str, str]] = []
    for pair in value:
        if (
            type(pair) is not list
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
        ):
            raise CredentialExecutionSessionError("READ_PARAMETERS_INVALID")
        pairs.append((pair[0], pair[1]))
    normalized = tuple(pairs)
    if tuple(sorted(normalized)) != normalized or len(dict(normalized)) != len(normalized):
        raise CredentialExecutionSessionError("READ_PARAMETERS_INVALID")
    return normalized


def _reserved_request_to_payload(reserved: ReservedRequest) -> dict[str, object]:
    if type(reserved) is not ReservedRequest:
        raise CredentialExecutionSessionError("READ_RESERVATION_INVALID")
    return {
        "request_sha256": reserved.request_sha256,
        "logical_request_sha256": reserved.logical_request_sha256,
        "ledger": _ledger_to_payload(reserved.ledger),
        "intent_sha256": reserved.intent_sha256,
        "origin": reserved.origin,
        "method": reserved.method,
        "path": reserved.path,
        "purpose": reserved.purpose.value,
        "parameters": _parameters_to_payload(reserved.parameters),
        "elapsed_seconds": format(reserved.elapsed_seconds, "f"),
        "retry_index": reserved.retry_index,
    }


def _reserved_request_from_payload(value: object) -> ReservedRequest:
    item = _exact_mapping(value, _RESERVED_REQUEST_FIELDS, "READ_RESERVATION_FIELDS_INVALID")
    try:
        purpose_value = item["purpose"]
        if type(purpose_value) is not str:
            raise TypeError
        reserved = ReservedRequest(
            ledger=_ledger_from_payload(item["ledger"]),
            intent_sha256=item["intent_sha256"],
            origin=item["origin"],
            method=item["method"],
            path=item["path"],
            purpose=RequestPurpose(purpose_value),
            parameters=_parameters_from_payload(item["parameters"]),
            elapsed_seconds=_parse_decimal(
                item["elapsed_seconds"],
                "READ_ELAPSED_INVALID",
            ),
            retry_index=item["retry_index"],
        )
    except (TypeError, ValueError, MutationProtocolError) as exc:
        raise CredentialExecutionSessionError("READ_RESERVATION_INVALID") from exc
    if (
        item["request_sha256"] != reserved.request_sha256
        or item["logical_request_sha256"] != reserved.logical_request_sha256
    ):
        raise CredentialExecutionSessionError("READ_RESERVATION_DIGEST_MISMATCH")
    return reserved


_PRE_INTENT_FIELDS = frozenset(
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


def _pre_intent_to_payload(reserved: PreIntentReadReservation) -> dict[str, object]:
    if type(reserved) is not PreIntentReadReservation:
        raise CredentialExecutionSessionError("PRE_INTENT_RESERVATION_INVALID")
    return {
        "reservation_sha256": reserved.reservation_sha256,
        "logical_request_sha256": reserved.logical_request_sha256,
        "session_authority_sha256": reserved.session_authority_sha256,
        "generation": reserved.generation,
        "deadline_ns": reserved.deadline_ns,
        "origin": reserved.origin,
        "method": reserved.method,
        "path": reserved.path,
        "purpose": reserved.purpose.value,
        "parameters": _parameters_to_payload(reserved.parameters),
        "ledger": _ledger_to_payload(reserved.ledger),
        "elapsed_seconds": format(reserved.elapsed_seconds, "f"),
        "retry_index": reserved.retry_index,
    }


def _pre_intent_from_payload(value: object) -> PreIntentReadReservation:
    item = _exact_mapping(value, _PRE_INTENT_FIELDS, "PRE_INTENT_FIELDS_INVALID")
    try:
        purpose_value = item["purpose"]
        if type(purpose_value) is not str:
            raise TypeError
        reserved = PreIntentReadReservation(
            reservation_sha256=item["reservation_sha256"],
            logical_request_sha256=item["logical_request_sha256"],
            session_authority_sha256=item["session_authority_sha256"],
            generation=item["generation"],
            deadline_ns=item["deadline_ns"],
            origin=item["origin"],
            method=item["method"],
            path=item["path"],
            purpose=RequestPurpose(purpose_value),
            parameters=_parameters_from_payload(item["parameters"]),
            ledger=_ledger_from_payload(item["ledger"]),
            elapsed_seconds=_parse_decimal(
                item["elapsed_seconds"],
                "PRE_INTENT_ELAPSED_INVALID",
            ),
            retry_index=item["retry_index"],
        )
    except (TypeError, ValueError) as exc:
        raise CredentialExecutionSessionError("PRE_INTENT_RESERVATION_INVALID") from exc
    return reserved


_READ_PROOF_FIELDS = frozenset(
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


def _read_proof_to_payload(proof: ReadReservationProof) -> dict[str, object]:
    if type(proof) is not ReadReservationProof:
        raise CredentialExecutionSessionError("READ_PROOF_INVALID")
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


def _read_proof_from_payload(value: object) -> ReadReservationProof:
    item = _exact_mapping(value, _READ_PROOF_FIELDS, "READ_PROOF_FIELDS_INVALID")
    try:
        read_kind = item["read_kind"]
        purpose = item["purpose"]
        if type(read_kind) is not str or type(purpose) is not str:
            raise TypeError
        return ReadReservationProof(
            request_sha256=item["request_sha256"],
            proof_sha256=item["proof_sha256"],
            logical_request_sha256=item["logical_request_sha256"],
            read_kind=ReadKind(read_kind),
            purpose=ReadPurpose(purpose),
            method=item["method"],
            path=item["path"],
            retry_index=item["retry_index"],
            generation=item["generation"],
            deadline_ns=item["deadline_ns"],
            monotonic_sequence=item["monotonic_sequence"],
            parameters_sha256=item["parameters_sha256"],
            ledger_sha256=item["ledger_sha256"],
            source_attempt_id=item["source_attempt_id"],
            client_id=item["client_id"],
            authorization_id=item["authorization_id"],
            intent_sha256=item["intent_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise CredentialExecutionSessionError("READ_PROOF_INVALID") from exc


_READ_COMMAND_FIELDS = frozenset(
    {
        "schema_version",
        "binding_kind",
        "generation",
        "deadline_ns",
        "phase_permit_sequence",
        "phase_permit_digest",
        "reservation",
        "read_proof",
    }
)


@dataclass(frozen=True, slots=True)
class ReadCommand:
    binding_kind: ReadBindingKind
    generation: int
    deadline_ns: int
    phase_permit_sequence: int
    phase_permit_digest: str
    pre_intent_reservation: PreIntentReadReservation | None
    reserved_request: ReservedRequest | None
    read_proof: ReadReservationProof | None

    def __post_init__(self) -> None:
        if (
            type(self.binding_kind) is not ReadBindingKind
            or not _positive_int(self.generation)
            or not _positive_int(self.deadline_ns)
            or type(self.phase_permit_sequence) is not int
            or self.phase_permit_sequence < 0
            or not _is_sha256(self.phase_permit_digest)
        ):
            raise CredentialExecutionSessionError("READ_COMMAND_INVALID")
        if self.binding_kind is ReadBindingKind.PRE_INTENT:
            reserved = self.pre_intent_reservation
            if (
                type(reserved) is not PreIntentReadReservation
                or self.reserved_request is not None
                or self.read_proof is not None
                or reserved.generation != self.generation
                or reserved.deadline_ns != self.deadline_ns
            ):
                raise CredentialExecutionSessionError("PRE_INTENT_READ_BINDING_INVALID")
        else:
            reserved_request = self.reserved_request
            proof = self.read_proof
            if (
                self.pre_intent_reservation is not None
                or type(reserved_request) is not ReservedRequest
                or reserved_request.purpose is not RequestPurpose.READ
                or type(proof) is not ReadReservationProof
                or proof.generation != self.generation
                or proof.deadline_ns != self.deadline_ns
            ):
                raise CredentialExecutionSessionError("INTENT_BOUND_READ_BINDING_INVALID")
            try:
                proof.validate_reserved_request(reserved_request)
            except (TypeError, ValueError) as exc:
                raise CredentialExecutionSessionError("INTENT_BOUND_READ_BINDING_INVALID") from exc

    @classmethod
    def from_pre_intent(
        cls,
        reservation: PreIntentReadReservation,
        *,
        phase_permit: PhaseDeadlinePermit,
    ) -> ReadCommand:
        return cls(
            binding_kind=ReadBindingKind.PRE_INTENT,
            generation=reservation.generation,
            deadline_ns=reservation.deadline_ns,
            phase_permit_sequence=phase_permit.sequence,
            phase_permit_digest=phase_permit.digest,
            pre_intent_reservation=reservation,
            reserved_request=None,
            read_proof=None,
        )

    @classmethod
    def from_intent_bound(
        cls,
        reservation: ReservedRequest,
        *,
        proof: ReadReservationProof,
        phase_permit: PhaseDeadlinePermit,
    ) -> ReadCommand:
        return cls(
            binding_kind=ReadBindingKind.INTENT_BOUND,
            generation=proof.generation,
            deadline_ns=proof.deadline_ns,
            phase_permit_sequence=phase_permit.sequence,
            phase_permit_digest=phase_permit.digest,
            pre_intent_reservation=None,
            reserved_request=reservation,
            read_proof=proof,
        )

    def to_payload(self) -> dict[str, object]:
        if self.binding_kind is ReadBindingKind.PRE_INTENT:
            reservation = _pre_intent_to_payload(self.pre_intent_reservation)  # type: ignore[arg-type]
            proof: dict[str, object] | None = None
        else:
            reservation = _reserved_request_to_payload(self.reserved_request)  # type: ignore[arg-type]
            proof = _read_proof_to_payload(self.read_proof)  # type: ignore[arg-type]
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "binding_kind": self.binding_kind.value,
            "generation": self.generation,
            "deadline_ns": self.deadline_ns,
            "phase_permit_sequence": self.phase_permit_sequence,
            "phase_permit_digest": self.phase_permit_digest,
            "reservation": reservation,
            "read_proof": proof,
        }

    @classmethod
    def from_payload(cls, value: object) -> ReadCommand:
        item = _exact_mapping(value, _READ_COMMAND_FIELDS, "READ_COMMAND_FIELDS_INVALID")
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        try:
            binding_kind = ReadBindingKind(item["binding_kind"])
            if binding_kind is ReadBindingKind.PRE_INTENT:
                if item["read_proof"] is not None:
                    raise CredentialExecutionSessionError("PRE_INTENT_READ_BINDING_INVALID")
                pre_intent = _pre_intent_from_payload(item["reservation"])
                reserved = None
                proof = None
            else:
                pre_intent = None
                reserved = _reserved_request_from_payload(item["reservation"])
                proof = _read_proof_from_payload(item["read_proof"])
            return cls(
                binding_kind=binding_kind,
                generation=item["generation"],
                deadline_ns=item["deadline_ns"],
                phase_permit_sequence=item["phase_permit_sequence"],
                phase_permit_digest=item["phase_permit_digest"],
                pre_intent_reservation=pre_intent,
                reserved_request=reserved,
                read_proof=proof,
            )
        except CredentialExecutionSessionError:
            raise
        except (TypeError, ValueError) as exc:
            raise CredentialExecutionSessionError("READ_COMMAND_INVALID") from exc

    def validate_phase(
        self,
        phase_permit: PhaseDeadlinePermit,
        *,
        bootstrap: ChildBootstrap,
    ) -> None:
        now_ns = _seconds_to_ns(bootstrap.deadline.clock())
        if (
            type(phase_permit) is not PhaseDeadlinePermit
            or self.generation != bootstrap.generation
            or phase_permit.generation != bootstrap.generation
            or phase_permit.lifecycle_deadline != bootstrap.deadline.at
            or self.phase_permit_sequence != phase_permit.sequence
            or not hmac.compare_digest(self.phase_permit_digest, phase_permit.digest)
            or self.deadline_ns != _seconds_to_ns(phase_permit.absolute_deadline)
            or self.deadline_ns > _seconds_to_ns(phase_permit.lifecycle_deadline)
            or self.deadline_ns <= now_ns
        ):
            raise CredentialExecutionSessionError("READ_PHASE_PERMIT_MISMATCH")

    def result_payload(self, result: TransportResult) -> dict[str, object]:
        if type(result) is not TransportResult:
            raise CredentialExecutionSessionError("TRANSPORT_RESULT_INVALID")
        if self.binding_kind is ReadBindingKind.PRE_INTENT:
            reservation = self.pre_intent_reservation
            if reservation is None:  # pragma: no cover - constructor invariant.
                raise CredentialExecutionSessionError("READ_COMMAND_INVALID")
            request_sha256 = reservation.reservation_sha256
            logical_sha256 = reservation.logical_request_sha256
            proof_sha256 = None
        else:
            reservation = self.reserved_request
            proof = self.read_proof
            if reservation is None or proof is None:  # pragma: no cover
                raise CredentialExecutionSessionError("READ_COMMAND_INVALID")
            request_sha256 = reservation.request_sha256
            logical_sha256 = reservation.logical_request_sha256
            proof_sha256 = proof.proof_sha256
        if (
            result.request_sha256 != request_sha256
            or result.logical_request_sha256 != logical_sha256
        ):
            raise CredentialExecutionSessionError("TRANSPORT_RESULT_BINDING_MISMATCH")
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "binding_kind": self.binding_kind.value,
            "generation": self.generation,
            "reservation_sha256": request_sha256,
            "read_proof_sha256": proof_sha256,
            "result": transport_result_to_payload(result),
        }


_READ_FAILURE_FIELDS = frozenset(
    {
        "schema_version",
        "binding_kind",
        "generation",
        "reservation_sha256",
        "read_proof_sha256",
        "failure_kind",
        "io_may_have_occurred",
        "digest",
    }
)


@dataclass(frozen=True, slots=True)
class ReadFailureResult:
    """Exact reservation-bound failure projected without exception material."""

    binding_kind: ReadBindingKind
    generation: int
    reservation_sha256: str
    read_proof_sha256: str | None
    failure_kind: ReadFailureKind
    io_may_have_occurred: bool
    digest: str

    @classmethod
    def build(
        cls,
        command: ReadCommand,
        *,
        failure_kind: ReadFailureKind,
        io_may_have_occurred: bool,
    ) -> ReadFailureResult:
        if type(command) is not ReadCommand:
            raise CredentialExecutionSessionError("READ_FAILURE_COMMAND_INVALID")
        if command.binding_kind is ReadBindingKind.PRE_INTENT:
            reservation = command.pre_intent_reservation
            if reservation is None:  # pragma: no cover - constructor invariant.
                raise CredentialExecutionSessionError("READ_FAILURE_COMMAND_INVALID")
            reservation_sha256 = reservation.reservation_sha256
            proof_sha256 = None
        else:
            reservation = command.reserved_request
            proof = command.read_proof
            if reservation is None or proof is None:  # pragma: no cover
                raise CredentialExecutionSessionError("READ_FAILURE_COMMAND_INVALID")
            reservation_sha256 = reservation.request_sha256
            proof_sha256 = proof.proof_sha256
        body = cls._body(
            binding_kind=command.binding_kind,
            generation=command.generation,
            reservation_sha256=reservation_sha256,
            read_proof_sha256=proof_sha256,
            failure_kind=failure_kind,
            io_may_have_occurred=io_may_have_occurred,
        )
        return cls(
            binding_kind=command.binding_kind,
            generation=command.generation,
            reservation_sha256=reservation_sha256,
            read_proof_sha256=proof_sha256,
            failure_kind=failure_kind,
            io_may_have_occurred=io_may_have_occurred,
            digest=hashlib.sha256(_canonical_json(body)).hexdigest(),
        )

    def __post_init__(self) -> None:
        if (
            type(self.binding_kind) is not ReadBindingKind
            or not _positive_int(self.generation)
            or not _is_sha256(self.reservation_sha256)
            or (self.read_proof_sha256 is not None and not _is_sha256(self.read_proof_sha256))
            or (
                self.binding_kind is ReadBindingKind.PRE_INTENT
                and self.read_proof_sha256 is not None
            )
            or (
                self.binding_kind is ReadBindingKind.INTENT_BOUND and self.read_proof_sha256 is None
            )
            or type(self.failure_kind) is not ReadFailureKind
            or type(self.io_may_have_occurred) is not bool
            or not _is_sha256(self.digest)
        ):
            raise CredentialExecutionSessionError("READ_FAILURE_INVALID")
        expected = hashlib.sha256(
            _canonical_json(
                self._body(
                    binding_kind=self.binding_kind,
                    generation=self.generation,
                    reservation_sha256=self.reservation_sha256,
                    read_proof_sha256=self.read_proof_sha256,
                    failure_kind=self.failure_kind,
                    io_may_have_occurred=self.io_may_have_occurred,
                )
            )
        ).hexdigest()
        if not hmac.compare_digest(expected, self.digest):
            raise CredentialExecutionSessionError("READ_FAILURE_DIGEST_MISMATCH")

    @staticmethod
    def _body(
        *,
        binding_kind: ReadBindingKind,
        generation: int,
        reservation_sha256: str,
        read_proof_sha256: str | None,
        failure_kind: ReadFailureKind,
        io_may_have_occurred: bool,
    ) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "binding_kind": binding_kind.value,
            "generation": generation,
            "reservation_sha256": reservation_sha256,
            "read_proof_sha256": read_proof_sha256,
            "failure_kind": failure_kind.value,
            "io_may_have_occurred": io_may_have_occurred,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._body(
                binding_kind=self.binding_kind,
                generation=self.generation,
                reservation_sha256=self.reservation_sha256,
                read_proof_sha256=self.read_proof_sha256,
                failure_kind=self.failure_kind,
                io_may_have_occurred=self.io_may_have_occurred,
            ),
            "digest": self.digest,
        }

    @classmethod
    def from_payload(cls, value: object) -> ReadFailureResult:
        item = _exact_mapping(value, _READ_FAILURE_FIELDS, "READ_FAILURE_FIELDS_INVALID")
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        try:
            binding_kind_value = item["binding_kind"]
            failure_kind_value = item["failure_kind"]
            if type(binding_kind_value) is not str or type(failure_kind_value) is not str:
                raise TypeError
            return cls(
                binding_kind=ReadBindingKind(binding_kind_value),
                generation=item["generation"],
                reservation_sha256=item["reservation_sha256"],
                read_proof_sha256=item["read_proof_sha256"],
                failure_kind=project_read_failure_kind(failure_kind_value),
                io_may_have_occurred=item["io_may_have_occurred"],
                digest=item["digest"],
            )
        except CredentialExecutionSessionError:
            raise
        except (ExecutionJournalError, TypeError, ValueError) as exc:
            raise CredentialExecutionSessionError("READ_FAILURE_INVALID") from exc


_TRANSPORT_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "logical_request_sha256",
        "result_sha256",
        "kind",
        "fields",
    }
)


def transport_result_to_payload(result: TransportResult) -> dict[str, object]:
    if type(result) is not TransportResult:
        raise CredentialExecutionSessionError("TRANSPORT_RESULT_INVALID")
    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "request_sha256": result.request_sha256,
        "logical_request_sha256": result.logical_request_sha256,
        "result_sha256": result.result_sha256,
        "kind": result.kind.value,
        "fields": [[name, value] for name, value in result.fields],
    }


def transport_result_from_payload(value: object) -> TransportResult:
    item = _exact_mapping(value, _TRANSPORT_RESULT_FIELDS, "TRANSPORT_RESULT_FIELDS_INVALID")
    if item["schema_version"] != SESSION_SCHEMA_VERSION or type(item["fields"]) is not list:
        raise CredentialExecutionSessionError("TRANSPORT_RESULT_INVALID")
    fields: list[tuple[str, object]] = []
    for pair in item["fields"]:
        if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
            raise CredentialExecutionSessionError("TRANSPORT_RESULT_INVALID")
        fields.append((pair[0], pair[1]))
    try:
        kind_value = item["kind"]
        if type(kind_value) is not str:
            raise TypeError
        return TransportResult(
            request_sha256=item["request_sha256"],
            logical_request_sha256=item["logical_request_sha256"],
            result_sha256=item["result_sha256"],
            kind=ResponseKind(kind_value),
            fields=tuple(fields),
        )
    except (TypeError, ValueError, CredentialTransportError) as exc:
        raise CredentialExecutionSessionError("TRANSPORT_RESULT_INVALID") from exc


_FINISH_FIELDS = frozenset(
    {
        "schema_version",
        "generation",
        "final_state",
        "final_evidence_sha256",
        "phase_permit_sequence",
        "phase_permit_digest",
        "deadline_ns",
    }
)


@dataclass(frozen=True, slots=True)
class SessionFinishCommand:
    generation: int
    final_state: SessionFinalState | str
    final_evidence_sha256: str | None
    phase_permit: PhaseDeadlinePermit

    def __post_init__(self) -> None:
        try:
            normalized = SessionFinalState(self.final_state)
        except (TypeError, ValueError) as exc:
            raise CredentialExecutionSessionError("SESSION_FINAL_STATE_INVALID") from exc
        object.__setattr__(self, "final_state", normalized)
        if (
            not _positive_int(self.generation)
            or type(self.phase_permit) is not PhaseDeadlinePermit
            or self.phase_permit.generation != self.generation
            or (
                self.final_evidence_sha256 is not None
                and not _is_sha256(self.final_evidence_sha256)
            )
            or (normalized is SessionFinalState.COMPLETED and self.final_evidence_sha256 is None)
        ):
            raise CredentialExecutionSessionError("SESSION_FINISH_INVALID")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "generation": self.generation,
            "final_state": self.final_state.value,
            "final_evidence_sha256": self.final_evidence_sha256,
            "phase_permit_sequence": self.phase_permit.sequence,
            "phase_permit_digest": self.phase_permit.digest,
            "deadline_ns": _seconds_to_ns(self.phase_permit.absolute_deadline),
        }

    @classmethod
    def from_payload(
        cls,
        value: object,
        *,
        phase_permit: PhaseDeadlinePermit,
    ) -> SessionFinishCommand:
        item = _exact_mapping(value, _FINISH_FIELDS, "SESSION_FINISH_FIELDS_INVALID")
        if item["schema_version"] != SESSION_SCHEMA_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        command = cls(
            generation=item["generation"],
            final_state=item["final_state"],
            final_evidence_sha256=item["final_evidence_sha256"],
            phase_permit=phase_permit,
        )
        if (
            item["phase_permit_sequence"] != phase_permit.sequence
            or item["phase_permit_digest"] != phase_permit.digest
            or item["deadline_ns"] != _seconds_to_ns(phase_permit.absolute_deadline)
        ):
            raise CredentialExecutionSessionError("PHASE_PERMIT_BINDING_MISMATCH")
        return command


class _VerifiedIntentResolver(Protocol):
    """Verify the durable journal chain, then return its replayed intent."""

    def __call__(self, reference: IntentBindingReference) -> PersistedIntent: ...


@dataclass(slots=True)
class _BufferedChannel:
    message: IPCMessage
    delegate: Any
    consumed: bool = False

    def receive(self) -> IPCMessage:
        if self.consumed:
            raise CredentialExecutionSessionError("BUFFERED_COMMAND_ALREADY_CONSUMED")
        self.consumed = True
        return self.message

    def send(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.delegate.send(kind, payload)


class CredentialExecutionSession:
    """One synchronous, single-generation credential child command router."""

    def __init__(
        self,
        *,
        bootstrap: ChildBootstrap,
        transport: ProcessBoundCredentialTransport,
        verified_intent_resolver: _VerifiedIntentResolver,
    ) -> None:
        if (
            type(bootstrap) is not ChildBootstrap
            or type(transport) is not ProcessBoundCredentialTransport
            or not callable(verified_intent_resolver)
        ):
            raise CredentialExecutionSessionError("SESSION_BINDING_INVALID")
        self.bootstrap = bootstrap
        self._transport = transport
        self._verified_intent_resolver = verified_intent_resolver
        self._authority: (
            SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority | None
        ) = None
        self._execution_journal_path: Path | None = None
        self._persisted_intent: PersistedIntent | None = None
        self._started = False
        self._finished = False

    def __repr__(self) -> str:
        capability = self.bootstrap.capability.value
        return (
            f"{type(self).__name__}(generation={self.bootstrap.generation}, "
            f"capability={capability!r}, started={self._started}, "
            f"finished={self._finished})"
        )

    @property
    def execution_journal_path(self) -> Path:
        """Validated journal path used to derive fixed child-owned evidence."""

        if not self._started:
            raise CredentialExecutionSessionError("SESSION_NOT_STARTED")
        return self._require_execution_journal_path()

    @property
    def finished(self) -> bool:
        """Whether the exact terminal command has been accepted."""

        return self._finished

    def start(self) -> None:
        if self._started:
            raise CredentialExecutionSessionError("SESSION_ALREADY_STARTED")
        try:
            message = self.bootstrap.channel.receive()
        except BaseException:
            raise CredentialExecutionSessionError("SESSION_INIT_RECEIVE_FAILED") from None
        if message.version != IPC_VERSION or message.kind != "SESSION_INIT":
            raise CredentialExecutionSessionError("SESSION_INIT_REQUIRED")
        command = SessionInitCommand.from_payload(message.payload)
        if (
            command.authority.generation != self.bootstrap.generation
            or command.capability is not self.bootstrap.capability
        ):
            raise CredentialExecutionSessionError("SESSION_INIT_BINDING_MISMATCH")
        self._authority = command.authority
        self._execution_journal_path = command.execution_journal_path
        if command.capability is GenerationCapability.RECOVERY:
            reference = command.recovery_reference
            if reference is None:  # pragma: no cover - command invariant.
                raise CredentialExecutionSessionError("RECOVERY_INTENT_REFERENCE_REQUIRED")
            self._persisted_intent = self._verify_recovery_init(
                command.authority,
                reference,
            )  # type: ignore[arg-type]
        self._started = True
        ready: dict[str, object] = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "status": "READY",
            "generation": self.bootstrap.generation,
            "capability": self.bootstrap.capability.value,
            "authority_sha256": command.authority.authority_sha256,
        }
        if self._persisted_intent is not None:
            ready["intent_sha256"] = self._persisted_intent.intent.intent_sha256
        self.bootstrap.channel.send("SESSION_READY", ready)

    def run(self) -> None:
        self.start()
        while self.run_next():
            pass

    def run_next(self) -> bool:
        if not self._started:
            raise CredentialExecutionSessionError("SESSION_NOT_STARTED")
        if self._finished:
            raise CredentialExecutionSessionError("SESSION_ALREADY_FINISHED")
        try:
            phase_permit = self.bootstrap.accept_phase_permit()
        except BaseException:
            raise CredentialExecutionSessionError("PHASE_PERMIT_REJECTED") from None
        try:
            message = self.bootstrap.channel.receive()
        except BaseException:
            raise CredentialExecutionSessionError("SESSION_COMMAND_RECEIVE_FAILED") from None
        if message.version != IPC_VERSION:
            raise CredentialExecutionSessionError("SESSION_VERSION_MISMATCH")
        if message.kind == "BIND_INTENT":
            if self.bootstrap.capability is GenerationCapability.RECOVERY:
                raise CredentialExecutionSessionError("RECOVERY_BIND_INTENT_FORBIDDEN")
            self._bind_intent(message.payload, phase_permit)
            return True
        if message.kind == "READ":
            self._execute_read(message.payload, phase_permit)
            return True
        if message.kind == "GO":
            self._execute_go(message, phase_permit)
            return True
        if message.kind == "SESSION_FINISH":
            self._finish(message.payload, phase_permit)
            return False
        raise CredentialExecutionSessionError("SESSION_COMMAND_KIND_INVALID")

    def _bind_intent(
        self,
        payload: object,
        phase_permit: PhaseDeadlinePermit,
    ) -> None:
        if self._persisted_intent is not None:
            raise CredentialExecutionSessionError("INTENT_ALREADY_BOUND")
        command = BindIntentCommand.from_payload(payload, phase_permit=phase_permit)
        reference = command.reference
        authority = self._require_authority()
        if (
            reference.generation != self.bootstrap.generation
            or reference.binding.session_authority_sha256 != authority.authority_sha256
        ):
            raise CredentialExecutionSessionError("INTENT_AUTHORITY_MISMATCH")
        try:
            resolved = self._verified_intent_resolver(reference)
        except BaseException:
            raise CredentialExecutionSessionError("INTENT_RECEIPT_VERIFICATION_FAILED") from None
        if type(resolved) is not PersistedIntent:
            raise CredentialExecutionSessionError("INTENT_RECEIPT_MISMATCH")
        try:
            replayed = load_persisted_intent(resolved.path)
        except (DurableIntentError, OSError, TypeError, ValueError):
            raise CredentialExecutionSessionError("INTENT_RECEIPT_REPLAY_FAILED") from None
        intent = replayed.intent
        try:
            replayed_authority = SessionAuthority.build(
                authorization_id=intent.authorization_id,
                runtime_commit=intent.runtime_commit,
                session_nonce=intent.session_nonce,
                generation=self.bootstrap.generation,
            )
        except (TypeError, ValueError):
            raise CredentialExecutionSessionError("INTENT_LINEAGE_MISMATCH") from None
        if (
            replayed != resolved
            or replayed.path.absolute() != reference.intent_path.absolute()
            or replayed.file_sha256 != reference.binding.intent_file_sha256
            or intent.intent_sha256 != reference.binding.intent_sha256
            or replayed_authority != authority
        ):
            raise CredentialExecutionSessionError("INTENT_RECEIPT_MISMATCH")
        self._persisted_intent = replayed
        self.bootstrap.channel.send(
            "INTENT_BOUND",
            {
                "schema_version": SESSION_SCHEMA_VERSION,
                "status": "BOUND",
                "generation": self.bootstrap.generation,
                "authority_sha256": authority.authority_sha256,
                "binding_sha256": reference.binding.binding_sha256,
                "intent_sha256": intent.intent_sha256,
                "intent_file_sha256": replayed.file_sha256,
            },
        )

    def _verify_recovery_init(
        self,
        authority: RecoverySessionAuthority | IntentBoundRecoveryAuthority,
        reference: IntentBindingReference,
    ) -> PersistedIntent:
        journal_path = self._require_execution_journal_path()
        try:
            journal = ExecutionJournal(journal_path)
            records = journal.records()
        except (ExecutionJournalError, OSError, TypeError, ValueError):
            raise CredentialExecutionSessionError("RECOVERY_JOURNAL_REPLAY_FAILED") from None
        authority_event_name = (
            "_RecoverySessionAuthorityIssued"
            if type(authority) is RecoverySessionAuthority
            else "_IntentBoundRecoveryAuthorityIssued"
        )
        authority_seen = False
        binding_seen = False
        active_generation = False
        reaped_generation = False
        for record in records:
            event = record.event
            event_name = type(event).__name__
            if (
                event_name == authority_event_name
                and getattr(event, "authority", None) == authority
            ):
                authority_seen = True
            elif (
                event_name == "_IntentChainBound"
                and getattr(event, "binding", None) == reference.binding
            ):
                binding_seen = True
            elif (
                event_name == "_GenerationAdmitted"
                and getattr(event, "generation", None) == authority.generation
                and getattr(event, "capability", None) is GenerationCapability.RECOVERY
            ):
                active_generation = True
            elif event_name == "_GenerationReaped":
                receipt = getattr(event, "receipt", None)
                if getattr(receipt, "generation", None) == authority.generation:
                    reaped_generation = True
        if not authority_seen or not binding_seen or not active_generation or reaped_generation:
            raise CredentialExecutionSessionError("RECOVERY_AUTHORITY_REPLAY_MISMATCH")
        try:
            resolved = self._verified_intent_resolver(reference)
            replayed = load_persisted_intent(reference.intent_path)
        except BaseException:
            raise CredentialExecutionSessionError("RECOVERY_INTENT_REPLAY_FAILED") from None
        intent = replayed.intent
        expected_client_id = (
            authority.source_client_id
            if type(authority) is RecoverySessionAuthority
            else authority.query_client_id
        )
        if (
            type(resolved) is not PersistedIntent
            or resolved != replayed
            or replayed.file_sha256 != reference.binding.intent_file_sha256
            or intent.intent_sha256 != authority.source_intent_sha256
            or intent.authorization_id != authority.source_authorization_id
            or intent.runtime_commit != authority.source_runtime_commit
            or intent.session_nonce != authority.source_session_nonce
            or intent.client_order_id != expected_client_id
            or (
                type(authority) is IntentBoundRecoveryAuthority
                and authority.intent_binding_sha256 != reference.binding.binding_sha256
            )
        ):
            raise CredentialExecutionSessionError("RECOVERY_INTENT_LINEAGE_MISMATCH")
        return replayed

    def _execute_read(
        self,
        payload: object,
        phase_permit: PhaseDeadlinePermit,
    ) -> None:
        command = ReadCommand.from_payload(payload)
        command.validate_phase(phase_permit, bootstrap=self.bootstrap)
        authority = self._require_authority()
        try:
            self.bootstrap.assert_network_ready()
        except BaseException:
            self._send_read_failure(
                command,
                failure_kind=ReadFailureKind.NETWORK_GUARD,
                io_may_have_occurred=False,
            )
            return
        if command.binding_kind is ReadBindingKind.PRE_INTENT:
            if self.bootstrap.capability is GenerationCapability.RECOVERY:
                raise CredentialExecutionSessionError("RECOVERY_PRE_INTENT_FORBIDDEN")
            if self._persisted_intent is not None:
                raise CredentialExecutionSessionError("PRE_INTENT_PHASE_CLOSED")
            reservation = command.pre_intent_reservation
            if (
                reservation is None
                or reservation.session_authority_sha256 != authority.authority_sha256
            ):
                raise CredentialExecutionSessionError("PRE_INTENT_AUTHORITY_MISMATCH")
            try:
                exact = self._verify_pre_intent_dispatch(reservation)
                result = self._transport.execute_pre_intent(
                    exact,
                    absolute_deadline_ns=command.deadline_ns,
                )
            except CredentialExecutionSessionError:
                raise
            except CredentialTransportError as exc:
                self._send_transport_read_failure(command, exc)
                return
            except BaseException:
                self._send_read_failure(
                    command,
                    failure_kind=ReadFailureKind.EXECUTOR_FAILURE,
                    io_may_have_occurred=True,
                )
                return
        else:
            persisted = self._persisted_intent
            if persisted is None:
                raise CredentialExecutionSessionError("INTENT_BINDING_REQUIRED")
            reservation = command.reserved_request
            proof = command.read_proof
            if (
                reservation is None
                or proof is None
                or reservation.intent_sha256 != persisted.intent.intent_sha256
                or proof.intent_sha256 != persisted.intent.intent_sha256
                or proof.authorization_id != self._authority_authorization_id(authority)
            ):
                raise CredentialExecutionSessionError("INTENT_BOUND_READ_LINEAGE_MISMATCH")
            try:
                result = self._transport.execute(
                    reservation,
                    absolute_deadline_ns=command.deadline_ns,
                )
            except CredentialTransportError as exc:
                self._send_transport_read_failure(command, exc)
                return
            except BaseException:
                self._send_read_failure(
                    command,
                    failure_kind=ReadFailureKind.EXECUTOR_FAILURE,
                    io_may_have_occurred=True,
                )
                return
        try:
            response = command.result_payload(result)
        except BaseException:
            self._send_read_failure(
                command,
                failure_kind=ReadFailureKind.RESULT_INVALID,
                io_may_have_occurred=True,
            )
            return
        self.bootstrap.channel.send("READ_RESULT", response)

    def _send_transport_read_failure(
        self,
        command: ReadCommand,
        error: CredentialTransportError,
    ) -> None:
        failure_name = {
            "READ_IO_AMBIGUOUS": "IO_AMBIGUOUS",
            "READ_RESPONSE_INVALID": "RESPONSE_INVALID",
        }.get(error.reason, "TRANSPORT_REJECTED")
        try:
            failure_kind = project_read_failure_kind(failure_name)
        except ExecutionJournalError:
            failure_kind = project_read_failure_kind("TRANSPORT_REJECTED")
        self._send_read_failure(
            command,
            failure_kind=failure_kind,
            io_may_have_occurred=error.post_dispatch,
        )

    def _send_read_failure(
        self,
        command: ReadCommand,
        *,
        failure_kind: ReadFailureKind,
        io_may_have_occurred: bool,
    ) -> None:
        failure = ReadFailureResult.build(
            command,
            failure_kind=failure_kind,
            io_may_have_occurred=io_may_have_occurred,
        )
        self.bootstrap.channel.send("READ_FAILURE", failure.to_payload())

    def _execute_go(
        self,
        message: IPCMessage,
        phase_permit: PhaseDeadlinePermit,
    ) -> None:
        persisted = self._persisted_intent
        if persisted is None:
            raise CredentialExecutionSessionError("INTENT_BINDING_REQUIRED")
        authority = self._require_authority()
        try:
            command = GoCommand.from_payload(message.payload)
        except (DispatchKernelError, TypeError, ValueError):
            raise CredentialExecutionSessionError("GO_COMMAND_INVALID") from None
        if type(authority) is IntentBoundRecoveryAuthority:
            raise CredentialExecutionSessionError("INTENT_BOUND_RECOVERY_MUTATION_FORBIDDEN")
        if (
            command.generation != self.bootstrap.generation
            or command.attempt.intent_sha256 != persisted.intent.intent_sha256
            or command.attempt.authorization_id != self._authority_authorization_id(authority)
            or command.phase_permit_sequence != phase_permit.sequence
            or command.phase_permit_digest != phase_permit.digest
        ):
            raise CredentialExecutionSessionError("GO_LINEAGE_MISMATCH")
        try:
            self.bootstrap.assert_mutation_allowed(command.attempt.kind)
        except CredentialBoundaryError as exc:
            if str(exc) == "RECOVERY_CREATE_CAPABILITY_FORBIDDEN":
                raise CredentialExecutionSessionError(
                    "RECOVERY_CREATE_CAPABILITY_FORBIDDEN"
                ) from None
            raise CredentialExecutionSessionError("MUTATION_CAPABILITY_REJECTED") from None
        buffered = _BufferedChannel(message=message, delegate=self.bootstrap.channel)
        dispatcher = ChildDispatcher(
            channel=buffered,
            generation=self.bootstrap.generation,
            lifecycle_deadline=self.bootstrap.deadline,
            hard_deadline=self.bootstrap.hard_deadline,
        )

        def execute(reservation: ReservedRequest) -> ConfirmedIO:
            try:
                kind = {
                    RequestPurpose.CREATE: MutationKind.CREATE,
                    RequestPurpose.CANCEL: MutationKind.CANCEL,
                    RequestPurpose.EMERGENCY_CLOSE: MutationKind.EMERGENCY_CLOSE,
                }.get(reservation.purpose)
                if kind is None:
                    raise CredentialExecutionSessionError("MUTATION_REQUEST_REQUIRED")
                self.bootstrap.assert_mutation_allowed(kind)
                journal = ExecutionJournal(self._require_execution_journal_path())
                receipt = journal.verify_child_economic_binding(
                    attempt=command.attempt,
                    reservation_proof=command.reservation_proof,
                    reserved_request=reservation,
                    persisted_intent_path=persisted.path,
                )
                exact = receipt.reserved_request
                if exact != reservation:
                    raise CredentialExecutionSessionError("MUTATION_DISPATCH_RECEIPT_INVALID")
                self.bootstrap.assert_network_ready()
                result = self._transport.execute(
                    exact,
                    absolute_deadline_ns=command.go_deadline_ns,
                )
                if (
                    result.kind is not ResponseKind.MUTATION_ACK
                    or result.request_sha256 != exact.request_sha256
                    or result.logical_request_sha256 != exact.logical_request_sha256
                ):
                    raise CredentialExecutionSessionError("MUTATION_RESULT_INVALID")
                return ConfirmedIO(result)
            except BaseException:
                raise CredentialExecutionSessionError("MUTATION_EXECUTION_FAILED") from None

        try:
            dispatcher.dispatch_once(execute, phase_permit=phase_permit)
        except CredentialExecutionSessionError:
            raise
        except (DispatchKernelError, ProcessBoundaryError, TypeError, ValueError):
            raise CredentialExecutionSessionError("GO_DISPATCH_FAILED") from None

    def _finish(
        self,
        payload: object,
        phase_permit: PhaseDeadlinePermit,
    ) -> None:
        command = SessionFinishCommand.from_payload(payload, phase_permit=phase_permit)
        if command.generation != self.bootstrap.generation:
            raise CredentialExecutionSessionError("SESSION_FINISH_GENERATION_MISMATCH")
        self._finished = True
        self.bootstrap.channel.send(
            "SESSION_FINISHED",
            {
                "schema_version": SESSION_SCHEMA_VERSION,
                "status": "FINISHED",
                "generation": self.bootstrap.generation,
                "final_state": command.final_state.value,
                "final_evidence_sha256": command.final_evidence_sha256,
            },
        )

    def _verify_pre_intent_dispatch(
        self,
        reservation: PreIntentReadReservation,
    ) -> PreIntentReadReservation:
        """Reopen the durable frontier and return only its exact pending request."""

        try:
            journal = ExecutionJournal(self._require_execution_journal_path())
            records = journal.records()
            snapshot = journal.request_ledger_snapshot(reservation.session_authority_sha256)
        except (ExecutionJournalError, OSError, TypeError, ValueError):
            raise CredentialExecutionSessionError("PRE_INTENT_DURABLE_PREPARED_REQUIRED") from None
        matches = tuple(
            getattr(record.event, "reservation", None)
            for record in records
            if type(record.event).__name__ == "_PreIntentReadReserved"
            and getattr(record.event, "reservation", None) == reservation
        )
        if (
            matches != (reservation,)
            or snapshot.authority.authority_sha256 != reservation.session_authority_sha256
            or snapshot.pending_pre_intent_reads != (reservation,)
        ):
            raise CredentialExecutionSessionError("PRE_INTENT_DURABLE_PREPARED_REQUIRED")
        return matches[0]

    def _require_authority(
        self,
    ) -> SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority:
        authority = self._authority
        if authority is None:  # pragma: no cover - start establishes it.
            raise CredentialExecutionSessionError("SESSION_AUTHORITY_REQUIRED")
        return authority

    def _require_execution_journal_path(self) -> Path:
        path = self._execution_journal_path
        if path is None:  # pragma: no cover - start establishes it.
            raise CredentialExecutionSessionError("EXECUTION_JOURNAL_PATH_REQUIRED")
        return path

    @staticmethod
    def _authority_authorization_id(
        authority: (SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority),
    ) -> str:
        if type(authority) is SessionAuthority:
            return authority.authorization_id
        if type(authority) in {
            RecoverySessionAuthority,
            IntentBoundRecoveryAuthority,
        }:
            return authority.source_authorization_id
        raise CredentialExecutionSessionError("SESSION_AUTHORITY_INVALID")
