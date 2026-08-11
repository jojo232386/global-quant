"""Credential-free durable projector for the Gate 1B execution loop.

The process supervisor owns time and child lifecycle.  This module owns the
mechanical economic projection between that supervisor and the immutable
lifecycle reducer.  Public methods accept only replayable domain objects; no
caller supplied truth flag, digest, sequence number, or response mapping can
advance the lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from global_quant.gate1b.credential_execution_session import ReadFailureResult
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.durable_intent import (
    PersistedIntent,
    load_persisted_intent,
    persist_intent,
)
from global_quant.gate1b.execution_evidence_log import ExecutionEvidenceLog
from global_quant.gate1b.execution_journal import (
    ExactReadFailure,
    ExecutionJournal,
    FrontierState,
    IntentBoundRecoveryAuthority,
    JournalRecord,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    OwnedFillCloseProof,
    PreIntentReadFailure,
    PreIntentReadReservation,
    PreIntentReadResult,
    ReadKind,
    ReadOutcome,
    ReadPurpose,
    ReadReservationProof,
    ReadResultProof,
    ReconciliationObservation,
    RecoverySessionAuthority,
    SessionAuthority,
)
from global_quant.gate1b.execution_kernel import DispatchResult
from global_quant.gate1b.execution_lifecycle import (
    CONTAINMENT_READ_STEPS,
    FINAL_READ_STEPS,
    PRE_INTENT_READ_STEPS,
    ActionKind,
    BudgetState,
    Capability,
    LifecycleState,
    LifecycleTiming,
    MutationDisposition,
    MutationResolution,
    PhasePermitProjection,
    PlannedAction,
    PrimaryJournalProjection,
    ReadDisposition,
    ReadResolution,
    RecoveryJournalProjection,
    RecoveryTarget,
    Step,
    plan_next,
)
from global_quant.gate1b.final_evidence import (
    EvidenceKind,
    FinalEvidenceBundle,
    FinalReadProvenance,
    MutationBarrier,
    PersistedPreflightProjection,
    PreflightEvidenceBundle,
    PreflightKind,
    PreflightProjection,
    PreIntentReadProvenance,
    ReplayedPreflightEvidence,
    final_evidence_bundle_sha256,
    load_preflight_evidence,
    persist_preflight_projection,
    project_final_evidence,
    project_preflight_evidence,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    MarketCloseFilters,
    MarketCloseProof,
    MutationLedger,
    OwnedPositionProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    build_emergency_client_order_id,
)
from global_quant.gate1b.process_boundary import (
    PhaseDeadlinePermit,
    ProcessLifecycleJournal,
)
from global_quant.gate1b.runtime_binding import RuntimeSnapshot


class ExecutionProjectionError(RuntimeError):
    """A live durable source cannot prove the requested projection."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ExactReadProjection:
    reserved_request: ReservedRequest
    reservation_proof: ReadReservationProof

    def __post_init__(self) -> None:
        if (
            type(self.reserved_request) is not ReservedRequest
            or type(self.reservation_proof) is not ReadReservationProof
        ):
            raise ExecutionProjectionError("EXACT_READ_PROJECTION_INVALID")
        try:
            self.reservation_proof.validate_reserved_request(self.reserved_request)
        except BaseException:
            raise ExecutionProjectionError("EXACT_READ_PROJECTION_INVALID") from None


@dataclass(frozen=True, slots=True)
class ExactReadCompletion:
    result_proof: ReadResultProof
    resolution: ReadResolution
    result_record: JournalRecord

    def __post_init__(self) -> None:
        if (
            type(self.result_proof) is not ReadResultProof
            or type(self.resolution) is not ReadResolution
            or type(self.result_record) is not JournalRecord
            or getattr(self.result_record.event, "proof", None) != self.result_proof
        ):
            raise ExecutionProjectionError("EXACT_READ_COMPLETION_INVALID")


@dataclass(frozen=True, slots=True)
class MutationProjection:
    reserved_request: ReservedRequest
    reservation_proof: MutationReservationProof
    attempt: MutationAttempt

    def __post_init__(self) -> None:
        if (
            type(self.reserved_request) is not ReservedRequest
            or type(self.reservation_proof) is not MutationReservationProof
            or type(self.attempt) is not MutationAttempt
        ):
            raise ExecutionProjectionError("MUTATION_PROJECTION_INVALID")
        try:
            self.reservation_proof.validate_dispatch_binding(
                self.reserved_request,
                self.attempt,
            )
        except BaseException:
            raise ExecutionProjectionError("MUTATION_PROJECTION_INVALID") from None


@dataclass(frozen=True, slots=True)
class RecoverySource:
    """Live-journal selected recovery lineage; ``None`` means no attempt exists."""

    source_attempt_id: str | None

    def __post_init__(self) -> None:
        if self.source_attempt_id is not None and (
            type(self.source_attempt_id) is not str
            or len(self.source_attempt_id) != 64
            or any(character not in "0123456789abcdef" for character in self.source_attempt_id)
        ):
            raise ExecutionProjectionError("RECOVERY_SOURCE_INVALID")


@dataclass(frozen=True, slots=True)
class FreshFinalReference:
    final_evidence_sha256: str
    preflight_bundle: PreflightEvidenceBundle
    final_bundle: FinalEvidenceBundle

    def __post_init__(self) -> None:
        if (
            type(self.final_evidence_sha256) is not str
            or len(self.final_evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.final_evidence_sha256)
            or type(self.preflight_bundle) is not PreflightEvidenceBundle
            or type(self.final_bundle) is not FinalEvidenceBundle
            or self.final_bundle.preflight != self.preflight_bundle
        ):
            raise ExecutionProjectionError("FRESH_FINAL_REFERENCE_INVALID")


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionProjectionError("PROJECTION_RECONSTRUCTION_INVALID") from exc
    return hashlib.sha256(encoded).hexdigest()


class ExecutionProjector:
    """The sole concrete credential-free projector used by the supervisor."""

    def __init__(
        self,
        *,
        runtime_snapshot: RuntimeSnapshot,
        execution_journal: ExecutionJournal,
        process_journal: ProcessLifecycleJournal,
    ) -> None:
        if type(runtime_snapshot) is not RuntimeSnapshot:
            raise ExecutionProjectionError("RUNTIME_SNAPSHOT_REQUIRED")
        if type(execution_journal) is not ExecutionJournal:
            raise ExecutionProjectionError("EXECUTION_JOURNAL_REQUIRED")
        if type(process_journal) is not ProcessLifecycleJournal:
            raise ExecutionProjectionError("PROCESS_JOURNAL_REQUIRED")
        if process_journal.execution_journal_path.resolve() != execution_journal.path.resolve():
            raise ExecutionProjectionError("JOURNAL_PATH_BINDING_MISMATCH")
        self._runtime_snapshot = runtime_snapshot
        self._execution_journal = execution_journal
        self._process_journal = process_journal
        self._preflight_provenances: list[PreIntentReadProvenance] = []
        self._preflight_projection: PreflightProjection | None = None
        self._persisted_intent: PersistedIntent | None = None
        self._final_provenances: list[FinalReadProvenance] = []
        self._containment_results: dict[Step, TransportResult] = {}
        self._containment_result_proofs: dict[Step, ReadResultProof] = {}
        self._containment_source_attempt_id: str | None = None
        self._owned_fill_close_proof: OwnedFillCloseProof | None = None

    @property
    def execution_journal(self) -> ExecutionJournal:
        return self._execution_journal

    @property
    def process_journal(self) -> ProcessLifecycleJournal:
        return self._process_journal

    @property
    def runtime_snapshot(self) -> RuntimeSnapshot:
        return self._runtime_snapshot

    @property
    def preflight_path(self) -> Path:
        """The sole canonical durable preflight artifact for this session."""

        return self._execution_journal.path.resolve().parent / "preflight.json"

    def project_primary(self, authority: SessionAuthority) -> PrimaryJournalProjection:
        """Reconstruct generation one from both validated durable journals."""

        if type(authority) is not SessionAuthority or authority.generation != 1:
            raise ExecutionProjectionError("PRIMARY_AUTHORITY_REQUIRED")
        records = self._execution_journal.records()
        established = tuple(
            getattr(record.event, "authority", None)
            for record in records
            if type(getattr(record.event, "authority", None)) is SessionAuthority
        )
        if established != (authority,):
            raise ExecutionProjectionError("PRIMARY_AUTHORITY_REPLAY_MISMATCH")
        active_identity = self._process_journal.active_identity
        if (
            self._process_journal.active_generation != authority.generation
            or self._process_journal.last_generation != authority.generation
            or self._process_journal.active_admission_committed is not True
            or active_identity is None
        ):
            raise ExecutionProjectionError("PRIMARY_PROCESS_GENERATION_MISMATCH")
        admitted = tuple(
            event
            for event in (record.event for record in records)
            if type(event).__name__ == "_GenerationAdmitted"
        )
        if (
            len(admitted) != 1
            or admitted[0].generation != authority.generation
            or admitted[0].capability.value != "PRIMARY"
            or admitted[0].process_identity_sha256 != active_identity.sha256
        ):
            raise ExecutionProjectionError("PRIMARY_EXECUTION_ADMISSION_MISMATCH")
        timing = LifecycleTiming(
            lifecycle_started_at=Decimal(str(self._process_journal.lifecycle_started_at)),
            lifecycle_deadline=Decimal(str(self._process_journal.lifecycle_deadline)),
        )
        close_id = build_emergency_client_order_id(
            authority.runtime_commit,
            authority.session_nonce,
        )
        reconstruction = _canonical_sha256(
            {
                "authority_sha256": authority.authority_sha256,
                "execution_head": records[-1].digest,
                "generation": authority.generation,
                "lifecycle_deadline": format(timing.lifecycle_deadline, "f"),
                "lifecycle_started_at": format(timing.lifecycle_started_at, "f"),
                "process_journal_sha256": hashlib.sha256(
                    self._process_journal.path.read_bytes()
                ).hexdigest(),
                "runtime_commit": self._runtime_snapshot.runtime_commit,
            }
        )
        return PrimaryJournalProjection(
            reconstruction_sha256=reconstruction,
            generation=authority.generation,
            timing=timing,
            probe_client_id=authority.client_id,
            close_client_id=close_id,
        )

    def project_recovery(
        self,
        authority: RecoverySessionAuthority | IntentBoundRecoveryAuthority,
        persisted_intent: PersistedIntent,
    ) -> RecoveryJournalProjection:
        if (
            type(authority) not in {RecoverySessionAuthority, IntentBoundRecoveryAuthority}
            or type(persisted_intent) is not PersistedIntent
            or load_persisted_intent(persisted_intent.path) != persisted_intent
        ):
            raise ExecutionProjectionError("RECOVERY_AUTHORITY_REQUIRED")
        records = self._execution_journal.records()
        primary = _primary_authority(records, authority.primary_authority_sha256)
        selected = self.select_recovery_source(
            primary_authority=primary,
            persisted_intent=persisted_intent,
        )
        try:
            replayed_preflight = load_preflight_evidence(
                self.preflight_path,
                execution_journal=self._execution_journal,
                persisted_intent=persisted_intent,
            )
        except BaseException:
            raise ExecutionProjectionError("RECOVERY_PREFLIGHT_REPLAY_FAILED") from None
        if (
            type(replayed_preflight) is not ReplayedPreflightEvidence
            or replayed_preflight.projection.authority != primary
            or replayed_preflight.projection.intent.intent_sha256
            != persisted_intent.intent.intent_sha256
            or primary.runtime_commit != self._runtime_snapshot.runtime_commit
            or persisted_intent.intent.protocol_commit != self._runtime_snapshot.protocol_commit
            or persisted_intent.intent.protocol_tag_object
            != self._runtime_snapshot.protocol_tag_object
            or persisted_intent.intent.protocol_sha256 != self._runtime_snapshot.protocol_sha256
        ):
            raise ExecutionProjectionError("RECOVERY_PREFLIGHT_REPLAY_MISMATCH")
        source_attempt_id = selected.source_attempt_id
        if type(authority) is RecoverySessionAuthority:
            if authority.source_attempt_id != source_attempt_id:
                raise ExecutionProjectionError("RECOVERY_SOURCE_AUTHORITY_MISMATCH")
        elif source_attempt_id is not None:
            raise ExecutionProjectionError("INTENT_BOUND_RECOVERY_SOURCE_MISMATCH")
        if (
            authority.generation != self._process_journal.active_generation
            or authority.generation <= primary.generation
        ):
            raise ExecutionProjectionError("RECOVERY_GENERATION_MISMATCH")
        snapshot = self._execution_journal.request_ledger_snapshot(primary.authority_sha256)
        budget = _budget_from_ledger(snapshot.last_ledger)
        if source_attempt_id is None:
            target = RecoveryTarget.FINAL_EVIDENCE
            source_sha256 = authority.intent_binding_sha256
        else:
            source = _attempt_by_id(records, source_attempt_id)
            frontier = self._execution_journal.frontier(source_attempt_id)
            target = _recovery_target(source.kind, frontier)
            source_sha256 = source.attempt_id
        timing = LifecycleTiming(
            lifecycle_started_at=Decimal(str(self._process_journal.lifecycle_started_at)),
            lifecycle_deadline=Decimal(str(self._process_journal.lifecycle_deadline)),
        )
        reconstruction = _canonical_sha256(
            {
                "authority_sha256": authority.authority_sha256,
                "budget": _budget_payload(budget),
                "execution_head": records[-1].digest,
                "generation": authority.generation,
                "intent_file_sha256": persisted_intent.file_sha256,
                "process_journal_sha256": hashlib.sha256(
                    self._process_journal.path.read_bytes()
                ).hexdigest(),
                "source_attempt_sha256": source_sha256,
                "target": target.value,
            }
        )
        self._preflight_provenances = list(replayed_preflight.projection.provenances)
        self._preflight_projection = replayed_preflight.projection
        self._persisted_intent = persisted_intent
        self._final_provenances.clear()
        self._containment_results.clear()
        self._containment_result_proofs.clear()
        self._containment_source_attempt_id = None
        self._owned_fill_close_proof = None
        return RecoveryJournalProjection(
            reconstruction_sha256=reconstruction,
            source_attempt_sha256=source_sha256,
            generation=authority.generation,
            timing=timing,
            probe_client_id=primary.client_id,
            close_client_id=build_emergency_client_order_id(
                primary.runtime_commit,
                primary.session_nonce,
            ),
            budget=budget,
            target=target,
        )

    def select_recovery_source(
        self,
        *,
        primary_authority: SessionAuthority,
        persisted_intent: PersistedIntent,
    ) -> RecoverySource:
        """Select the sole replay-authorized recovery lineage after exact reap."""

        if (
            type(primary_authority) is not SessionAuthority
            or type(persisted_intent) is not PersistedIntent
            or load_persisted_intent(persisted_intent.path) != persisted_intent
            or persisted_intent.intent.authorization_id != primary_authority.authorization_id
            or persisted_intent.intent.runtime_commit != primary_authority.runtime_commit
            or persisted_intent.intent.session_nonce != primary_authority.session_nonce
        ):
            raise ExecutionProjectionError("RECOVERY_PRIMARY_LINEAGE_MISMATCH")
        records = self._execution_journal.records()
        replayed = _primary_authority(records, primary_authority.authority_sha256)
        if replayed != primary_authority:
            raise ExecutionProjectionError("RECOVERY_PRIMARY_LINEAGE_MISMATCH")
        bindings = tuple(
            getattr(record.event, "binding", None)
            for record in records
            if getattr(record.event, "binding", None) is not None
            and getattr(
                getattr(record.event, "binding", None),
                "session_authority_sha256",
                None,
            )
            == primary_authority.authority_sha256
        )
        if (
            len(bindings) != 1
            or bindings[0].intent_sha256 != persisted_intent.intent.intent_sha256
            or bindings[0].intent_file_sha256 != persisted_intent.file_sha256
        ):
            raise ExecutionProjectionError("RECOVERY_INTENT_BINDING_MISMATCH")
        attempts = tuple(
            event.attempt
            for event in (record.event for record in records)
            if type(getattr(event, "attempt", None)) is MutationAttempt
            and event.attempt.authorization_id == primary_authority.authorization_id
            and event.attempt.intent_sha256 == persisted_intent.intent.intent_sha256
            and event.attempt.runtime_commit == primary_authority.runtime_commit
            and event.attempt.session_nonce == primary_authority.session_nonce
        )
        reaped_generations = {
            event.receipt.generation
            for event in (record.event for record in records)
            if type(event).__name__ == "_GenerationReaped"
        }
        if not attempts:
            if primary_authority.generation not in reaped_generations:
                raise ExecutionProjectionError("RECOVERY_REQUIRES_REAP")
            return RecoverySource(source_attempt_id=None)
        for attempt in reversed(attempts):
            frontier = self._execution_journal.frontier(attempt.attempt_id)
            if attempt.generation not in reaped_generations:
                continue
            if frontier is FrontierState.UNKNOWN or (
                frontier is FrontierState.CONFIRMED and attempt.kind is MutationKind.CREATE
            ):
                return RecoverySource(source_attempt_id=attempt.attempt_id)
        raise ExecutionProjectionError("RECOVERY_SOURCE_NOT_RECOVERABLE")

    def project_pre_intent_success(
        self,
        *,
        action: PlannedAction,
        result: TransportResult,
        state: LifecycleState,
    ) -> ReadResolution:
        if (
            type(action) is not PlannedAction
            or type(result) is not TransportResult
            or type(state) is not LifecycleState
            or action.step not in PRE_INTENT_READ_STEPS
            or state.pending is None
            or state.pending.action != action
        ):
            raise ExecutionProjectionError("PREFLIGHT_ACTION_NOT_RESERVED")
        records = self._execution_journal.records()
        prepared_matches = tuple(
            record
            for record in records
            if type(getattr(record.event, "reservation", None)) is PreIntentReadReservation
            and record.event.reservation.reservation_sha256 == result.request_sha256
        )
        result_matches = tuple(
            record
            for record in records
            if type(getattr(record.event, "result", None)) is PreIntentReadResult
            and record.event.result.reservation_sha256 == result.request_sha256
        )
        if len(prepared_matches) != 1 or len(result_matches) != 1:
            raise ExecutionProjectionError("PREFLIGHT_DURABLE_RESULT_REQUIRED")
        prepared_record = prepared_matches[0]
        result_record = result_matches[0]
        reservation = prepared_record.event.reservation
        durable_result = result_record.event.result
        if (
            result.logical_request_sha256 != reservation.logical_request_sha256
            or result.result_sha256 != durable_result.result_sha256
            or result_record.sequence <= prepared_record.sequence
        ):
            raise ExecutionProjectionError("PREFLIGHT_DURABLE_RESULT_MISMATCH")
        kind = _preflight_kind(action.step)
        try:
            provenance = PreIntentReadProvenance(
                kind=kind,
                reservation=reservation,
                prepared_record=prepared_record,
                result_record=result_record,
                transport_result=result,
            )
        except BaseException:
            raise ExecutionProjectionError("PREFLIGHT_TYPED_RESULT_MISMATCH") from None
        expected_index = len(self._preflight_provenances)
        if (
            expected_index >= len(PRE_INTENT_READ_STEPS)
            or PRE_INTENT_READ_STEPS[expected_index] is not action.step
            or provenance in self._preflight_provenances
        ):
            raise ExecutionProjectionError("PREFLIGHT_RESULT_SEQUENCE_MISMATCH")
        self._preflight_provenances.append(provenance)
        disposition = self._read_disposition(action, result)
        observed_at = _seconds_from_ns(provenance.observed_at_ns)
        return ReadResolution(
            action_sha256=action.action_sha256,
            result_proof_sha256=durable_result.result_proof_sha256,
            disposition=disposition,
            observed_at=observed_at,
        )

    def project_read_failure(
        self,
        *,
        action: PlannedAction,
        failure: ReadFailureResult,
        state: LifecycleState,
    ) -> ReadResolution:
        if (
            type(action) is not PlannedAction
            or type(failure) is not ReadFailureResult
            or type(state) is not LifecycleState
            or state.pending is None
            or state.pending.action != action
            or failure.generation != state.generation
        ):
            raise ExecutionProjectionError("DURABLE_READ_FAILURE_REQUIRED")
        matches: list[tuple[str, int]] = []
        for record in self._execution_journal.records():
            event_failure = getattr(record.event, "failure", None)
            if type(event_failure) is PreIntentReadFailure:
                if (
                    event_failure.reservation_sha256 == failure.reservation_sha256
                    and event_failure.failure is failure.failure_kind
                    and event_failure.io_may_have_occurred is failure.io_may_have_occurred
                ):
                    matches.append(
                        (event_failure.failure_proof_sha256, event_failure.observed_at_ns)
                    )
            elif (
                type(event_failure) is ExactReadFailure
                and event_failure.request_sha256 == failure.reservation_sha256
                and event_failure.failure is failure.failure_kind
                and event_failure.io_may_have_occurred is failure.io_may_have_occurred
            ):
                matches.append((event_failure.failure_proof_sha256, event_failure.observed_at_ns))
        if len(matches) != 1:
            raise ExecutionProjectionError("DURABLE_READ_FAILURE_MISMATCH")
        proof_sha256, observed_at_ns = matches[0]
        retry_available = action.retry_index == 0 and state.budget.read_retries == 0
        return ReadResolution(
            action_sha256=action.action_sha256,
            result_proof_sha256=proof_sha256,
            disposition=(
                ReadDisposition.SAFE_FAILURE if retry_available else ReadDisposition.UNSAFE_FAILURE
            ),
            observed_at=_seconds_from_ns(observed_at_ns),
        )

    def persist_intent(
        self,
        *,
        authority: SessionAuthority,
        state: LifecycleState,
    ) -> PersistedIntent:
        if (
            type(authority) is not SessionAuthority
            or type(state) is not LifecycleState
            or self._persisted_intent is not None
            or plan_next(state).kind is not ActionKind.PERSIST_INTENT
            or len(self._preflight_provenances) != len(PRE_INTENT_READ_STEPS)
        ):
            raise ExecutionProjectionError("PREFLIGHT_NOT_COMPLETE")
        records = self._execution_journal.records()
        authority_records = tuple(
            record for record in records if getattr(record.event, "authority", None) == authority
        )
        if len(authority_records) != 1:
            raise ExecutionProjectionError("PREFLIGHT_AUTHORITY_REPLAY_MISMATCH")
        try:
            projection = project_preflight_evidence(
                authority=authority,
                authority_record=authority_records[0],
                provenances=tuple(self._preflight_provenances),
                execution_journal=self._execution_journal,
                protocol_commit=self._runtime_snapshot.protocol_commit,
                protocol_tag_object=self._runtime_snapshot.protocol_tag_object,
                protocol_sha256=self._runtime_snapshot.protocol_sha256,
            )
            preflight_receipt = persist_preflight_projection(
                self.preflight_path,
                projection,
            )
            if (
                type(preflight_receipt) is not PersistedPreflightProjection
                or preflight_receipt.path != self.preflight_path
                or preflight_receipt.projection_sha256 != projection.artifact_sha256
            ):
                raise ExecutionProjectionError("PREFLIGHT_PROJECTION_DURABILITY_MISMATCH")
            persisted = persist_intent(
                self._execution_journal.path.parent / "intent.json",
                projection.intent,
            )
        except BaseException:
            raise ExecutionProjectionError("PREFLIGHT_INTENT_PERSIST_FAILED") from None
        self._preflight_projection = projection
        self._persisted_intent = persisted
        return persisted

    def _phase_context(
        self,
        *,
        action: PlannedAction,
        permit: PhaseDeadlinePermit,
        permit_projection: PhasePermitProjection,
        state: LifecycleState,
        authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    ) -> tuple[SessionAuthority, PersistedIntent, MutationLedger, int, Decimal]:
        if (
            type(action) is not PlannedAction
            or type(permit) is not PhaseDeadlinePermit
            or type(permit_projection) is not PhasePermitProjection
            or type(state) is not LifecycleState
            or type(authority)
            not in {SessionAuthority, RecoverySessionAuthority, IntentBoundRecoveryAuthority}
            or state.pending is not None
            or plan_next(state) != action
            or authority.generation != state.generation
            or self._process_journal.active_generation != state.generation
            or permit.generation != state.generation
            or permit.sequence != permit_projection.sequence
            or permit_projection.sequence != state.last_permit_sequence + 1
            or permit_projection.action_sha256 != action.action_sha256
            or Decimal(str(permit.lifecycle_deadline)) != state.timing.lifecycle_deadline
            or permit_projection.lifecycle_deadline != state.timing.lifecycle_deadline
            or Decimal(str(permit.absolute_deadline)) != permit_projection.absolute_deadline
            or permit_projection.issued_at < state.timing.lifecycle_started_at
            or permit_projection.absolute_deadline <= permit_projection.issued_at
            or permit_projection.absolute_deadline > state.timing.lifecycle_deadline
            or permit_projection.absolute_deadline > action.absolute_deadline_cap
        ):
            raise ExecutionProjectionError("PHASE_PROJECTION_MISMATCH")
        records = self._execution_journal.records()
        if type(authority) is SessionAuthority:
            primary = _primary_authority(records, authority.authority_sha256)
            if primary != authority or state.capability is not Capability.PRIMARY:
                raise ExecutionProjectionError("PRIMARY_AUTHORITY_REQUIRED")
        else:
            primary = _primary_authority(records, authority.primary_authority_sha256)
            authority_matches = tuple(
                candidate
                for candidate in (getattr(record.event, "authority", None) for record in records)
                if type(candidate) is type(authority)
                and candidate.authority_sha256 == authority.authority_sha256
            )
            if (
                authority_matches != (authority,)
                or state.capability is not Capability.RECOVERY_ONLY
            ):
                raise ExecutionProjectionError("RECOVERY_AUTHORITY_REQUIRED")
        persisted = self._persisted_intent
        if (
            type(persisted) is not PersistedIntent
            or load_persisted_intent(persisted.path) != persisted
            or persisted.intent.authorization_id != primary.authorization_id
            or persisted.intent.runtime_commit != primary.runtime_commit
            or persisted.intent.session_nonce != primary.session_nonce
        ):
            raise ExecutionProjectionError("PERSISTED_INTENT_REQUIRED")
        snapshot = self._execution_journal.request_ledger_snapshot(primary.authority_sha256)
        if (
            snapshot.authority != primary
            or snapshot.bound_intent_sha256 != persisted.intent.intent_sha256
            or snapshot.pending_requests
            or _budget_from_ledger(snapshot.last_ledger) != state.budget
        ):
            raise ExecutionProjectionError("REQUEST_LEDGER_STATE_MISMATCH")
        deadline_ns = _decimal_seconds_to_ns(permit_projection.absolute_deadline)
        elapsed = permit_projection.issued_at - state.timing.lifecycle_started_at
        if elapsed < snapshot.last_ledger.last_elapsed_seconds:
            raise ExecutionProjectionError("REQUEST_ELAPSED_TIME_REWIND")
        return primary, persisted, snapshot.last_ledger, deadline_ns, elapsed

    def build_exact_read(
        self,
        *,
        action: PlannedAction,
        permit: PhaseDeadlinePermit,
        permit_projection: PhasePermitProjection,
        state: LifecycleState,
        authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    ) -> ExactReadProjection:
        primary, persisted, previous, deadline_ns, elapsed = self._phase_context(
            action=action,
            permit=permit,
            permit_projection=permit_projection,
            state=state,
            authority=authority,
        )
        if (
            action.kind is not ActionKind.READ
            or action.step in PRE_INTENT_READ_STEPS
            or action.method != "GET"
            or action.path is None
        ):
            raise ExecutionProjectionError("EXACT_READ_ACTION_REQUIRED")
        source = _source_attempt_for_read(
            records=self._execution_journal.records(),
            step=action.step,
            authority=authority,
            persisted_intent=persisted,
            retained_source_attempt_id=self._containment_source_attempt_id,
        )
        read_kind, purpose = _read_contract(action.step)
        if action.step is Step.CONTAINMENT_ORDER:
            if source is None:
                raise ExecutionProjectionError("CONTAINMENT_SOURCE_REQUIRED")
            self._containment_source_attempt_id = source.attempt_id
            self._containment_results.clear()
            self._containment_result_proofs.clear()
            self._owned_fill_close_proof = None
        if purpose is not ReadPurpose.EVIDENCE and source is None:
            raise ExecutionProjectionError("READ_SOURCE_ATTEMPT_REQUIRED")
        ledger = _next_request_ledger(
            previous,
            purpose=RequestPurpose.READ,
            retry_index=action.retry_index,
            logical_request_sha256=_logical_read_sha256(
                intent_sha256=persisted.intent.intent_sha256,
                path=action.path,
                parameters=action.parameters,
            ),
            elapsed=elapsed,
        )
        try:
            reserved = ReservedRequest(
                ledger=ledger,
                intent_sha256=persisted.intent.intent_sha256,
                origin=DEMO_HTTP_ORIGIN,
                method=action.method,
                path=action.path,
                purpose=RequestPurpose.READ,
                parameters=action.parameters,
                elapsed_seconds=elapsed,
                retry_index=action.retry_index,
            )
            proof = ReadReservationProof.from_reserved_request(
                reserved,
                read_kind=read_kind,
                purpose=purpose,
                generation=state.generation,
                deadline_ns=deadline_ns,
                source_attempt_id=(source.attempt_id if source is not None else None),
                client_id=(source.client_id if source is not None else None),
                authorization_id=primary.authorization_id,
            )
            return ExactReadProjection(
                reserved_request=reserved,
                reservation_proof=proof,
            )
        except BaseException:
            raise ExecutionProjectionError("EXACT_READ_PROJECTION_FAILED") from None

    def complete_exact_read(
        self,
        *,
        action: PlannedAction,
        result: TransportResult,
        observed_at: Decimal,
        state: LifecycleState,
    ) -> ExactReadCompletion:
        if (
            type(action) is not PlannedAction
            or type(result) is not TransportResult
            or type(observed_at) is not Decimal
            or not observed_at.is_finite()
            or type(state) is not LifecycleState
            or state.pending is None
            or state.pending.action != action
            or action.kind is not ActionKind.READ
            or action.step in PRE_INTENT_READ_STEPS
            or observed_at < state.pending.permit.issued_at
            or observed_at > state.pending.permit.absolute_deadline
            or observed_at > state.timing.lifecycle_deadline
        ):
            raise ExecutionProjectionError("EXACT_READ_COMPLETION_INVALID")
        records = self._execution_journal.records()
        prepared_matches = tuple(
            record
            for record in records
            if type(getattr(record.event, "proof", None)) is ReadReservationProof
            and record.event.proof.request_sha256 == result.request_sha256
        )
        if len(prepared_matches) != 1:
            raise ExecutionProjectionError("READ_PREPARED_RECORD_REQUIRED")
        prepared = prepared_matches[0]
        reservation = prepared.event.proof
        if (
            result.logical_request_sha256 != reservation.logical_request_sha256
            or reservation.generation != state.generation
            or reservation.monotonic_sequence != state.budget.total_http_attempts
            or result.kind not in _expected_response_kinds(action.step)
        ):
            raise ExecutionProjectionError("EXACT_READ_RESULT_BINDING_MISMATCH")
        disposition = self._read_disposition(action, result)
        outcome = _read_outcome(action.step, disposition)
        try:
            proof = ReadResultProof.build(
                request_sha256=result.request_sha256,
                prepared_record_sequence=prepared.sequence,
                prepared_record_digest=prepared.digest,
                generation=state.generation,
                monotonic_sequence=reservation.monotonic_sequence,
                read_kind=reservation.read_kind,
                outcome=outcome,
                result_sha256=result.result_sha256,
                observed_at_ns=_decimal_seconds_to_ns(observed_at),
            )
            result_record = self._execution_journal.record_read_result(proof)
        except BaseException:
            raise ExecutionProjectionError("READ_RESULT_DURABILITY_FAILED") from None
        resolution_proof_sha256 = proof.result_proof_sha256
        if disposition in {
            ReadDisposition.ORDER_NEW,
            ReadDisposition.ORDER_PARTIALLY_FILLED,
        }:
            try:
                source_attempt_id = reservation.source_attempt_id
                if source_attempt_id is None:
                    raise ExecutionProjectionError("READ_SOURCE_ATTEMPT_REQUIRED")
                observation = self._execution_journal.new_reconciliation_observation(
                    source_attempt_id=source_attempt_id,
                    read_result_proof_sha256=proof.result_proof_sha256,
                )
                self._execution_journal.record_reconciliation_observation(observation)
                resolution_proof_sha256 = observation.observation_sha256
            except BaseException:
                raise ExecutionProjectionError("FRESH_OPEN_OBSERVATION_FAILED") from None
        if action.step in FINAL_READ_STEPS:
            self._retain_final_provenance(
                action=action,
                result=result,
                prepared_record=prepared,
                result_record=result_record,
            )
        elif action.step in CONTAINMENT_READ_STEPS:
            self._containment_results[action.step] = result
            self._containment_result_proofs[action.step] = proof
            if action.step is Step.CONTAINMENT_MARK_PRICE:
                self._record_owned_fill_close_proof(observed_at=observed_at)
        return ExactReadCompletion(
            result_proof=proof,
            resolution=ReadResolution(
                action_sha256=action.action_sha256,
                result_proof_sha256=resolution_proof_sha256,
                disposition=disposition,
                observed_at=observed_at,
            ),
            result_record=result_record,
        )

    def build_mutation(
        self,
        *,
        action: PlannedAction,
        permit: PhaseDeadlinePermit,
        permit_projection: PhasePermitProjection,
        state: LifecycleState,
        authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    ) -> MutationProjection:
        primary, persisted, previous, deadline_ns, elapsed = self._phase_context(
            action=action,
            permit=permit,
            permit_projection=permit_projection,
            state=state,
            authority=authority,
        )
        kind = self._mutation_kind(action.kind)
        expected_method = {
            MutationKind.CREATE: "POST",
            MutationKind.CANCEL: "DELETE",
            MutationKind.EMERGENCY_CLOSE: "POST",
        }[kind]
        if (
            action.path != "/fapi/v1/order"
            or action.method != expected_method
            or action.retry_index != 0
        ):
            raise ExecutionProjectionError("MUTATION_ACTION_BINDING_INVALID")
        records = self._execution_journal.records()
        source: MutationAttempt | None = None
        precondition_sha256: str | None = None
        recovery_of_attempt_id: str | None = None
        if kind is MutationKind.CREATE:
            if (
                state.capability is not Capability.PRIMARY
                or type(authority) is not SessionAuthority
            ):
                raise ExecutionProjectionError("RECOVERY_CREATE_FORBIDDEN")
            request_purpose = RequestPurpose.CREATE
            mutation_purpose = MutationPurpose.PRIMARY_CREATE
            parameters = persisted.intent.probe_payload
        elif kind is MutationKind.CANCEL:
            precondition_sha256 = action.precondition_action_sha256
            observation = _observation_by_id(records, precondition_sha256)
            source = _attempt_by_id(records, observation.source_attempt_id)
            request_purpose = RequestPurpose.CANCEL
            mutation_purpose = (
                MutationPurpose.PRIMARY_CANCEL
                if state.capability is Capability.PRIMARY
                else MutationPurpose.RECOVERY_CONDITIONAL_CANCEL
            )
            parameters = persisted.intent.cancel_parameters
            if state.capability is Capability.RECOVERY_ONLY:
                recovery_of_attempt_id = source.attempt_id
        else:
            expected_lifecycle_precondition = _canonical_sha256(
                list(state.containment_source_result_sha256s)
            )
            if action.precondition_action_sha256 != expected_lifecycle_precondition:
                raise ExecutionProjectionError("OWNED_FILL_LIFECYCLE_PROOF_MISMATCH")
            owned = _owned_fill_proof_for_state(
                records,
                state.containment_source_result_sha256s,
                generation=state.generation,
                intent_sha256=persisted.intent.intent_sha256,
            )
            precondition_sha256 = owned.proof_sha256
            source = _attempt_by_id(records, owned.source_attempt_id)
            request_purpose = RequestPurpose.EMERGENCY_CLOSE
            mutation_purpose = (
                MutationPurpose.PRIMARY_EMERGENCY_CLOSE
                if state.capability is Capability.PRIMARY
                else MutationPurpose.RECOVERY_OWNED_FILL_CLOSE
            )
            parameters = persisted.intent.emergency_close_payload(Decimal(owned.residual_quantity))
            if state.capability is Capability.RECOVERY_ONLY:
                recovery_of_attempt_id = source.attempt_id
        ledger = _next_request_ledger(
            previous,
            purpose=request_purpose,
            retry_index=0,
            logical_request_sha256=None,
            elapsed=elapsed,
        )
        try:
            reserved = ReservedRequest(
                ledger=ledger,
                intent_sha256=persisted.intent.intent_sha256,
                origin=DEMO_HTTP_ORIGIN,
                method=action.method,
                path=action.path,
                purpose=request_purpose,
                parameters=tuple(sorted(parameters.items())),
                elapsed_seconds=elapsed,
                retry_index=0,
            )
            client_id = (
                persisted.intent.emergency_client_order_id
                if kind is MutationKind.EMERGENCY_CLOSE
                else persisted.intent.client_order_id
            )
            reservation_proof = MutationReservationProof.from_reserved_request(
                reserved,
                purpose=mutation_purpose,
                generation=state.generation,
                deadline_ns=deadline_ns,
                client_id=client_id,
                authorization_id=primary.authorization_id,
                source_attempt_id=(source.attempt_id if source is not None else None),
                precondition_sha256=precondition_sha256,
            )
            attempt = MutationAttempt.build(
                kind=kind,
                generation=state.generation,
                retry_index=0,
                deadline_ns=deadline_ns,
                reservation_sha256=reserved.request_sha256,
                authorization_id=primary.authorization_id,
                intent_sha256=persisted.intent.intent_sha256,
                runtime_commit=primary.runtime_commit,
                session_nonce=primary.session_nonce,
                fresh_open_proof_sha256=(
                    precondition_sha256 if kind is MutationKind.CANCEL else None
                ),
                recovery_of_attempt_id=recovery_of_attempt_id,
            )
            return MutationProjection(
                reserved_request=reserved,
                reservation_proof=reservation_proof,
                attempt=attempt,
            )
        except BaseException:
            raise ExecutionProjectionError("MUTATION_PROJECTION_FAILED") from None

    def project_mutation_outcome(
        self,
        *,
        action: PlannedAction,
        attempt: MutationAttempt,
        frontier: FrontierState,
        dispatch_result: DispatchResult | None,
        observed_at: Decimal,
        state: LifecycleState,
    ) -> MutationResolution:
        if (
            type(action) is not PlannedAction
            or type(attempt) is not MutationAttempt
            or type(frontier) is not FrontierState
            or (dispatch_result is not None and type(dispatch_result) is not DispatchResult)
            or type(observed_at) is not Decimal
            or not observed_at.is_finite()
            or type(state) is not LifecycleState
            or state.pending is None
            or state.pending.action != action
            or attempt.kind is not self._mutation_kind(action.kind)
            or attempt.generation != state.generation
            or observed_at < state.pending.permit.issued_at
            or observed_at > state.timing.lifecycle_deadline
        ):
            raise ExecutionProjectionError("MUTATION_OUTCOME_BINDING_INVALID")
        try:
            replayed = _attempt_by_id(self._execution_journal.records(), attempt.attempt_id)
            live_frontier = self._execution_journal.frontier(attempt.attempt_id)
        except BaseException:
            raise ExecutionProjectionError("MUTATION_OUTCOME_REPLAY_FAILED") from None
        if replayed != attempt or live_frontier is not frontier:
            raise ExecutionProjectionError("MUTATION_OUTCOME_REPLAY_MISMATCH")
        if dispatch_result is not None and (
            dispatch_result.attempt_id != attempt.attempt_id
            or dispatch_result.generation != attempt.generation
            or dispatch_result.kind is not attempt.kind
            or dispatch_result.client_id != attempt.client_id
            or dispatch_result.transport_result.request_sha256 != attempt.reservation_sha256
            or dispatch_result.transport_result.field("clientOrderId") != attempt.client_id
        ):
            raise ExecutionProjectionError("MUTATION_DISPATCH_RESULT_MISMATCH")
        records = self._execution_journal.records()
        terminal = _mutation_terminal_record(records, attempt, frontier)
        if frontier is FrontierState.CONFIRMED:
            if (
                dispatch_result is None
                or getattr(terminal.event, "result_sha256", None) != dispatch_result.digest
            ):
                raise ExecutionProjectionError("CONFIRMED_DISPATCH_RESULT_REQUIRED")
        elif frontier not in {FrontierState.NOT_DISPATCHED, FrontierState.UNKNOWN}:
            raise ExecutionProjectionError("MUTATION_FRONTIER_NOT_TERMINAL")
        return MutationResolution(
            action_sha256=action.action_sha256,
            frontier_proof_sha256=terminal.digest,
            disposition=self._mutation_disposition(frontier),
            observed_at=observed_at,
            accepted_at=(
                observed_at
                if attempt.kind is MutationKind.CREATE and frontier is FrontierState.CONFIRMED
                else None
            ),
        )

    def fresh_child_final_reference(
        self,
        *,
        state: LifecycleState,
        evidence_log: ExecutionEvidenceLog,
    ) -> FreshFinalReference:
        if (
            type(state) is not LifecycleState
            or type(evidence_log) is not ExecutionEvidenceLog
            or state.pending is not None
            or plan_next(state).kind is not ActionKind.COMPLETE_CHILD
            or state.block_reason is not None
            or state.final_evidence_steps != FINAL_READ_STEPS
            or len(self._final_provenances) != len(FINAL_READ_STEPS)
            or tuple(item.kind for item in self._final_provenances)
            != tuple(_final_evidence_kind(step) for step in FINAL_READ_STEPS)
            or tuple(item.result_proof.result_proof_sha256 for item in self._final_provenances)
            != state.final_result_proof_sha256s
            or evidence_log.execution_journal_path.resolve()
            != self._execution_journal.path.resolve()
        ):
            raise ExecutionProjectionError("FRESH_FINAL_STATE_INCOMPLETE")
        try:
            evidence_log.replay()
            records = self._execution_journal.records()
            persisted = self._persisted_intent
            if (
                type(persisted) is not PersistedIntent
                or load_persisted_intent(persisted.path) != persisted
            ):
                raise ExecutionProjectionError("PREFLIGHT_REPLAY_REQUIRED")
            replayed_preflight = load_preflight_evidence(
                self.preflight_path,
                execution_journal=self._execution_journal,
                persisted_intent=persisted,
            )
            if type(replayed_preflight) is not ReplayedPreflightEvidence:
                raise ExecutionProjectionError("PREFLIGHT_REPLAY_REQUIRED")
            preflight = replayed_preflight.bundle
            attempts_with_records = tuple(
                (record, attempt)
                for record in records
                if type(attempt := getattr(record.event, "attempt", None)) is MutationAttempt
                and attempt.intent_sha256 == persisted.intent.intent_sha256
            )
            if not attempts_with_records:
                raise ExecutionProjectionError("FINAL_MUTATION_BARRIER_REQUIRED")
            _attempt_record, last_attempt = attempts_with_records[-1]
            last_request = _reserved_request_by_sha256(
                records,
                last_attempt.reservation_sha256,
            )
            related = tuple(
                record
                for record in records
                if _record_relates_to_attempt(record, last_attempt)
                and record.sequence
                < min(item.prepared_record.sequence for item in self._final_provenances)
            )
            if not related:
                raise ExecutionProjectionError("FINAL_MUTATION_BARRIER_REQUIRED")
            barrier = MutationBarrier(
                last_request=last_request,
                last_mutation_record=related[-1],
            )
            bundle = project_final_evidence(
                preflight=preflight,
                barrier=barrier,
                provenances=tuple(self._final_provenances),
            )
            digest = final_evidence_bundle_sha256(bundle, self._execution_journal)
            return FreshFinalReference(
                final_evidence_sha256=digest,
                preflight_bundle=preflight,
                final_bundle=bundle,
            )
        except ExecutionProjectionError:
            raise
        except BaseException:
            raise ExecutionProjectionError("FRESH_FINAL_EVIDENCE_INVALID") from None

    def _retain_final_provenance(
        self,
        *,
        action: PlannedAction,
        result: TransportResult,
        prepared_record: JournalRecord,
        result_record: JournalRecord,
    ) -> None:
        if action.step not in FINAL_READ_STEPS:
            raise ExecutionProjectionError("FINAL_READ_STEP_REQUIRED")
        records = self._execution_journal.records()
        reserved = _reserved_request_by_sha256(records, result.request_sha256)
        kind = _final_evidence_kind(action.step)
        try:
            provenance = FinalReadProvenance(
                kind=kind,
                reserved_request=reserved,
                prepared_record=prepared_record,
                result_record=result_record,
                transport_result=result,
            )
        except BaseException:
            raise ExecutionProjectionError("FINAL_READ_PROVENANCE_INVALID") from None
        if action.step is Step.FINAL_ORDER:
            self._final_provenances.clear()
        expected_index = len(self._final_provenances)
        if (
            expected_index >= len(FINAL_READ_STEPS)
            or FINAL_READ_STEPS[expected_index] is not action.step
        ):
            raise ExecutionProjectionError("FINAL_READ_SEQUENCE_MISMATCH")
        self._final_provenances.append(provenance)

    def _record_owned_fill_close_proof(
        self,
        *,
        observed_at: Decimal,
    ) -> OwnedFillCloseProof:
        del observed_at  # Durable reservation elapsed times, not a new clock, bind freshness.
        if (
            self._containment_source_attempt_id is None
            or tuple(self._containment_results) != CONTAINMENT_READ_STEPS
            or tuple(self._containment_result_proofs) != CONTAINMENT_READ_STEPS
        ):
            raise ExecutionProjectionError("OWNED_FILL_READ_SET_INCOMPLETE")
        order = self._containment_results[Step.CONTAINMENT_ORDER]
        trades = self._containment_results[Step.CONTAINMENT_TRADES]
        account = self._containment_results[Step.CONTAINMENT_ACCOUNT]
        exchange = self._containment_results[Step.CONTAINMENT_EXCHANGE_INFO]
        mark = self._containment_results[Step.CONTAINMENT_MARK_PRICE]
        records = self._execution_journal.records()
        source = _attempt_by_id(records, self._containment_source_attempt_id)
        reserved_by_step = {
            step: _reserved_request_by_sha256(records, result.request_sha256)
            for step, result in self._containment_results.items()
        }
        try:
            exchange_fields = dict(exchange.fields)
            market_lot = exchange_fields["marketLotSize"]
            counts = exchange_fields["filterTypeCounts"]
            uninterpreted = exchange_fields["uninterpretedFilterTypes"]
            if (
                type(market_lot) is not dict
                or type(counts) is not dict
                or type(uninterpreted) is not list
            ):
                raise ExecutionProjectionError("OWNED_FILL_FILTERS_INVALID")
            filters = MarketCloseFilters(
                min_quantity=Decimal(market_lot["minQuantity"]),
                max_quantity=Decimal(market_lot["maxQuantity"]),
                step_size=Decimal(market_lot["stepSize"]),
                min_notional=Decimal(exchange_fields["minNotional"]),
                market_lot_size_filter_count=counts.get("MARKET_LOT_SIZE", 0),
                min_notional_filter_count=counts.get("MIN_NOTIONAL", 0),
                uninterpreted_applicable_filter_types=tuple(uninterpreted),
            )
            mark_fields = dict(mark.fields)
            mark_price = Decimal(mark_fields["markPrice"])
            mark_age_ms = (
                Decimal(mark_fields["localWallBeforeMs"]) + Decimal(mark_fields["localWallAfterMs"])
            ) / Decimal(2) - Decimal(mark_fields["time"])
            account_positions = account.field("nonzeroPositions")
            if type(account_positions) is not list or len(account_positions) != 1:
                raise ExecutionProjectionError("OWNED_FILL_POSITION_INVALID")
            residual = Decimal(account_positions[0]["positionAmt"])
            order_fields = dict(order.fields)
            market_proof = MarketCloseProof(
                filter_snapshot_sha256=exchange.result_sha256,
                filter_contract_sha256=filters.canonical_sha256,
                filters=filters,
                quantity=residual,
                mark_price=mark_price,
                mark_price_age_ms=mark_age_ms,
                observed_elapsed_seconds=reserved_by_step[
                    Step.CONTAINMENT_MARK_PRICE
                ].elapsed_seconds,
            )
            position_proof = OwnedPositionProof(
                intent_sha256=source.intent_sha256,
                symbol="ETHUSDT",
                residual_quantity=residual,
                owned_executed_quantity=Decimal(order_fields["executedQty"]),
                position_direction="LONG",
                probe_terminal_status=order_fields["status"],
                open_remainder_quantity=Decimal(0),
                other_activity_absent=True,
                market_close_proof=market_proof,
                observed_after_http_attempt=max(
                    request.ledger.total_http_requests for request in reserved_by_step.values()
                ),
                source_request_sha256s=tuple(
                    sorted(
                        (request.path, request.request_sha256)
                        for request in reserved_by_step.values()
                    )
                ),
                observed_elapsed_seconds=max(
                    request.elapsed_seconds for request in reserved_by_step.values()
                ),
            )
            proof = self._execution_journal.new_owned_fill_close_proof(
                source_attempt_id=source.attempt_id,
                owned_position_proof=position_proof,
                order_transport_result=order,
                trade_transport_result=trades,
                account_transport_result=account,
                symbol_filter_transport_result=exchange,
                mark_price_transport_result=mark,
            )
            self._execution_journal.record_owned_fill_close_proof(proof)
        except BaseException:
            raise ExecutionProjectionError("OWNED_FILL_CLOSE_PROOF_FAILED") from None
        self._owned_fill_close_proof = proof
        return proof

    @staticmethod
    def _mutation_kind(kind: ActionKind) -> MutationKind:
        try:
            return {
                ActionKind.CREATE: MutationKind.CREATE,
                ActionKind.CANCEL: MutationKind.CANCEL,
                ActionKind.EMERGENCY_CLOSE: MutationKind.EMERGENCY_CLOSE,
            }[kind]
        except (KeyError, TypeError) as exc:
            raise ExecutionProjectionError("MUTATION_ACTION_REQUIRED") from exc

    @staticmethod
    def _mutation_disposition(frontier: FrontierState) -> MutationDisposition:
        try:
            return {
                FrontierState.CONFIRMED: MutationDisposition.CONFIRMED,
                FrontierState.NOT_DISPATCHED: MutationDisposition.NOT_DISPATCHED,
                FrontierState.UNKNOWN: MutationDisposition.UNKNOWN,
            }[frontier]
        except (KeyError, TypeError) as exc:
            raise ExecutionProjectionError("MUTATION_FRONTIER_NOT_TERMINAL") from exc

    @staticmethod
    def _read_disposition(
        action: PlannedAction,
        result: TransportResult,
    ) -> ReadDisposition:
        if not isinstance(action, PlannedAction) or type(result) is not TransportResult:
            raise ExecutionProjectionError("TYPED_READ_RESULT_REQUIRED")
        if action.step is Step.PRE_DUPLICATE_ORDER:
            if (
                result.kind is ResponseKind.ORDER_NOT_FOUND
                and set(name for name, _value in result.fields)
                == {"clientOrderId", "outcome", "venueCode"}
                and result.field("outcome") == "CONFIRMED_NOT_FOUND"
                and result.field("venueCode") == -2013
            ):
                return ReadDisposition.ORDER_NOT_FOUND
            raise ExecutionProjectionError("DUPLICATE_LOOKUP_RESULT_INVALID")
        if result.kind is ResponseKind.ORDER_NOT_FOUND:
            return ReadDisposition.ORDER_NOT_FOUND
        if result.kind is ResponseKind.ORDER_OBSERVATION:
            status = result.field("status")
            executed = _decimal_field(result, "executedQty")
            if action.step is Step.RECONCILE_CLOSE_ORDER and status in {
                "FILLED",
                "CANCELED",
                "REJECTED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
            }:
                return ReadDisposition.CLOSE_ORDER_TERMINAL
            if status == "NEW":
                return ReadDisposition.ORDER_NEW
            if status == "PARTIALLY_FILLED":
                return ReadDisposition.ORDER_PARTIALLY_FILLED
            if status == "FILLED":
                return ReadDisposition.ORDER_FILLED
            if status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
                return (
                    ReadDisposition.ORDER_CANCELED_ZERO_FILL
                    if executed == 0
                    else ReadDisposition.ORDER_CANCELED_WITH_FILL
                )
            raise ExecutionProjectionError("ORDER_STATUS_NOT_ALLOWED")
        if result.kind is ResponseKind.ACCOUNT:
            positions = result.field("nonzeroPositions")
            if type(positions) is not list:
                raise ExecutionProjectionError("ACCOUNT_RESULT_INVALID")
            if action.step is Step.PRE_ACCOUNT:
                if positions:
                    raise ExecutionProjectionError("PRE_ACCOUNT_STATE_NOT_FLAT")
                return ReadDisposition.VALIDATED
            if action.step not in {Step.CONTAINMENT_ACCOUNT, Step.FINAL_ACCOUNT}:
                raise ExecutionProjectionError("ACCOUNT_RESULT_CONTEXT_INVALID")
            return (
                ReadDisposition.ACCOUNT_FLAT
                if not positions
                else ReadDisposition.ACCOUNT_OWNED_RESIDUAL
            )
        if action.step.name.startswith("FINAL_"):
            if (
                result.kind
                in {
                    ResponseKind.OPEN_ORDERS,
                    ResponseKind.OPEN_ALGO_ORDERS,
                }
                and result.field("count") != 0
            ):
                raise ExecutionProjectionError("FINAL_OPEN_ORDER_STATE_NOT_CLEAN")
            return ReadDisposition.FINAL_CLEAN
        return ReadDisposition.VALIDATED


def _decimal_field(result: TransportResult, name: str) -> Decimal:
    value = result.field(name)
    if type(value) is not str:
        raise ExecutionProjectionError("TRANSPORT_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ExecutionProjectionError("TRANSPORT_DECIMAL_INVALID") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ExecutionProjectionError("TRANSPORT_DECIMAL_INVALID")
    return parsed


def _seconds_from_ns(value: int) -> Decimal:
    if type(value) is not int or value <= 0:
        raise ExecutionProjectionError("OBSERVATION_TIME_INVALID")
    return Decimal(value) / Decimal(1_000_000_000)


def _decimal_seconds_to_ns(value: Decimal) -> int:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ExecutionProjectionError("OBSERVATION_TIME_INVALID")
    # Phase permits originate as one supervisor-owned monotonic float.  Every
    # process/IPC consumer projects that same value with ``int(seconds * 1e9)``;
    # preserve the contract here without minting or rounding up a deadline.
    nanoseconds = int(value * Decimal(1_000_000_000))
    if nanoseconds <= 0:
        raise ExecutionProjectionError("OBSERVATION_TIME_INVALID")
    return nanoseconds


def _logical_read_sha256(
    *,
    intent_sha256: str,
    path: str,
    parameters: tuple[tuple[str, str], ...],
) -> str:
    return _canonical_sha256(
        {
            "intent_sha256": intent_sha256,
            "method": "GET",
            "origin": DEMO_HTTP_ORIGIN,
            "parameters": dict(parameters),
            "path": path,
            "purpose": RequestPurpose.READ.value,
        }
    )


def _next_request_ledger(
    previous: MutationLedger,
    *,
    purpose: RequestPurpose,
    retry_index: int,
    logical_request_sha256: str | None,
    elapsed: Decimal,
) -> MutationLedger:
    if (
        type(previous) is not MutationLedger
        or type(purpose) is not RequestPurpose
        or type(retry_index) is not int
        or retry_index not in {0, 1}
        or type(elapsed) is not Decimal
        or not elapsed.is_finite()
        or elapsed < previous.last_elapsed_seconds
    ):
        raise ExecutionProjectionError("REQUEST_LEDGER_PROJECTION_INVALID")
    updated = replace(
        previous,
        total_http_requests=previous.total_http_requests + 1,
        last_elapsed_seconds=elapsed,
        retryable_read_sha256=None,
    )
    try:
        if purpose is RequestPurpose.READ:
            if logical_request_sha256 is None:
                raise ExecutionProjectionError("READ_LOGICAL_REQUEST_REQUIRED")
            if retry_index == 0:
                if previous.retryable_read_sha256 is not None:
                    raise ExecutionProjectionError("READ_RETRY_PENDING")
            elif previous.retryable_read_sha256 != logical_request_sha256:
                raise ExecutionProjectionError("READ_RETRY_NOT_PROVEN")
            updated = replace(
                updated,
                post_create_read_requests=(
                    previous.post_create_read_requests + (1 if previous.create_requests == 1 else 0)
                ),
                read_retry_requests=(previous.read_retry_requests + (1 if retry_index == 1 else 0)),
            )
        elif purpose is RequestPurpose.CREATE:
            if retry_index != 0:
                raise ExecutionProjectionError("MUTATION_RETRY_FORBIDDEN")
            updated = replace(
                updated,
                create_requests=previous.create_requests + 1,
                stage=RequestStage.CREATE_ATTEMPTED,
            )
        elif purpose is RequestPurpose.CANCEL:
            if retry_index != 0:
                raise ExecutionProjectionError("MUTATION_RETRY_FORBIDDEN")
            updated = replace(
                updated,
                cancel_requests=previous.cancel_requests + 1,
                stage=RequestStage.CANCEL_ATTEMPTED,
            )
        elif purpose is RequestPurpose.EMERGENCY_CLOSE:
            if retry_index != 0:
                raise ExecutionProjectionError("MUTATION_RETRY_FORBIDDEN")
            updated = replace(
                updated,
                emergency_close_requests=previous.emergency_close_requests + 1,
                stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
            )
        else:  # pragma: no cover - RequestPurpose exhaustiveness guard.
            raise ExecutionProjectionError("REQUEST_PURPOSE_INVALID")
    except BaseException as exc:
        if type(exc) is ExecutionProjectionError:
            raise
        raise ExecutionProjectionError("REQUEST_LEDGER_PROJECTION_INVALID") from None
    return updated


def _read_contract(step: Step) -> tuple[ReadKind, ReadPurpose]:
    order_reconciliation = {
        Step.PROBE_ORDER,
        Step.RECONCILE_CREATE_ORDER,
        Step.RECONCILE_CANCEL_ORDER,
        Step.RECONCILE_CLOSE_ORDER,
    }
    if step in order_reconciliation:
        return ReadKind.ORDER, ReadPurpose.ORDER_RECONCILIATION
    containment = {
        Step.CONTAINMENT_ORDER: ReadKind.ORDER,
        Step.CONTAINMENT_TRADES: ReadKind.TRADE,
        Step.CONTAINMENT_ACCOUNT: ReadKind.ACCOUNT,
        Step.CONTAINMENT_EXCHANGE_INFO: ReadKind.SYMBOL_FILTER,
        Step.CONTAINMENT_MARK_PRICE: ReadKind.MARK_PRICE,
    }
    if step in containment:
        return containment[step], ReadPurpose.OWNED_FILL_CLOSE
    final = {
        Step.FINAL_ORDER: ReadKind.ORDER,
        Step.FINAL_OPEN_ORDERS: ReadKind.GENERAL,
        Step.FINAL_OPEN_ALGO_ORDERS: ReadKind.GENERAL,
        Step.FINAL_TRADES: ReadKind.TRADE,
        Step.FINAL_ACCOUNT: ReadKind.ACCOUNT,
        Step.FINAL_SYMBOL_CONFIG: ReadKind.GENERAL,
        Step.FINAL_POSITION_MODE: ReadKind.GENERAL,
    }
    try:
        return final[step], ReadPurpose.EVIDENCE
    except (KeyError, TypeError) as exc:
        raise ExecutionProjectionError("EXACT_READ_STEP_INVALID") from exc


def _expected_response_kinds(step: Step) -> frozenset[ResponseKind]:
    if step in {
        Step.PROBE_ORDER,
        Step.RECONCILE_CREATE_ORDER,
        Step.RECONCILE_CANCEL_ORDER,
        Step.RECONCILE_CLOSE_ORDER,
    }:
        return frozenset({ResponseKind.ORDER_OBSERVATION, ResponseKind.ORDER_NOT_FOUND})
    expected = {
        Step.CONTAINMENT_ORDER: ResponseKind.ORDER_OBSERVATION,
        Step.CONTAINMENT_TRADES: ResponseKind.USER_TRADES,
        Step.CONTAINMENT_ACCOUNT: ResponseKind.ACCOUNT,
        Step.CONTAINMENT_EXCHANGE_INFO: ResponseKind.EXCHANGE_INFO,
        Step.CONTAINMENT_MARK_PRICE: ResponseKind.MARK_PRICE,
        Step.FINAL_ORDER: ResponseKind.ORDER_OBSERVATION,
        Step.FINAL_OPEN_ORDERS: ResponseKind.OPEN_ORDERS,
        Step.FINAL_OPEN_ALGO_ORDERS: ResponseKind.OPEN_ALGO_ORDERS,
        Step.FINAL_TRADES: ResponseKind.USER_TRADES,
        Step.FINAL_ACCOUNT: ResponseKind.ACCOUNT,
        Step.FINAL_SYMBOL_CONFIG: ResponseKind.SYMBOL_CONFIG,
        Step.FINAL_POSITION_MODE: ResponseKind.POSITION_MODE,
    }
    try:
        return frozenset({expected[step]})
    except (KeyError, TypeError) as exc:
        raise ExecutionProjectionError("EXACT_READ_STEP_INVALID") from exc


def _read_outcome(step: Step, disposition: ReadDisposition) -> ReadOutcome:
    if step in CONTAINMENT_READ_STEPS:
        if step is Step.CONTAINMENT_ORDER:
            if disposition is ReadDisposition.ORDER_NEW:
                return ReadOutcome.ORDER_NEW
            if disposition is ReadDisposition.ORDER_PARTIALLY_FILLED:
                return ReadOutcome.ORDER_PARTIALLY_FILLED
            return ReadOutcome.OWNED_ORDER_FILL_CONFIRMED
        if step is Step.CONTAINMENT_TRADES:
            return ReadOutcome.OWNED_TRADE_FILL_CONFIRMED
        if step is Step.CONTAINMENT_ACCOUNT:
            return (
                ReadOutcome.OWNED_ACCOUNT_POSITION_CONFIRMED
                if disposition is ReadDisposition.ACCOUNT_OWNED_RESIDUAL
                else ReadOutcome.NEGATIVE
            )
        if step is Step.CONTAINMENT_EXCHANGE_INFO:
            return ReadOutcome.FILTER_SNAPSHOT_CONFIRMED
        return ReadOutcome.MARK_PRICE_CONFIRMED
    if step in FINAL_READ_STEPS:
        if step is Step.FINAL_ORDER:
            return ReadOutcome.ORDER_TERMINAL
        if step in {
            Step.FINAL_OPEN_ORDERS,
            Step.FINAL_OPEN_ALGO_ORDERS,
            Step.FINAL_TRADES,
            Step.FINAL_ACCOUNT,
        }:
            return ReadOutcome.NEGATIVE
        return ReadOutcome.SUCCESS
    if disposition is ReadDisposition.ORDER_NEW:
        return ReadOutcome.ORDER_NEW
    if disposition is ReadDisposition.ORDER_PARTIALLY_FILLED:
        return ReadOutcome.ORDER_PARTIALLY_FILLED
    if disposition is ReadDisposition.ORDER_NOT_FOUND:
        return ReadOutcome.NEGATIVE
    return ReadOutcome.ORDER_TERMINAL


def _final_evidence_kind(step: Step) -> EvidenceKind:
    try:
        return {
            Step.FINAL_ORDER: EvidenceKind.ORDER,
            Step.FINAL_OPEN_ORDERS: EvidenceKind.OPEN_REGULAR_ORDERS,
            Step.FINAL_OPEN_ALGO_ORDERS: EvidenceKind.OPEN_ALGO_ORDERS,
            Step.FINAL_TRADES: EvidenceKind.TRADE,
            Step.FINAL_ACCOUNT: EvidenceKind.ACCOUNT,
            Step.FINAL_SYMBOL_CONFIG: EvidenceKind.SYMBOL_CONFIG,
            Step.FINAL_POSITION_MODE: EvidenceKind.POSITION_MODE,
        }[step]
    except (KeyError, TypeError) as exc:
        raise ExecutionProjectionError("FINAL_READ_STEP_REQUIRED") from exc


def _attempts_for_intent(
    records: tuple[JournalRecord, ...],
    persisted_intent: PersistedIntent,
) -> tuple[MutationAttempt, ...]:
    intent = persisted_intent.intent
    return tuple(
        attempt
        for record in records
        if type(attempt := getattr(record.event, "attempt", None)) is MutationAttempt
        and attempt.authorization_id == intent.authorization_id
        and attempt.intent_sha256 == intent.intent_sha256
        and attempt.runtime_commit == intent.runtime_commit
        and attempt.session_nonce == intent.session_nonce
    )


def _source_attempt_for_read(
    *,
    records: tuple[JournalRecord, ...],
    step: Step,
    authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    persisted_intent: PersistedIntent,
    retained_source_attempt_id: str | None,
) -> MutationAttempt | None:
    if step in FINAL_READ_STEPS:
        return None
    if type(authority) is IntentBoundRecoveryAuthority:
        raise ExecutionProjectionError("INTENT_BOUND_RECOVERY_READ_FORBIDDEN")
    attempts = _attempts_for_intent(records, persisted_intent)
    if step in {Step.PROBE_ORDER, Step.RECONCILE_CREATE_ORDER}:
        kinds = {MutationKind.CREATE}
    elif step is Step.RECONCILE_CANCEL_ORDER:
        kinds = {MutationKind.CANCEL}
    elif step is Step.RECONCILE_CLOSE_ORDER:
        kinds = {MutationKind.EMERGENCY_CLOSE}
    elif step in CONTAINMENT_READ_STEPS:
        if retained_source_attempt_id is not None:
            return _attempt_by_id(records, retained_source_attempt_id)
        kinds = {MutationKind.CREATE, MutationKind.CANCEL}
    else:
        raise ExecutionProjectionError("EXACT_READ_STEP_INVALID")
    matches = tuple(attempt for attempt in attempts if attempt.kind in kinds)
    if not matches:
        raise ExecutionProjectionError("READ_SOURCE_ATTEMPT_REQUIRED")
    return matches[-1]


def _reserved_request_by_sha256(
    records: tuple[JournalRecord, ...],
    request_sha256: str,
) -> ReservedRequest:
    matches = tuple(
        reserved
        for record in records
        if type(reserved := getattr(record.event, "reserved_request", None)) is ReservedRequest
        and reserved.request_sha256 == request_sha256
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("EXACT_REQUEST_REPLAY_MISMATCH")
    return matches[0]


def _observation_by_id(
    records: tuple[JournalRecord, ...],
    observation_sha256: str | None,
) -> ReconciliationObservation:
    matches = tuple(
        observation
        for record in records
        if type(observation := getattr(record.event, "observation", None))
        is ReconciliationObservation
        and observation.observation_sha256 == observation_sha256
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("FRESH_OPEN_OBSERVATION_REPLAY_MISMATCH")
    return matches[0]


def _owned_fill_proof_by_id(
    records: tuple[JournalRecord, ...],
    proof_sha256: str | None,
) -> OwnedFillCloseProof:
    matches = tuple(
        proof
        for record in records
        if type(proof := getattr(record.event, "proof", None)) is OwnedFillCloseProof
        and proof.proof_sha256 == proof_sha256
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("OWNED_FILL_PROOF_REPLAY_MISMATCH")
    return matches[0]


def _owned_fill_proof_for_state(
    records: tuple[JournalRecord, ...],
    source_result_proof_sha256s: tuple[str, ...],
    *,
    generation: int,
    intent_sha256: str,
) -> OwnedFillCloseProof:
    matches = tuple(
        proof
        for record in records
        if type(proof := getattr(record.event, "proof", None)) is OwnedFillCloseProof
        and proof.generation == generation
        and proof.source_intent_sha256 == intent_sha256
        and (
            proof.order_result.result_proof_sha256,
            proof.trade_result.result_proof_sha256,
            proof.account_result.result_proof_sha256,
            proof.symbol_filter_result.result_proof_sha256,
            proof.mark_price_result.result_proof_sha256,
        )
        == source_result_proof_sha256s
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("OWNED_FILL_PROOF_REPLAY_MISMATCH")
    return matches[0]


def _mutation_terminal_record(
    records: tuple[JournalRecord, ...],
    attempt: MutationAttempt,
    frontier: FrontierState,
) -> JournalRecord:
    matches = tuple(
        record
        for record in records
        if getattr(record.event, "attempt_id", None) == attempt.attempt_id
        and (
            (
                frontier is FrontierState.CONFIRMED
                and type(getattr(record.event, "result_sha256", None)) is str
            )
            or getattr(record.event, "state", None) is frontier
        )
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("MUTATION_TERMINAL_RECORD_MISMATCH")
    return matches[0]


def _record_relates_to_attempt(
    record: JournalRecord,
    attempt: MutationAttempt,
) -> bool:
    if getattr(record.event, "attempt", None) == attempt:
        return True
    if getattr(record.event, "attempt_id", None) == attempt.attempt_id:
        return True
    proof = getattr(record.event, "proof", None)
    return (
        type(proof) is MutationReservationProof
        and proof.request_sha256 == attempt.reservation_sha256
    )


def _preflight_kind(step: Step) -> PreflightKind:
    try:
        return {
            Step.PRE_SERVER_TIME: PreflightKind.SERVER_TIME,
            Step.PRE_POSITION_MODE: PreflightKind.POSITION_MODE,
            Step.PRE_SYMBOL_CONFIG: PreflightKind.SYMBOL_CONFIG,
            Step.PRE_ACCOUNT: PreflightKind.ACCOUNT,
            Step.PRE_OPEN_ORDERS: PreflightKind.OPEN_REGULAR_ORDERS,
            Step.PRE_OPEN_ALGO_ORDERS: PreflightKind.OPEN_ALGO_ORDERS,
            Step.PRE_EXCHANGE_INFO: PreflightKind.EXCHANGE_INFO,
            Step.PRE_DUPLICATE_ORDER: PreflightKind.DUPLICATE_ORDER,
            Step.PRE_USER_TRADES: PreflightKind.TRADE,
            Step.PRE_BOOK_TICKER: PreflightKind.BOOK_TICKER,
            Step.PRE_MARK_PRICE: PreflightKind.MARK_PRICE,
        }[step]
    except (KeyError, TypeError) as exc:
        raise ExecutionProjectionError("PREFLIGHT_STEP_REQUIRED") from exc


def _primary_authority(
    records: tuple[JournalRecord, ...],
    authority_sha256: str,
) -> SessionAuthority:
    matches = tuple(
        authority
        for authority in (getattr(record.event, "authority", None) for record in records)
        if type(authority) is SessionAuthority and authority.authority_sha256 == authority_sha256
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("PRIMARY_AUTHORITY_REPLAY_MISMATCH")
    return matches[0]


def _attempt_by_id(
    records: tuple[JournalRecord, ...],
    attempt_id: str,
) -> MutationAttempt:
    matches = tuple(
        attempt
        for attempt in (getattr(record.event, "attempt", None) for record in records)
        if type(attempt) is MutationAttempt and attempt.attempt_id == attempt_id
    )
    if len(matches) != 1:
        raise ExecutionProjectionError("RECOVERY_ATTEMPT_REPLAY_MISMATCH")
    return matches[0]


def _budget_from_ledger(ledger: MutationLedger) -> BudgetState:
    if type(ledger) is not MutationLedger:
        raise ExecutionProjectionError("REQUEST_LEDGER_REPLAY_REQUIRED")
    mutation_requests = (
        ledger.create_requests + ledger.cancel_requests + ledger.emergency_close_requests
    )
    pre_intent = ledger.total_http_requests - ledger.post_create_read_requests - mutation_requests
    try:
        return BudgetState(
            total_http_attempts=ledger.total_http_requests,
            pre_intent_read_attempts=pre_intent,
            post_create_read_attempts=ledger.post_create_read_requests,
            read_retries=ledger.read_retry_requests,
            create_requests=ledger.create_requests,
            cancel_requests=ledger.cancel_requests,
            close_requests=ledger.emergency_close_requests,
            mutation_requests=mutation_requests,
            submissions=ledger.create_requests + ledger.emergency_close_requests,
        )
    except BaseException:
        raise ExecutionProjectionError("REQUEST_LEDGER_BUDGET_MISMATCH") from None


def _budget_payload(budget: BudgetState) -> dict[str, int]:
    return {
        "cancel_requests": budget.cancel_requests,
        "close_requests": budget.close_requests,
        "create_requests": budget.create_requests,
        "mutation_requests": budget.mutation_requests,
        "post_create_read_attempts": budget.post_create_read_attempts,
        "pre_intent_read_attempts": budget.pre_intent_read_attempts,
        "read_retries": budget.read_retries,
        "submissions": budget.submissions,
        "total_http_attempts": budget.total_http_attempts,
    }


def _recovery_target(kind: MutationKind, frontier: FrontierState) -> RecoveryTarget:
    if frontier is FrontierState.UNKNOWN:
        return {
            MutationKind.CREATE: RecoveryTarget.CREATE_UNKNOWN,
            MutationKind.CANCEL: RecoveryTarget.CANCEL_UNKNOWN,
            MutationKind.EMERGENCY_CLOSE: RecoveryTarget.CLOSE_UNKNOWN,
        }[kind]
    if frontier is FrontierState.CONFIRMED:
        return {
            MutationKind.CREATE: RecoveryTarget.CREATE_UNKNOWN,
            MutationKind.CANCEL: RecoveryTarget.FINAL_EVIDENCE,
            MutationKind.EMERGENCY_CLOSE: RecoveryTarget.CLOSE_UNKNOWN,
        }[kind]
    raise ExecutionProjectionError("RECOVERY_FRONTIER_NOT_ALLOWED")
