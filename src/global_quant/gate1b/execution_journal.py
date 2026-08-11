"""Durable mutation-attempt frontier for the Gate 1B process boundary.

The journal deliberately accepts only fixed, sanitized event types.  It stores no
request payloads or credentials and performs no process or network operations.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from global_quant.gate1b.mutation_protocol import (
    build_client_order_id,
    build_emergency_client_order_id,
)

SCHEMA_VERSION = "gate1b.execution-journal.v1"
MAX_RECORD_BYTES = 8_192
ZERO_DIGEST = "0" * 64

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SESSION_NONCE = re.compile(r"^[0-9a-f]{16}$")
_AUTHORIZATION_ID = re.compile(r"^g1b16-[0-9a-f]{16}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_ATTEMPT_SCHEMA = "gate1b.mutation-attempt.v1"


class ExecutionJournalError(ValueError):
    """Raised whenever journal safety or lifecycle reconstruction fails closed."""


class MutationKind(StrEnum):
    """The only mutation transports that may cross the worker boundary."""

    CREATE = "CREATE"
    CANCEL = "CANCEL"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


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


class BoundaryResult(StrEnum):
    """Sanitized supervisor observation of the worker result channel."""

    CONFIRMED = "CONFIRMED"
    ABSENT = "ABSENT"
    CORRUPT = "CORRUPT"
    EOF = "EOF"


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
            self.kind is not MutationKind.CANCEL
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


@dataclass(frozen=True, slots=True)
class RecoveryDirective:
    """Fixed read-first action for one durably UNKNOWN mutation attempt."""

    source_attempt_id: str
    source_generation: int
    kind: MutationKind
    mode: RecoveryMode
    query_client_id: str

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
    def requires_fresh_position_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE

    @property
    def requires_fresh_order_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE

    @property
    def requires_fresh_trade_state(self) -> bool:
        return self.kind is MutationKind.EMERGENCY_CLOSE

    def new_conditional_cleanup_cancel(
        self,
        *,
        generation: int,
        deadline_ns: int,
        reservation_sha256: str,
        authorization_id: str,
        intent_sha256: str,
        runtime_commit: str,
        session_nonce: str,
        fresh_open_proof_sha256: str | None,
    ) -> MutationAttempt:
        """Create a distinct retry-index-zero cancel only after a fresh OPEN proof."""

        if self.kind not in {MutationKind.CREATE, MutationKind.CANCEL}:
            raise ExecutionJournalError("CONDITIONAL_CANCEL_NOT_ALLOWED")
        if not _is_positive_int(generation) or generation <= self.source_generation:
            raise ExecutionJournalError("RECOVERY_GENERATION_REQUIRED")
        if not _is_sha256(fresh_open_proof_sha256):
            raise ExecutionJournalError("FRESH_OPEN_PROOF_REQUIRED")
        return MutationAttempt.build(
            kind=MutationKind.CANCEL,
            generation=generation,
            retry_index=0,
            deadline_ns=deadline_ns,
            reservation_sha256=reservation_sha256,
            authorization_id=authorization_id,
            intent_sha256=intent_sha256,
            runtime_commit=runtime_commit,
            session_nonce=session_nonce,
            fresh_open_proof_sha256=fresh_open_proof_sha256,
            recovery_of_attempt_id=self.source_attempt_id,
        )


@dataclass(frozen=True, slots=True)
class _JournalCreated:
    pass


@dataclass(frozen=True, slots=True)
class _GenerationAdmitted:
    generation: int
    capability: GenerationCapability


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
    generation: int


@dataclass(frozen=True, slots=True)
class _AttemptResolved:
    attempt_id: str
    generation: int
    state: FrontierState
    boundary_result: BoundaryResult


_JournalEvent = (
    _JournalCreated
    | _GenerationAdmitted
    | _AttemptPrepared
    | _GoDurable
    | _AttemptConfirmed
    | _GenerationReaped
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


def _event_to_mapping(event: _JournalEvent) -> dict[str, object]:
    if type(event) is _JournalCreated:
        return {"type": "JOURNAL_CREATED"}
    if type(event) is _GenerationAdmitted:
        return {
            "type": "GENERATION_ADMITTED",
            "generation": event.generation,
            "capability": event.capability.value,
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
        return {"type": "GENERATION_REAPED", "generation": event.generation}
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
            frozenset({"type", "generation", "capability"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _GenerationAdmitted(
            generation=item["generation"],  # type: ignore[arg-type]
            capability=_enum(GenerationCapability, item["capability"]),  # type: ignore[arg-type]
        )
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
            frozenset({"type", "generation"}),
            "JOURNAL_EVENT_FIELDS",
        )
        return _GenerationReaped(generation=item["generation"])  # type: ignore[arg-type]
    if event_type == "ATTEMPT_RESOLVED":
        item = _require_exact_fields(
            value,
            frozenset(
                {"type", "attempt_id", "generation", "state", "boundary_result"}
            ),
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
    reaped_generations: set[int] = field(default_factory=set)
    attempts: dict[str, MutationAttempt] = field(default_factory=dict)
    frontiers: dict[str, FrontierState] = field(default_factory=dict)

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
            self._apply_generation_admitted(event)
        elif type(event) is _AttemptPrepared:
            self._apply_attempt_prepared(event.attempt)
        elif type(event) is _GoDurable:
            self._apply_go(event)
        elif type(event) is _AttemptConfirmed:
            self._apply_confirmation(event)
        elif type(event) is _GenerationReaped:
            self._apply_reap(event)
        elif type(event) is _AttemptResolved:
            self._apply_resolution(event)
        else:
            raise ExecutionJournalError("JOURNAL_EVENT_TYPE")

    def _apply_generation_admitted(self, event: _GenerationAdmitted) -> None:
        if self.active_generation is not None:
            raise ExecutionJournalError("GENERATION_ACTIVE")
        if (
            not _is_positive_int(event.generation)
            or type(event.capability) is not GenerationCapability
        ):
            raise ExecutionJournalError("INVALID_GENERATION")
        if event.generation != self.last_generation + 1:
            raise ExecutionJournalError("GENERATION_SEQUENCE")
        if event.capability is GenerationCapability.RECOVERY and (
            event.generation == 1 or event.generation - 1 not in self.reaped_generations
        ):
            raise ExecutionJournalError("RECOVERY_REQUIRES_REAP")
        self.last_generation = event.generation
        self.active_generation = event.generation
        self.generation_capabilities[event.generation] = event.capability

    def _apply_attempt_prepared(self, attempt: MutationAttempt) -> None:
        if attempt.attempt_id in self.attempts:
            raise ExecutionJournalError("ATTEMPT_ALREADY_EXISTS")
        if self.active_generation != attempt.generation:
            raise ExecutionJournalError("ATTEMPT_GENERATION_NOT_ACTIVE")
        capability = self.generation_capabilities[attempt.generation]
        if capability is GenerationCapability.RECOVERY:
            if attempt.kind is not MutationKind.CANCEL:
                raise ExecutionJournalError("RECOVERY_MUTATION_FORBIDDEN")
            source_id = attempt.recovery_of_attempt_id
            source = self.attempts.get(source_id or "")
            source_frontier = self.frontiers.get(source_id or "")
            source_allows_cleanup = source is not None and (
                (
                    source.kind is MutationKind.CANCEL
                    and source_frontier is FrontierState.UNKNOWN
                )
                or (
                    source.kind is MutationKind.CREATE
                    and source_frontier in {FrontierState.UNKNOWN, FrontierState.CONFIRMED}
                )
            )
            if source_id is None or not source_allows_cleanup:
                raise ExecutionJournalError("RECOVERY_CANCEL_NOT_AUTHORIZED")
        elif attempt.recovery_of_attempt_id is not None:
            raise ExecutionJournalError("RECOVERY_LINK_FORBIDDEN")
        self.attempts[attempt.attempt_id] = attempt
        self.frontiers[attempt.attempt_id] = FrontierState.PREPARED

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

    def _apply_reap(self, event: _GenerationReaped) -> None:
        if self.active_generation != event.generation:
            raise ExecutionJournalError("REAP_GENERATION_MISMATCH")
        self.reaped_generations.add(event.generation)
        self.active_generation = None

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

    def records(self) -> tuple[JournalRecord, ...]:
        with self._locked_existing_fd() as fd:
            return _read_records(fd)

    def admit_generation(
        self,
        generation: int,
        capability: GenerationCapability,
    ) -> JournalRecord:
        return self._append_event(
            lambda _state: _GenerationAdmitted(
                generation=generation,
                capability=capability,
            )
        )

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

    def reap_generation(self, generation: int) -> JournalRecord:
        return self._append_event(lambda _state: _GenerationReaped(generation))

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
            state = _state_from_records(_read_records(fd))
            state._find_attempt(attempt_id)
            return state.frontiers[attempt_id]

    def recovery_directive(self, attempt_id: str) -> RecoveryDirective:
        with self._locked_existing_fd() as fd:
            state = _state_from_records(_read_records(fd))
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
            )

    def _append_event(
        self,
        event_factory: Callable[[_JournalState], _JournalEvent],
    ) -> JournalRecord:
        with self._locked_existing_fd() as fd:
            records = _read_records(fd)
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
                    _read_records(existing_fd)
                    return
            except OSError as exc:
                raise ExecutionJournalError("JOURNAL_CREATE_FAILED") from exc
            try:
                os.fchmod(fd, 0o600)
                _validate_file_stat(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX)
                _record, encoded = _build_record(
                    sequence=1,
                    previous_digest=ZERO_DIGEST,
                    event=_JournalCreated(),
                )
                _append_and_fsync(fd, encoded)
                try:
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise ExecutionJournalError("JOURNAL_DIRECTORY_FSYNC_FAILED") from exc
            finally:
                os.close(fd)
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
