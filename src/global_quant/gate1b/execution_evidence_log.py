"""Durable sanitized request evidence for the Gate 1B execution boundary.

``requests.jsonl`` is deliberately not a mutation authority.  Every retained
record is projected from the canonical :class:`ExecutionJournal`, and every
terminal mutation result is additionally bound to the journaled dispatch-result
digest.  Request parameters, response bodies, credentials, and signed request
material are never retained here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    JournalRecord,
    MutationAttempt,
    MutationKind,
    MutationReservationProof,
    ReconciliationKeyKind,
)
from global_quant.gate1b.execution_kernel import DispatchKernelError, DispatchResult
from global_quant.gate1b.mutation_protocol import ReservedRequest

SCHEMA_VERSION = "gate1b.execution-evidence.v1"
HEAD_SCHEMA_VERSION = "gate1b.execution-evidence-head.v1"
ZERO_DIGEST = "0" * 64
MAX_RECORD_BYTES = 8_192

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "authorizationid",
        "body",
        "credential",
        "credentials",
        "header",
        "headers",
        "rawbody",
        "requestbody",
        "requestheaders",
        "secret",
        "signature",
        "signedheaders",
        "signedmaterial",
        "signedrequest",
        "signedurl",
        "url",
        "xmbxapikey",
    }
)
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_digest",
        "digest",
        "kind",
        "attempt_id",
        "mutation_kind",
        "client_id",
        "reconciliation_key_kind",
        "reconciliation_client_id",
        "request_sha256",
        "logical_request_sha256",
        "request_sequence",
        "deadline_ns",
        "generation",
        "retry_index",
        "reservation_sha256",
        "reservation_proof_sha256",
        "frontier",
        "journal_reservation_sequence",
        "journal_reservation_digest",
        "journal_proof_sequence",
        "journal_proof_digest",
        "journal_attempt_sequence",
        "journal_attempt_digest",
        "journal_go_sequence",
        "journal_go_digest",
        "journal_terminal_sequence",
        "journal_terminal_digest",
        "journal_head_sequence",
        "journal_head_digest",
        "outcome",
    }
)
_RESULT_OUTCOME_FIELDS = frozenset(
    {
        "type",
        "status",
        "client_order_id",
        "order_id_sha256",
        "transport_result_sha256",
        "dispatch_result_sha256",
    }
)
_FAILURE_OUTCOME_FIELDS = frozenset({"type", "boundary_result"})
_HEAD_FIELDS = frozenset({"schema_version", "record_sequence", "record_digest"})


class ExecutionEvidenceLogError(RuntimeError):
    """Raised when request evidence cannot be durably verified."""


class EvidenceRecordKind(StrEnum):
    """Fixed record types retained by ``requests.jsonl``."""

    PREPARED = "PREPARED"
    RESULT = "RESULT"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class SanitizedMutationResult:
    """Allowlisted mutation acknowledgement; never a raw venue response."""

    status: str
    client_order_id: str
    order_id_sha256: str | None
    transport_result_sha256: str
    dispatch_result_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.status) is not str
            or not self.status
            or type(self.client_order_id) is not str
            or not self.client_order_id
            or (self.order_id_sha256 is not None and not _is_sha256(self.order_id_sha256))
            or not _is_sha256(self.transport_result_sha256)
            or not _is_sha256(self.dispatch_result_sha256)
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_OUTCOME")


@dataclass(frozen=True, slots=True)
class SanitizedMutationFailure:
    """Typed boundary failure derived only from ``ATTEMPT_RESOLVED``."""

    boundary_result: BoundaryResult

    def __post_init__(self) -> None:
        if type(self.boundary_result) is not BoundaryResult:
            raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_OUTCOME")


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceRecord:
    """One typed record from the execution-evidence hash chain."""

    schema_version: str
    sequence: int
    previous_digest: str
    digest: str
    kind: EvidenceRecordKind
    attempt_id: str
    mutation_kind: MutationKind
    client_id: str
    reconciliation_key_kind: ReconciliationKeyKind
    reconciliation_client_id: str
    request_sha256: str
    logical_request_sha256: str
    request_sequence: int
    deadline_ns: int
    generation: int
    retry_index: int
    reservation_sha256: str
    reservation_proof_sha256: str
    frontier: FrontierState
    journal_reservation_sequence: int
    journal_reservation_digest: str
    journal_proof_sequence: int
    journal_proof_digest: str
    journal_attempt_sequence: int
    journal_attempt_digest: str
    journal_go_sequence: int | None
    journal_go_digest: str | None
    journal_terminal_sequence: int | None
    journal_terminal_digest: str | None
    journal_head_sequence: int
    journal_head_digest: str
    outcome: SanitizedMutationResult | SanitizedMutationFailure | None


@dataclass(frozen=True, slots=True)
class _JournalBinding:
    attempt: MutationAttempt
    reserved_request: ReservedRequest
    reservation_proof: MutationReservationProof
    reservation_record: JournalRecord
    proof_record: JournalRecord
    attempt_record: JournalRecord
    go_record: JournalRecord | None
    confirmed_record: JournalRecord | None
    resolved_record: JournalRecord | None

    @property
    def frontier(self) -> FrontierState:
        if self.confirmed_record is not None:
            return FrontierState.CONFIRMED
        if self.resolved_record is not None:
            return self.resolved_record.event.state  # type: ignore[attr-defined, no-any-return]
        if self.go_record is not None:
            return FrontierState.GO_DURABLE
        return FrontierState.PREPARED


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED")


def _exact_mapping(value: object, expected: frozenset[str], reason: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != expected:
        raise ExecutionEvidenceLogError(reason)
    return value


def _enum(enum_type: type[StrEnum], value: object, reason: str) -> StrEnum:
    if type(value) is not str:
        raise ExecutionEvidenceLogError(reason)
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ExecutionEvidenceLogError(reason) from exc


def _optional_positive_int(value: object, reason: str) -> int | None:
    if value is None:
        return None
    if not _positive_int(value):
        raise ExecutionEvidenceLogError(reason)
    return value


def _optional_sha256(value: object, reason: str) -> str | None:
    if value is None:
        return None
    if not _is_sha256(value):
        raise ExecutionEvidenceLogError(reason)
    return value


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive OS contract
            raise ExecutionEvidenceLogError("EVIDENCE_WRITE_FAILED")
        view = view[written:]


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _event_name(record: JournalRecord) -> str:
    return type(record.event).__name__


def _record_at(records: tuple[JournalRecord, ...], sequence: int) -> JournalRecord:
    if not _positive_int(sequence) or sequence > len(records):
        raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_RECORD_MISMATCH")
    return records[sequence - 1]


class ExecutionEvidenceLog:
    """Owner-only append log whose records are projections of one journal."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        execution_journal_path: str | os.PathLike[str],
        credential_canaries: Iterable[str | bytes] = (),
    ) -> None:
        evidence_path = Path(path).absolute()
        journal_path = Path(execution_journal_path).absolute()
        if (
            evidence_path.name != "requests.jsonl"
            or ".." in Path(path).parts
            or not evidence_path.parent.name
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_PATH")
        if evidence_path.parent != journal_path.parent or evidence_path == journal_path:
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_COLOCATION")
        self._path = evidence_path
        self._journal_path = journal_path
        self._canaries = self._normalize_canaries(credential_canaries)
        self._validate_directory()
        self._validate_journal_path()
        try:
            self._journal = ExecutionJournal(self._journal_path)
        except ExecutionJournalError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_EXECUTION_JOURNAL_INVALID") from exc
        created = self._ensure_log_file()
        with self._locked_fd() as fd:
            existing = self._read_validated_records(fd)
        if not created:
            self._validate_journal_projection(existing, self._journal_records())

    @property
    def path(self) -> Path:
        return self._path

    @property
    def head_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.head")

    @property
    def execution_journal_path(self) -> Path:
        return self._journal_path

    def append_prepared(self, attempt_id: str) -> ExecutionEvidenceRecord:
        """Append PREPARED only while the canonical frontier is PREPARED."""

        return self._append(
            kind=EvidenceRecordKind.PREPARED,
            attempt_id=attempt_id,
            dispatch_result=None,
        )

    def append_result(self, result: DispatchResult) -> ExecutionEvidenceRecord:
        """Append a sanitized RESULT whose full digest is journal-confirmed."""

        if type(result) is not DispatchResult:
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_TYPE")
        return self._append(
            kind=EvidenceRecordKind.RESULT,
            attempt_id=result.attempt_id,
            dispatch_result=result,
        )

    def append_failure(self, attempt_id: str) -> ExecutionEvidenceRecord:
        """Append FAILURE derived from the exact durable resolution record."""

        return self._append(
            kind=EvidenceRecordKind.FAILURE,
            attempt_id=attempt_id,
            dispatch_result=None,
        )

    def replay(self) -> tuple[ExecutionEvidenceRecord, ...]:
        """Verify chain, head anchor, and complete canonical journal projection."""

        with self._locked_fd() as fd:
            records = self._read_validated_records(fd)
        self._validate_journal_projection(records, self._journal_records())
        return records

    @staticmethod
    def _normalize_canaries(values: Iterable[str | bytes]) -> tuple[bytes, ...]:
        result: list[bytes] = []
        try:
            iterator = iter(values)
        except TypeError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_CANARY_INVALID") from exc
        for value in iterator:
            if type(value) is str:
                try:
                    encoded = value.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ExecutionEvidenceLogError("EVIDENCE_CANARY_INVALID") from exc
            elif type(value) is bytes:
                encoded = value
            else:
                raise ExecutionEvidenceLogError("EVIDENCE_CANARY_INVALID")
            if not encoded:
                raise ExecutionEvidenceLogError("EVIDENCE_CANARY_INVALID")
            result.append(encoded)
        return tuple(result)

    def _validate_directory(self) -> None:
        try:
            info = self._path.parent.lstat()
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_DIRECTORY") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or _mode(info) != 0o700
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_DIRECTORY_MODE")

    def _validate_journal_path(self) -> None:
        try:
            info = self._journal_path.lstat()
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_EXECUTION_JOURNAL_REQUIRED") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or _mode(info) != 0o600
            or info.st_nlink != 1
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_EXECUTION_JOURNAL_MODE")

    def _ensure_log_file(self) -> bool:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_CREATE_FAILED") from exc
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory()
        return True

    @contextmanager
    def _locked_fd(self):
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags)
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_OPEN_FAILED") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or _mode(info) != 0o600
                or info.st_nlink != 1
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_FILE_MODE")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _journal_records(self) -> tuple[JournalRecord, ...]:
        try:
            return self._journal.records()
        except ExecutionJournalError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_EXECUTION_JOURNAL_INVALID") from exc

    def _append(
        self,
        *,
        kind: EvidenceRecordKind,
        attempt_id: str,
        dispatch_result: DispatchResult | None,
    ) -> ExecutionEvidenceRecord:
        if not _is_sha256(attempt_id):
            raise ExecutionEvidenceLogError("EVIDENCE_ATTEMPT_ID")
        with self._locked_fd() as fd:
            existing = self._read_validated_records(fd)
            journal_records = self._journal_records()
            self._validate_journal_projection(
                existing,
                journal_records,
                allowed_missing=frozenset({(attempt_id, kind)}),
            )
            binding = self._binding_for(journal_records, attempt_id)
            record = self._build_record(
                existing=existing,
                journal_records=journal_records,
                binding=binding,
                kind=kind,
                dispatch_result=dispatch_result,
            )
            self._assert_no_sensitive_material(_record_to_mapping(record))
            encoded = _canonical(_record_to_mapping(record)) + b"\n"
            if len(encoded) > MAX_RECORD_BYTES:
                raise ExecutionEvidenceLogError("EVIDENCE_RECORD_OVERSIZED")
            os.lseek(fd, 0, os.SEEK_END)
            _write_all(fd, encoded)
            os.fsync(fd)
            self._write_head(record)
            return record

    def _build_record(
        self,
        *,
        existing: tuple[ExecutionEvidenceRecord, ...],
        journal_records: tuple[JournalRecord, ...],
        binding: _JournalBinding,
        kind: EvidenceRecordKind,
        dispatch_result: DispatchResult | None,
    ) -> ExecutionEvidenceRecord:
        if any(
            record.attempt_id == binding.attempt.attempt_id and record.kind is kind
            for record in existing
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_DUPLICATE_RECORD")
        frontier = binding.frontier
        outcome: SanitizedMutationResult | SanitizedMutationFailure | None
        terminal: JournalRecord | None
        if kind is EvidenceRecordKind.PREPARED:
            if frontier is not FrontierState.PREPARED or dispatch_result is not None:
                raise ExecutionEvidenceLogError("EVIDENCE_PREPARED_FRONTIER")
            outcome = None
            terminal = None
        elif kind is EvidenceRecordKind.RESULT:
            if (
                frontier is not FrontierState.CONFIRMED
                or binding.confirmed_record is None
                or dispatch_result is None
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_RESULT_FRONTIER")
            outcome = self._validated_result(binding, dispatch_result)
            terminal = binding.confirmed_record
        else:
            if (
                frontier not in {FrontierState.NOT_DISPATCHED, FrontierState.UNKNOWN}
                or binding.resolved_record is None
                or dispatch_result is not None
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_FRONTIER")
            outcome = SanitizedMutationFailure(
                boundary_result=binding.resolved_record.event.boundary_result  # type: ignore[attr-defined]
            )
            terminal = binding.resolved_record
        sequence = len(existing) + 1
        previous_digest = existing[-1].digest if existing else ZERO_DIGEST
        head = journal_records[-1]
        body = self._record_body(
            sequence=sequence,
            previous_digest=previous_digest,
            kind=kind,
            binding=binding,
            frontier=frontier,
            terminal=terminal,
            journal_head=head,
            outcome=outcome,
        )
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        return _record_from_body(body, digest=digest)

    @staticmethod
    def _record_body(
        *,
        sequence: int,
        previous_digest: str,
        kind: EvidenceRecordKind,
        binding: _JournalBinding,
        frontier: FrontierState,
        terminal: JournalRecord | None,
        journal_head: JournalRecord,
        outcome: SanitizedMutationResult | SanitizedMutationFailure | None,
    ) -> dict[str, object]:
        attempt = binding.attempt
        request = binding.reserved_request
        proof = binding.reservation_proof
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "previous_digest": previous_digest,
            "kind": kind.value,
            "attempt_id": attempt.attempt_id,
            "mutation_kind": attempt.kind.value,
            "client_id": attempt.client_id,
            "reconciliation_key_kind": attempt.reconciliation_key.kind.value,
            "reconciliation_client_id": attempt.reconciliation_key.client_id,
            "request_sha256": request.request_sha256,
            "logical_request_sha256": request.logical_request_sha256,
            "request_sequence": request.ledger.total_http_requests,
            "deadline_ns": attempt.deadline_ns,
            "generation": attempt.generation,
            "retry_index": attempt.retry_index,
            "reservation_sha256": attempt.reservation_sha256,
            "reservation_proof_sha256": proof.proof_sha256,
            "frontier": frontier.value,
            "journal_reservation_sequence": binding.reservation_record.sequence,
            "journal_reservation_digest": binding.reservation_record.digest,
            "journal_proof_sequence": binding.proof_record.sequence,
            "journal_proof_digest": binding.proof_record.digest,
            "journal_attempt_sequence": binding.attempt_record.sequence,
            "journal_attempt_digest": binding.attempt_record.digest,
            "journal_go_sequence": (
                binding.go_record.sequence if binding.go_record is not None else None
            ),
            "journal_go_digest": (
                binding.go_record.digest if binding.go_record is not None else None
            ),
            "journal_terminal_sequence": terminal.sequence if terminal is not None else None,
            "journal_terminal_digest": terminal.digest if terminal is not None else None,
            "journal_head_sequence": journal_head.sequence,
            "journal_head_digest": journal_head.digest,
            "outcome": _outcome_to_mapping(outcome),
        }

    @staticmethod
    def _validated_result(
        binding: _JournalBinding,
        result: DispatchResult,
    ) -> SanitizedMutationResult:
        attempt = binding.attempt
        request = binding.reserved_request
        transport = result.transport_result
        try:
            client_id = transport.field("clientOrderId")
            status = transport.field("status")
        except KeyError as exc:  # pragma: no cover - DispatchResult validates fixed fields
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_OUTCOME") from exc
        try:
            order_id_sha256 = transport.field("orderIdSha256")
        except KeyError:
            order_id_sha256 = None
        if (
            result.attempt_id != attempt.attempt_id
            or result.generation != attempt.generation
            or result.kind is not attempt.kind
            or result.client_id != attempt.client_id
            or transport.kind is not ResponseKind.MUTATION_ACK
            or transport.request_sha256 != request.request_sha256
            or transport.logical_request_sha256 != request.logical_request_sha256
            or client_id != attempt.client_id
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_REQUEST_BINDING_MISMATCH")
        confirmed = binding.confirmed_record
        if (
            confirmed is None or confirmed.event.result_sha256 != result.digest  # type: ignore[attr-defined]
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_JOURNAL_MISMATCH")
        return SanitizedMutationResult(
            status=status,  # type: ignore[arg-type]
            client_order_id=client_id,  # type: ignore[arg-type]
            order_id_sha256=order_id_sha256,  # type: ignore[arg-type]
            transport_result_sha256=transport.result_sha256,
            dispatch_result_sha256=result.digest,
        )

    @staticmethod
    def _binding_for(
        records: tuple[JournalRecord, ...],
        attempt_id: str,
    ) -> _JournalBinding:
        attempt_records = [
            record
            for record in records
            if _event_name(record) == "_AttemptPrepared"
            and record.event.attempt.attempt_id == attempt_id  # type: ignore[attr-defined]
        ]
        if len(attempt_records) != 1:
            raise ExecutionEvidenceLogError("EVIDENCE_ATTEMPT_JOURNAL_MISMATCH")
        attempt_record = attempt_records[0]
        attempt = attempt_record.event.attempt  # type: ignore[attr-defined]
        reservation_records = [
            record
            for record in records
            if _event_name(record) == "_ExactRequestReserved"
            and record.event.reserved_request.request_sha256 == attempt.reservation_sha256  # type: ignore[attr-defined]
        ]
        proof_records = [
            record
            for record in records
            if _event_name(record) == "_MutationReserved"
            and record.event.proof.request_sha256 == attempt.reservation_sha256  # type: ignore[attr-defined]
        ]
        go_records = [
            record
            for record in records
            if _event_name(record) == "_GoDurable" and record.event.attempt_id == attempt_id  # type: ignore[attr-defined]
        ]
        confirmed_records = [
            record
            for record in records
            if _event_name(record) == "_AttemptConfirmed" and record.event.attempt_id == attempt_id  # type: ignore[attr-defined]
        ]
        resolved_records = [
            record
            for record in records
            if _event_name(record) == "_AttemptResolved" and record.event.attempt_id == attempt_id  # type: ignore[attr-defined]
        ]
        if (
            len(reservation_records) != 1
            or len(proof_records) != 1
            or len(go_records) > 1
            or len(confirmed_records) > 1
            or len(resolved_records) > 1
            or (confirmed_records and resolved_records)
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_BINDING_MISMATCH")
        reservation_record = reservation_records[0]
        proof_record = proof_records[0]
        reserved = reservation_record.event.reserved_request  # type: ignore[attr-defined]
        proof = proof_record.event.proof  # type: ignore[attr-defined]
        try:
            proof.validate_dispatch_binding(reserved, attempt)
        except ExecutionJournalError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_BINDING_MISMATCH") from exc
        exact_event = reservation_record.event
        if (
            exact_event.generation != attempt.generation  # type: ignore[attr-defined]
            or exact_event.deadline_ns != attempt.deadline_ns  # type: ignore[attr-defined]
            or proof.generation != attempt.generation
            or proof.deadline_ns != attempt.deadline_ns
            or attempt.reservation_sha256 != reserved.request_sha256
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_BINDING_MISMATCH")
        return _JournalBinding(
            attempt=attempt,
            reserved_request=reserved,
            reservation_proof=proof,
            reservation_record=reservation_record,
            proof_record=proof_record,
            attempt_record=attempt_record,
            go_record=go_records[0] if go_records else None,
            confirmed_record=confirmed_records[0] if confirmed_records else None,
            resolved_record=resolved_records[0] if resolved_records else None,
        )

    def _validate_journal_projection(
        self,
        evidence: tuple[ExecutionEvidenceRecord, ...],
        journal_records: tuple[JournalRecord, ...],
        *,
        allowed_missing: frozenset[tuple[str, EvidenceRecordKind]] = frozenset(),
    ) -> None:
        attempts = tuple(
            record.event.attempt.attempt_id  # type: ignore[attr-defined]
            for record in journal_records
            if _event_name(record) == "_AttemptPrepared"
        )
        grouped: dict[str, list[ExecutionEvidenceRecord]] = {}
        for record in evidence:
            binding = self._binding_for(journal_records, record.attempt_id)
            self._validate_record_projection(record, binding, journal_records)
            grouped.setdefault(record.attempt_id, []).append(record)
        for attempt_id in attempts:
            binding = self._binding_for(journal_records, attempt_id)
            records = grouped.get(attempt_id, [])
            prepared = [record for record in records if record.kind is EvidenceRecordKind.PREPARED]
            results = [record for record in records if record.kind is EvidenceRecordKind.RESULT]
            failures = [record for record in records if record.kind is EvidenceRecordKind.FAILURE]
            if (
                len(prepared) != 1
                and (
                    attempt_id,
                    EvidenceRecordKind.PREPARED,
                )
                not in allowed_missing
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_INCOMPLETE")
            if len(prepared) > 1 or len(results) > 1 or len(failures) > 1 or (results and failures):
                raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_DUPLICATE")
            expected_terminal: EvidenceRecordKind | None = None
            if binding.frontier is FrontierState.CONFIRMED:
                expected_terminal = EvidenceRecordKind.RESULT
            elif binding.frontier in {FrontierState.NOT_DISPATCHED, FrontierState.UNKNOWN}:
                expected_terminal = EvidenceRecordKind.FAILURE
            terminal_records = (
                results if expected_terminal is EvidenceRecordKind.RESULT else failures
            )
            if (
                expected_terminal is not None
                and len(terminal_records) != 1
                and (
                    attempt_id,
                    expected_terminal,
                )
                not in allowed_missing
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_INCOMPLETE")
            if expected_terminal is None and (results or failures):
                raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_MISMATCH")
            if (
                prepared
                and terminal_records
                and prepared[0].sequence >= terminal_records[0].sequence
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_ORDER")
        if frozenset(grouped).difference(attempts):  # pragma: no cover - binding already rejects
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_MISMATCH")

    def _validate_record_projection(
        self,
        record: ExecutionEvidenceRecord,
        binding: _JournalBinding,
        journal_records: tuple[JournalRecord, ...],
    ) -> None:
        attempt = binding.attempt
        request = binding.reserved_request
        proof = binding.reservation_proof
        static_matches = (
            record.attempt_id == attempt.attempt_id
            and record.mutation_kind is attempt.kind
            and record.client_id == attempt.client_id
            and record.reconciliation_key_kind is attempt.reconciliation_key.kind
            and record.reconciliation_client_id == attempt.reconciliation_key.client_id
            and record.request_sha256 == request.request_sha256
            and record.logical_request_sha256 == request.logical_request_sha256
            and record.request_sequence == request.ledger.total_http_requests
            and record.deadline_ns == attempt.deadline_ns
            and record.generation == attempt.generation
            and record.retry_index == attempt.retry_index
            and record.reservation_sha256 == attempt.reservation_sha256
            and record.reservation_proof_sha256 == proof.proof_sha256
            and record.journal_reservation_sequence == binding.reservation_record.sequence
            and record.journal_reservation_digest == binding.reservation_record.digest
            and record.journal_proof_sequence == binding.proof_record.sequence
            and record.journal_proof_digest == binding.proof_record.digest
            and record.journal_attempt_sequence == binding.attempt_record.sequence
            and record.journal_attempt_digest == binding.attempt_record.digest
        )
        if not static_matches:
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_PROJECTION_MISMATCH")
        head = _record_at(journal_records, record.journal_head_sequence)
        if (
            head.digest != record.journal_head_digest
            or record.journal_head_sequence < record.journal_attempt_sequence
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_JOURNAL_HEAD_BINDING_MISMATCH")
        if record.kind is EvidenceRecordKind.PREPARED:
            if (
                record.frontier is not FrontierState.PREPARED
                or record.outcome is not None
                or record.journal_go_sequence is not None
                or record.journal_go_digest is not None
                or record.journal_terminal_sequence is not None
                or record.journal_terminal_digest is not None
                or (
                    binding.go_record is not None
                    and binding.go_record.sequence <= record.journal_head_sequence
                )
                or (
                    binding.confirmed_record is not None
                    and binding.confirmed_record.sequence <= record.journal_head_sequence
                )
                or (
                    binding.resolved_record is not None
                    and binding.resolved_record.sequence <= record.journal_head_sequence
                )
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_PREPARED_PROJECTION_MISMATCH")
            return
        if record.kind is EvidenceRecordKind.RESULT:
            if (
                record.frontier is not FrontierState.CONFIRMED
                or binding.go_record is None
                or binding.confirmed_record is None
                or not isinstance(record.outcome, SanitizedMutationResult)
                or not self._matches_record(record, binding.go_record, terminal=False)
                or not self._matches_record(record, binding.confirmed_record, terminal=True)
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_RESULT_PROJECTION_MISMATCH")
            fields: tuple[tuple[str, object], ...] = (
                ("clientOrderId", record.outcome.client_order_id),
                *(
                    (("orderIdSha256", record.outcome.order_id_sha256),)
                    if record.outcome.order_id_sha256 is not None
                    else ()
                ),
                ("status", record.outcome.status),
            )
            try:
                transport = TransportResult.build(
                    request_sha256=record.request_sha256,
                    logical_request_sha256=record.logical_request_sha256,
                    kind=ResponseKind.MUTATION_ACK,
                    fields=tuple(sorted(fields)),
                )
                result = DispatchResult.build(binding.attempt, transport_result=transport)
            except (DispatchKernelError, ValueError) as exc:
                raise ExecutionEvidenceLogError("EVIDENCE_RESULT_PROJECTION_MISMATCH") from exc
            if (
                transport.result_sha256 != record.outcome.transport_result_sha256
                or result.digest != record.outcome.dispatch_result_sha256
                or binding.confirmed_record.event.result_sha256 != result.digest  # type: ignore[attr-defined]
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_RESULT_PROJECTION_MISMATCH")
            return
        if (
            record.frontier not in {FrontierState.NOT_DISPATCHED, FrontierState.UNKNOWN}
            or binding.resolved_record is None
            or not isinstance(record.outcome, SanitizedMutationFailure)
            or not self._matches_record(record, binding.resolved_record, terminal=True)
            or record.outcome.boundary_result is not binding.resolved_record.event.boundary_result  # type: ignore[attr-defined]
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_PROJECTION_MISMATCH")
        if record.frontier is FrontierState.UNKNOWN:
            if binding.go_record is None or not self._matches_record(
                record,
                binding.go_record,
                terminal=False,
            ):
                raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_PROJECTION_MISMATCH")
        elif record.journal_go_sequence is not None or record.journal_go_digest is not None:
            raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_PROJECTION_MISMATCH")

    @staticmethod
    def _matches_record(
        evidence: ExecutionEvidenceRecord,
        journal: JournalRecord,
        *,
        terminal: bool,
    ) -> bool:
        if terminal:
            return (
                evidence.journal_terminal_sequence == journal.sequence
                and evidence.journal_terminal_digest == journal.digest
                and evidence.journal_head_sequence >= journal.sequence
            )
        return (
            evidence.journal_go_sequence == journal.sequence
            and evidence.journal_go_digest == journal.digest
            and evidence.journal_head_sequence >= journal.sequence
        )

    def _read_validated_records(self, fd: int) -> tuple[ExecutionEvidenceRecord, ...]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        if payload and not payload.endswith(b"\n"):
            raise ExecutionEvidenceLogError("EVIDENCE_TORN_RECORD")
        records: list[ExecutionEvidenceRecord] = []
        previous_digest = ZERO_DIGEST
        for sequence, raw in enumerate(payload.splitlines(keepends=True), start=1):
            if len(raw) > MAX_RECORD_BYTES:
                raise ExecutionEvidenceLogError("EVIDENCE_RECORD_OVERSIZED")
            record = self._decode_record(
                raw,
                expected_sequence=sequence,
                expected_previous_digest=previous_digest,
            )
            records.append(record)
            previous_digest = record.digest
        self._validate_head(tuple(records))
        return tuple(records)

    def _decode_record(
        self,
        raw: bytes,
        *,
        expected_sequence: int,
        expected_previous_digest: str,
    ) -> ExecutionEvidenceRecord:
        try:
            value = json.loads(
                raw[:-1].decode("ascii"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except ExecutionEvidenceLogError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED") from exc
        self._assert_no_sensitive_material(value)
        item = _exact_mapping(value, _RECORD_FIELDS, "EVIDENCE_RECORD_FIELDS")
        if raw != _canonical(value) + b"\n":
            raise ExecutionEvidenceLogError("EVIDENCE_NONCANONICAL")
        if item["schema_version"] != SCHEMA_VERSION:
            raise ExecutionEvidenceLogError("EVIDENCE_SCHEMA_VERSION")
        if item["sequence"] != expected_sequence:
            raise ExecutionEvidenceLogError("EVIDENCE_SEQUENCE")
        if item["previous_digest"] != expected_previous_digest:
            raise ExecutionEvidenceLogError("EVIDENCE_PREVIOUS_DIGEST")
        if not _is_sha256(item["digest"]):
            raise ExecutionEvidenceLogError("EVIDENCE_DIGEST")
        body = {key: field_value for key, field_value in item.items() if key != "digest"}
        expected_digest = hashlib.sha256(_canonical(body)).hexdigest()
        if item["digest"] != expected_digest:
            raise ExecutionEvidenceLogError("EVIDENCE_DIGEST")
        return _record_from_body(body, digest=item["digest"])  # type: ignore[arg-type]

    def _validate_head(self, records: tuple[ExecutionEvidenceRecord, ...]) -> None:
        try:
            info = self.head_path.lstat()
        except FileNotFoundError:
            if records:
                raise ExecutionEvidenceLogError("EVIDENCE_HEAD_MISSING") from None
            return
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_INVALID") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or _mode(info) != 0o600
            or info.st_nlink != 1
        ):
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_MODE")
        try:
            raw = self.head_path.read_bytes()
            value = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except ExecutionEvidenceLogError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_INVALID") from exc
        self._assert_no_sensitive_material(value)
        item = _exact_mapping(value, _HEAD_FIELDS, "EVIDENCE_HEAD_FIELDS")
        if raw != _canonical(value) + b"\n" or item["schema_version"] != HEAD_SCHEMA_VERSION:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_INVALID")
        if not records:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_MISMATCH")
        tail = records[-1]
        if item["record_sequence"] != tail.sequence or item["record_digest"] != tail.digest:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_MISMATCH")

    def _write_head(self, record: ExecutionEvidenceRecord) -> None:
        wire = (
            _canonical(
                {
                    "schema_version": HEAD_SCHEMA_VERSION,
                    "record_sequence": record.sequence,
                    "record_digest": record.digest,
                }
            )
            + b"\n"
        )
        temporary = self.head_path.with_name(
            f".{self.head_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(temporary, flags, 0o600)
            os.fchmod(fd, 0o600)
            _write_all(fd, wire)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.head_path)
            self._fsync_directory()
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_HEAD_WRITE_FAILED") from exc
        finally:
            if fd is not None:
                os.close(fd)
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(self._path.parent, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ExecutionEvidenceLogError("EVIDENCE_DIRECTORY_FSYNC_FAILED") from exc

    def _assert_no_sensitive_material(self, value: object) -> None:
        if type(value) is dict:
            for key, nested in value.items():
                if type(key) is not str:
                    raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _SENSITIVE_KEYS:
                    raise ExecutionEvidenceLogError("EVIDENCE_SENSITIVE_MATERIAL")
                self._assert_no_sensitive_material(nested)
            return
        if type(value) in {list, tuple}:
            for nested in value:
                self._assert_no_sensitive_material(nested)
            return
        if type(value) is str:
            encoded = value.encode("utf-8")
            if any(canary in encoded for canary in self._canaries):
                raise ExecutionEvidenceLogError("EVIDENCE_SENSITIVE_MATERIAL")
            return
        if value is None or type(value) in {bool, int}:
            return
        raise ExecutionEvidenceLogError("EVIDENCE_MALFORMED")


def _outcome_to_mapping(
    outcome: SanitizedMutationResult | SanitizedMutationFailure | None,
) -> dict[str, object] | None:
    if outcome is None:
        return None
    if type(outcome) is SanitizedMutationResult:
        return {
            "type": "RESULT",
            "status": outcome.status,
            "client_order_id": outcome.client_order_id,
            "order_id_sha256": outcome.order_id_sha256,
            "transport_result_sha256": outcome.transport_result_sha256,
            "dispatch_result_sha256": outcome.dispatch_result_sha256,
        }
    if type(outcome) is SanitizedMutationFailure:
        return {
            "type": "FAILURE",
            "boundary_result": outcome.boundary_result.value,
        }
    raise ExecutionEvidenceLogError("EVIDENCE_OUTCOME")


def _outcome_from_mapping(
    value: object,
    kind: EvidenceRecordKind,
) -> SanitizedMutationResult | SanitizedMutationFailure | None:
    if kind is EvidenceRecordKind.PREPARED:
        if value is not None:
            raise ExecutionEvidenceLogError("EVIDENCE_PREPARED_OUTCOME")
        return None
    if kind is EvidenceRecordKind.RESULT:
        item = _exact_mapping(value, _RESULT_OUTCOME_FIELDS, "EVIDENCE_RESULT_OUTCOME")
        if item["type"] != "RESULT":
            raise ExecutionEvidenceLogError("EVIDENCE_RESULT_OUTCOME")
        return SanitizedMutationResult(
            status=item["status"],  # type: ignore[arg-type]
            client_order_id=item["client_order_id"],  # type: ignore[arg-type]
            order_id_sha256=item["order_id_sha256"],  # type: ignore[arg-type]
            transport_result_sha256=item["transport_result_sha256"],  # type: ignore[arg-type]
            dispatch_result_sha256=item["dispatch_result_sha256"],  # type: ignore[arg-type]
        )
    item = _exact_mapping(value, _FAILURE_OUTCOME_FIELDS, "EVIDENCE_FAILURE_OUTCOME")
    if item["type"] != "FAILURE":
        raise ExecutionEvidenceLogError("EVIDENCE_FAILURE_OUTCOME")
    return SanitizedMutationFailure(
        boundary_result=_enum(
            BoundaryResult,
            item["boundary_result"],
            "EVIDENCE_FAILURE_OUTCOME",
        ),  # type: ignore[arg-type]
    )


def _record_to_mapping(record: ExecutionEvidenceRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "sequence": record.sequence,
        "previous_digest": record.previous_digest,
        "digest": record.digest,
        "kind": record.kind.value,
        "attempt_id": record.attempt_id,
        "mutation_kind": record.mutation_kind.value,
        "client_id": record.client_id,
        "reconciliation_key_kind": record.reconciliation_key_kind.value,
        "reconciliation_client_id": record.reconciliation_client_id,
        "request_sha256": record.request_sha256,
        "logical_request_sha256": record.logical_request_sha256,
        "request_sequence": record.request_sequence,
        "deadline_ns": record.deadline_ns,
        "generation": record.generation,
        "retry_index": record.retry_index,
        "reservation_sha256": record.reservation_sha256,
        "reservation_proof_sha256": record.reservation_proof_sha256,
        "frontier": record.frontier.value,
        "journal_reservation_sequence": record.journal_reservation_sequence,
        "journal_reservation_digest": record.journal_reservation_digest,
        "journal_proof_sequence": record.journal_proof_sequence,
        "journal_proof_digest": record.journal_proof_digest,
        "journal_attempt_sequence": record.journal_attempt_sequence,
        "journal_attempt_digest": record.journal_attempt_digest,
        "journal_go_sequence": record.journal_go_sequence,
        "journal_go_digest": record.journal_go_digest,
        "journal_terminal_sequence": record.journal_terminal_sequence,
        "journal_terminal_digest": record.journal_terminal_digest,
        "journal_head_sequence": record.journal_head_sequence,
        "journal_head_digest": record.journal_head_digest,
        "outcome": _outcome_to_mapping(record.outcome),
    }


def _record_from_body(body: Mapping[str, Any], *, digest: str) -> ExecutionEvidenceRecord:
    try:
        kind = _enum(EvidenceRecordKind, body["kind"], "EVIDENCE_KIND")
        record = ExecutionEvidenceRecord(
            schema_version=body["schema_version"],
            sequence=body["sequence"],
            previous_digest=body["previous_digest"],
            digest=digest,
            kind=kind,
            attempt_id=body["attempt_id"],
            mutation_kind=_enum(MutationKind, body["mutation_kind"], "EVIDENCE_MUTATION_KIND"),
            client_id=body["client_id"],
            reconciliation_key_kind=_enum(
                ReconciliationKeyKind,
                body["reconciliation_key_kind"],
                "EVIDENCE_RECONCILIATION_KEY",
            ),
            reconciliation_client_id=body["reconciliation_client_id"],
            request_sha256=body["request_sha256"],
            logical_request_sha256=body["logical_request_sha256"],
            request_sequence=body["request_sequence"],
            deadline_ns=body["deadline_ns"],
            generation=body["generation"],
            retry_index=body["retry_index"],
            reservation_sha256=body["reservation_sha256"],
            reservation_proof_sha256=body["reservation_proof_sha256"],
            frontier=_enum(FrontierState, body["frontier"], "EVIDENCE_FRONTIER"),
            journal_reservation_sequence=body["journal_reservation_sequence"],
            journal_reservation_digest=body["journal_reservation_digest"],
            journal_proof_sequence=body["journal_proof_sequence"],
            journal_proof_digest=body["journal_proof_digest"],
            journal_attempt_sequence=body["journal_attempt_sequence"],
            journal_attempt_digest=body["journal_attempt_digest"],
            journal_go_sequence=_optional_positive_int(
                body["journal_go_sequence"],
                "EVIDENCE_JOURNAL_GO",
            ),
            journal_go_digest=_optional_sha256(
                body["journal_go_digest"],
                "EVIDENCE_JOURNAL_GO",
            ),
            journal_terminal_sequence=_optional_positive_int(
                body["journal_terminal_sequence"],
                "EVIDENCE_JOURNAL_TERMINAL",
            ),
            journal_terminal_digest=_optional_sha256(
                body["journal_terminal_digest"],
                "EVIDENCE_JOURNAL_TERMINAL",
            ),
            journal_head_sequence=body["journal_head_sequence"],
            journal_head_digest=body["journal_head_digest"],
            outcome=_outcome_from_mapping(body["outcome"], kind),
        )
    except KeyError as exc:
        raise ExecutionEvidenceLogError("EVIDENCE_RECORD_FIELDS") from exc
    if (
        record.schema_version != SCHEMA_VERSION
        or not _positive_int(record.sequence)
        or not _is_sha256(record.previous_digest)
        or not _is_sha256(record.digest)
        or not _is_sha256(record.attempt_id)
        or type(record.client_id) is not str
        or not record.client_id
        or type(record.reconciliation_client_id) is not str
        or not record.reconciliation_client_id
        or not _is_sha256(record.request_sha256)
        or not _is_sha256(record.logical_request_sha256)
        or not _positive_int(record.request_sequence)
        or not _positive_int(record.deadline_ns)
        or not _positive_int(record.generation)
        or not _nonnegative_int(record.retry_index)
        or not _is_sha256(record.reservation_sha256)
        or not _is_sha256(record.reservation_proof_sha256)
        or not _positive_int(record.journal_reservation_sequence)
        or not _is_sha256(record.journal_reservation_digest)
        or not _positive_int(record.journal_proof_sequence)
        or not _is_sha256(record.journal_proof_digest)
        or not _positive_int(record.journal_attempt_sequence)
        or not _is_sha256(record.journal_attempt_digest)
        or not _positive_int(record.journal_head_sequence)
        or not _is_sha256(record.journal_head_digest)
        or (record.journal_go_sequence is None) != (record.journal_go_digest is None)
        or (record.journal_terminal_sequence is None) != (record.journal_terminal_digest is None)
    ):
        raise ExecutionEvidenceLogError("EVIDENCE_RECORD_INVALID")
    return record
