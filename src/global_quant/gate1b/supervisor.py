"""Credential-free typed controller for the frozen Gate 1B process boundary.

This module composes the already-hardened process, journal, lifecycle, child
session, and dispatch-kernel contracts.  It has no generic process launcher and
does not decide a final Gate outcome.  A single production workload is admitted,
all child commands consume permits minted from the one lifecycle deadline, and
mutation ambiguity is settled only after an exact process reap.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from global_quant.gate1b.credential_execution_session import (
    SESSION_SCHEMA_VERSION,
    BindIntentCommand,
    IntentBindingReference,
    ReadBindingKind,
    ReadCommand,
    ReadFailureResult,
    SessionFinalState,
    SessionFinishCommand,
    SessionInitCommand,
    transport_result_from_payload,
)
from global_quant.gate1b.credential_transport import TransportResult
from global_quant.gate1b.durable_intent import PersistedIntent, load_persisted_intent
from global_quant.gate1b.execution_evidence_log import (
    ExecutionEvidenceLog,
    ExecutionEvidenceLogError,
    ExecutionEvidenceRecord,
)
from global_quant.gate1b.execution_journal import (
    ExecutionJournal,
    FrontierState,
    GenerationCapability,
    IntentBoundRecoveryAuthority,
    IntentChainBinding,
    MutationAttempt,
    MutationKind,
    MutationReservationProof,
    PreparedPreIntentRead,
    ReadReservationProof,
    ReadResultProof,
    ReconciliationObservation,
    RecoverySessionAuthority,
    SessionAuthority,
    _ReconciliationObserved,
)
from global_quant.gate1b.execution_kernel import (
    DispatchFailure,
    DispatchKernel,
    DispatchKernelError,
    DispatchResult,
)
from global_quant.gate1b.execution_lifecycle import (
    ActionKind,
    Capability,
    Freshness,
    LifecycleState,
    LocalDisposition,
    LocalResolution,
    MutationResolution,
    PhasePermitProjection,
    PlannedAction,
    ReadResolution,
    apply_local,
    plan_next,
    reserve_http,
    resolve_http,
    resume_recovery,
    start_primary,
)
from global_quant.gate1b.execution_projection import (
    ExactReadCompletion,
    ExactReadProjection,
    ExecutionProjector,
    FreshFinalReference,
    MutationProjection,
)
from global_quant.gate1b.final_evidence import (
    BlockedFinalizationCause,
    FinalEvidenceFinalizer,
    FinalizedEvidence,
)
from global_quant.gate1b.mutation_protocol import ReservedRequest
from global_quant.gate1b.process_boundary import (
    IPC_VERSION,
    CredentialProcessSupervisor,
    CredentialWorkload,
    CredentialWorkloadKind,
    IPCMessage,
    IPCProtocolError,
    ManagedChild,
    PhaseDeadlinePermit,
    ProcessLifecycleJournal,
    ReapAttestation,
)
from global_quant.gate1b.runtime_binding import RuntimeSnapshot, verify_runtime_unchanged

_CHILD_SCHEMA_VERSION = "gate1b.credential-child.v1"
_CHILD_PREEXIT_SCHEMA_VERSION = "gate1b.credential-child-pre-exit.v1"
_CONTROL_LOCAL_LIMIT_SECONDS = Decimal("5")
_NS_PER_SECOND = 1_000_000_000
_SHA256_LENGTH = 64
_FACTORY_TOKEN = object()
_RECOVERABLE_REAPED_SESSION_FAILURES = frozenset(
    {
        "BIND_INTENT_FAILED",
        "EXACT_READ_DISPATCH_FAILED",
        "MUTATION_PREPARE_FAILED",
        "READ_RESULT_DURABILITY_FAILED",
        "SESSION_FINISH_FAILED",
    }
)


class SupervisorError(RuntimeError):
    """A sanitized, fail-closed controller error."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class SessionStart:
    """Sanitized proof that one typed child session reached SESSION_READY."""

    generation: int
    capability: GenerationCapability
    authority_sha256: str
    reconstruction_sha256: str


@dataclass(frozen=True, slots=True)
class IssuedPhase:
    """The exact process permit and its Decimal lifecycle projection."""

    action: PlannedAction
    permit: PhaseDeadlinePermit
    projection: PhasePermitProjection


@dataclass(frozen=True, slots=True)
class PendingRead:
    """A durably reserved READ already sent to the sole credential child."""

    action: PlannedAction
    phase: IssuedPhase
    command: ReadCommand
    pre_intent: PreparedPreIntentRead | None
    exact_read_proof: ReadReservationProof | None


@dataclass(frozen=True, slots=True)
class DurableReadOutcome:
    """A sanitized child result whose terminal journal record is durable."""

    pending: PendingRead
    transport_result: TransportResult
    journal_record_sequence: int
    journal_record_digest: str
    result_proof_sha256: str


@dataclass(frozen=True, slots=True)
class UndurableExactReadOutcome:
    """An exact-read result that cannot authorize another command yet."""

    pending: PendingRead
    transport_result: TransportResult


@dataclass(frozen=True, slots=True)
class DurableReadFailureOutcome:
    """A sanitized READ failure durably closed in the request ledger."""

    pending: PendingRead
    failure: ReadFailureResult
    journal_record_sequence: int
    journal_record_digest: str


@dataclass(frozen=True, slots=True)
class MutationDispatchOutcome:
    """Typed mutation frontier; local reap never implies venue non-dispatch."""

    action: PlannedAction
    attempt: MutationAttempt
    frontier: FrontierState
    dispatch_result: DispatchResult | None
    evidence_record: ExecutionEvidenceRecord | None
    reap_attestation: ReapAttestation | None
    evidence_eligible: bool


@dataclass(frozen=True, slots=True)
class SessionExit:
    """Typed pre-exit validation followed by an exact local process reap."""

    generation: int
    capability: GenerationCapability
    child_pre_exit_path: Path
    child_pre_exit_sha256: str
    loaded_project_module_paths: tuple[Path, ...]
    post_runtime_snapshot: RuntimeSnapshot
    reap_attestation: ReapAttestation
    lifecycle_state: LifecycleState


@dataclass(frozen=True, slots=True)
class ExecutionCompletion:
    """Credential-free completion returned only after the final exact reap."""

    session_start: SessionStart
    session_exit: SessionExit
    lifecycle_state: LifecycleState
    recovery_generations: tuple[int, ...]
    fresh_final_reference: FreshFinalReference | None
    finalized_evidence: FinalizedEvidence
    final_evidence_eligible: bool
    evidence_block_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DrivenSessionExit:
    session_exit: SessionExit
    fresh_final_reference: FreshFinalReference | None


@dataclass(slots=True)
class _ActiveSession:
    child: ManagedChild
    authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority
    lifecycle_state: LifecycleState
    kernel: DispatchKernel


class ExecutionSupervisor:
    """Closed production composition of the process-bound execution kernel."""

    def __init__(
        self,
        *,
        workload: CredentialWorkload,
        process_supervisor: CredentialProcessSupervisor,
        execution_journal: ExecutionJournal,
        process_lifecycle_journal: ProcessLifecycleJournal,
        evidence_log: ExecutionEvidenceLog,
        projector: ExecutionProjector,
        finalizer: FinalEvidenceFinalizer,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _FACTORY_TOKEN:
            raise SupervisorError("USE_PRODUCTION_FACTORY")
        self._workload = workload
        self._process_supervisor = process_supervisor
        self._execution_journal = execution_journal
        self._process_lifecycle_journal = process_lifecycle_journal
        self._evidence_log = evidence_log
        self._projector = projector
        self._finalizer = finalizer
        self._active: _ActiveSession | None = None
        self._launching_child: ManagedChild | None = None
        self._pending_read: PendingRead | None = None
        self._undurable_exact_read: UndurableExactReadOutcome | None = None
        self._primary_authority: SessionAuthority | None = None
        self._persisted_intent: PersistedIntent | None = None
        self._intent_reference: IntentBindingReference | None = None
        self._last_reap_attestation: ReapAttestation | None = None
        self._last_lifecycle_state: LifecycleState | None = None
        self._finalization_attempted = False
        self._final_evidence_eligible = True
        self._evidence_block_reasons: list[str] = []

    @classmethod
    def production(
        cls,
        *,
        workload: CredentialWorkload,
        process_supervisor: CredentialProcessSupervisor,
        execution_journal: ExecutionJournal,
        process_lifecycle_journal: ProcessLifecycleJournal,
        evidence_log: ExecutionEvidenceLog,
        projector: ExecutionProjector,
        finalizer: FinalEvidenceFinalizer,
    ) -> ExecutionSupervisor:
        """Bind only the canonical production workload and three durable logs."""

        if (
            type(workload) is not CredentialWorkload
            or workload.kind is not CredentialWorkloadKind.PRODUCTION
        ):
            raise SupervisorError("PRODUCTION_WORKLOAD_REQUIRED")
        if type(process_supervisor) is not CredentialProcessSupervisor:
            raise SupervisorError("PROCESS_SUPERVISOR_REQUIRED")
        if type(execution_journal) is not ExecutionJournal:
            raise SupervisorError("EXECUTION_JOURNAL_REQUIRED")
        if type(process_lifecycle_journal) is not ProcessLifecycleJournal:
            raise SupervisorError("PROCESS_LIFECYCLE_JOURNAL_REQUIRED")
        if type(evidence_log) is not ExecutionEvidenceLog:
            raise SupervisorError("EXECUTION_EVIDENCE_LOG_REQUIRED")
        if type(projector) is not ExecutionProjector:
            raise SupervisorError("EXECUTION_PROJECTOR_REQUIRED")
        if type(finalizer) is not FinalEvidenceFinalizer:
            raise SupervisorError("FINAL_EVIDENCE_FINALIZER_REQUIRED")

        request_path = execution_journal.path.resolve()
        lifecycle_path = process_lifecycle_journal.path.resolve()
        evidence_path = evidence_log.path.resolve()
        root = request_path.parent
        finalizer_journal_path = getattr(finalizer, "_execution_journal_path", None)
        if (
            request_path.name != "request-ledger.json"
            or lifecycle_path.name != "lifecycle.jsonl"
            or evidence_path.name != "requests.jsonl"
            or lifecycle_path.parent != root
            or evidence_path.parent != root
            or evidence_log.execution_journal_path.resolve() != request_path
            or process_lifecycle_journal.execution_journal_path.resolve() != request_path
        ):
            raise SupervisorError("CANONICAL_ARTIFACT_BINDING_REQUIRED")
        if (
            getattr(process_supervisor, "_execution_journal", None) is not execution_journal
            or getattr(process_supervisor, "_lifecycle_journal", None)
            is not process_lifecycle_journal
            or process_supervisor.deadline.at != process_lifecycle_journal.lifecycle_deadline
            or projector.execution_journal is not execution_journal
            or projector.process_journal is not process_lifecycle_journal
            or finalizer.root.resolve() != root
            or not isinstance(finalizer_journal_path, Path)
            or finalizer_journal_path.resolve() != request_path
        ):
            raise SupervisorError("PROCESS_SUPERVISOR_BINDING_MISMATCH")
        return cls(
            workload=workload,
            process_supervisor=process_supervisor,
            execution_journal=execution_journal,
            process_lifecycle_journal=process_lifecycle_journal,
            evidence_log=evidence_log,
            projector=projector,
            finalizer=finalizer,
            _factory_token=_FACTORY_TOKEN,
        )

    @property
    def active_generation(self) -> int | None:
        active = self._active
        if active is not None:
            return active.child.generation
        child = self._launching_child
        return child.generation if child is not None else None

    @property
    def lifecycle_state(self) -> LifecycleState:
        return self._require_active().lifecycle_state

    @property
    def final_evidence_eligible(self) -> bool:
        return self._final_evidence_eligible

    @property
    def evidence_block_reasons(self) -> tuple[str, ...]:
        return tuple(self._evidence_block_reasons)

    def execute_primary(
        self,
        *,
        authority: SessionAuthority,
    ) -> ExecutionCompletion:
        """Own the complete primary loop and return only after exact local reap."""

        if self._primary_authority is not None:
            raise SupervisorError("SUPERVISOR_EXECUTION_ALREADY_STARTED")
        recovery_generations: tuple[int, ...] = ()
        try:
            session_start = self.start_primary(authority=authority)
            self._primary_authority = authority
            try:
                driven = self._drive_active_session()
            except SupervisorError as error:
                if (
                    self._active is not None
                    or self._persisted_intent is None
                    or error.reason not in _RECOVERABLE_REAPED_SESSION_FAILURES
                ):
                    raise
                driven, recoveries = self._recover_until_terminal(
                    primary_authority=authority,
                )
                recovery_generations = tuple(item.generation for item in recoveries)
            if driven is None:
                if self._persisted_intent is None:
                    raise SupervisorError("RECOVERY_INTENT_REQUIRED")
                driven, recoveries = self._recover_until_terminal(
                    primary_authority=authority,
                )
                recovery_generations = tuple(item.generation for item in recoveries)
            return self._finalize_completion(
                session_start=session_start,
                driven=driven,
                recovery_generations=recovery_generations,
            )
        except SupervisorError as error:
            self._finalize_crash_after_reap(
                cause=self._blocked_finalization_cause(error),
            )
            raise
        except BaseException:
            self._finalize_crash_after_reap(
                cause=BlockedFinalizationCause.CREDENTIAL_CHILD_CRASH,
            )
            raise SupervisorError("PRIMARY_EXECUTION_FAILED") from None
        finally:
            if self._active is not None:
                self._kill_active_child()
            elif self._launching_child is not None:
                self._kill_launching_child()

    def execute_recovery(
        self,
        *,
        primary_authority: SessionAuthority,
    ) -> ExecutionCompletion:
        """Resume only cleanup/reconciliation from live durable state."""

        if self._primary_authority is not None:
            raise SupervisorError("SUPERVISOR_EXECUTION_ALREADY_STARTED")
        self._primary_authority = primary_authority
        try:
            driven, recoveries = self._recover_until_terminal(
                primary_authority=primary_authority,
            )
            session_start = recoveries[0]
            return self._finalize_completion(
                session_start=session_start,
                driven=driven,
                recovery_generations=tuple(item.generation for item in recoveries),
            )
        except SupervisorError as error:
            self._finalize_crash_after_reap(
                cause=self._blocked_finalization_cause(error),
            )
            raise
        except BaseException:
            self._finalize_crash_after_reap(
                cause=BlockedFinalizationCause.CREDENTIAL_CHILD_CRASH,
            )
            raise SupervisorError("RECOVERY_EXECUTION_FAILED") from None
        finally:
            if self._active is not None:
                self._kill_active_child()
            elif self._launching_child is not None:
                self._kill_launching_child()

    def _recover_until_terminal(
        self,
        *,
        primary_authority: SessionAuthority,
    ) -> tuple[_DrivenSessionExit, tuple[SessionStart, ...]]:
        """Relaunch recovery-only generations under the original absolute deadline."""

        sessions: list[SessionStart] = []
        while True:
            if self._process_supervisor.deadline.remaining() <= 0:
                raise SupervisorError("LIFECYCLE_DEADLINE_EXHAUSTED")
            previous_reap = self._last_reap_attestation
            try:
                session = self.start_recovery(primary_authority=primary_authority)
            except SupervisorError as error:
                if (
                    error.reason != "RECOVERY_SESSION_START_FAILED"
                    or self._active is not None
                    or self._launching_child is not None
                    or type(self._last_reap_attestation) is not ReapAttestation
                    or self._last_reap_attestation is previous_reap
                ):
                    raise
                continue
            sessions.append(session)
            try:
                driven = self._drive_active_session()
            except SupervisorError as error:
                if (
                    self._active is not None
                    or error.reason not in _RECOVERABLE_REAPED_SESSION_FAILURES
                ):
                    raise
                continue
            if driven is not None:
                return driven, tuple(sessions)

    def _finalize_completion(
        self,
        *,
        session_start: SessionStart,
        driven: _DrivenSessionExit,
        recovery_generations: tuple[int, ...],
    ) -> ExecutionCompletion:
        reference = driven.fresh_final_reference
        self._finalization_attempted = True
        try:
            if reference is None:
                finalized = self._finalizer.finalize_blocked(
                    driven.session_exit.reap_attestation,
                    cause=BlockedFinalizationCause.FINAL_READ_SCHEDULE_INCOMPLETE,
                )
            else:
                finalized = self._finalizer.finalize(
                    reference.final_bundle,
                    driven.session_exit.reap_attestation,
                )
        except BaseException:
            raise SupervisorError("FINAL_EVIDENCE_FINALIZATION_FAILED") from None
        if type(finalized) is not FinalizedEvidence:
            raise SupervisorError("FINALIZED_EVIDENCE_REQUIRED")
        return ExecutionCompletion(
            session_start=session_start,
            session_exit=driven.session_exit,
            lifecycle_state=driven.session_exit.lifecycle_state,
            recovery_generations=recovery_generations,
            fresh_final_reference=reference,
            finalized_evidence=finalized,
            final_evidence_eligible=self._final_evidence_eligible,
            evidence_block_reasons=tuple(self._evidence_block_reasons),
        )

    @staticmethod
    def _blocked_finalization_cause(
        error: SupervisorError,
    ) -> BlockedFinalizationCause:
        if error.reason == "RUNTIME_BINDING_FAILED":
            return BlockedFinalizationCause.RUNTIME_BINDING_FAILED
        return BlockedFinalizationCause.CREDENTIAL_CHILD_CRASH

    def _finalize_crash_after_reap(
        self,
        *,
        cause: BlockedFinalizationCause,
    ) -> None:
        if self._finalization_attempted:
            return
        if self._active is not None:
            self._kill_active_child()
        elif self._launching_child is not None:
            self._kill_launching_child()
        reap = self._last_reap_attestation
        if reap is None:
            return
        self._finalization_attempted = True
        try:
            finalized = self._finalizer.finalize_blocked(
                reap,
                cause=cause,
            )
        except BaseException:
            raise SupervisorError("BLOCKED_FINALIZATION_FAILED") from None
        if type(finalized) is not FinalizedEvidence:
            raise SupervisorError("FINALIZED_EVIDENCE_REQUIRED")

    def _drive_active_session(self) -> _DrivenSessionExit | None:
        """Drive one primary or recovery generation; ``None`` means exact reap."""

        while True:
            active = self._require_command_ready()
            action = plan_next(active.lifecycle_state)
            if action.kind is ActionKind.READ:
                if action.freshness is Freshness.PRE_INTENT:
                    self.reserve_pre_intent_read()
                else:
                    phase = self._issue_phase(active, action)
                    try:
                        projected = self._projector.build_exact_read(
                            action=action,
                            permit=phase.permit,
                            permit_projection=phase.projection,
                            state=active.lifecycle_state,
                            authority=active.authority,
                        )
                        if type(projected) is not ExactReadProjection:
                            raise SupervisorError("EXACT_READ_PROJECTION_REQUIRED")
                    except BaseException:
                        self._kill_active_child()
                        raise SupervisorError("EXACT_READ_PROJECTION_FAILED") from None
                    self._reserve_exact_read_with_phase(
                        active=active,
                        action=action,
                        phase=phase,
                        reserved_request=projected.reserved_request,
                        reservation_proof=projected.reservation_proof,
                    )
                outcome = self.receive_read()
                state = active.lifecycle_state
                if type(outcome) is DurableReadFailureOutcome:
                    resolution = self._projector.project_read_failure(
                        action=action,
                        failure=outcome.failure,
                        state=state,
                    )
                elif type(outcome) is DurableReadOutcome:
                    resolution = self._projector.project_pre_intent_success(
                        action=action,
                        result=outcome.transport_result,
                        state=state,
                    )
                elif type(outcome) is UndurableExactReadOutcome:
                    completion = self._projector.complete_exact_read(
                        action=action,
                        result=outcome.transport_result,
                        observed_at=self._observed_at(),
                        state=state,
                    )
                    self._accept_exact_read_completion(completion)
                    resolution = completion.resolution
                else:  # pragma: no cover - closed union.
                    raise SupervisorError("READ_OUTCOME_INVALID")
                self.apply_http_resolution(resolution=resolution)
                continue
            if action.kind is ActionKind.PERSIST_INTENT:
                primary = self._require_primary_authority()
                persisted = self._projector.persist_intent(
                    authority=primary,
                    state=active.lifecycle_state,
                )
                self._bind_persisted_intent(primary, persisted, active.child.generation)
                self.record_persisted_intent(evidence_sha256=persisted.file_sha256)
                continue
            if action.kind is ActionKind.BIND_INTENT:
                reference = self._intent_reference
                if reference is None:
                    raise SupervisorError("INTENT_REFERENCE_REQUIRED")
                self.bind_intent(reference=reference)
                continue
            if action.kind in {
                ActionKind.CREATE,
                ActionKind.CANCEL,
                ActionKind.EMERGENCY_CLOSE,
            }:
                phase = self._issue_phase(active, action)
                try:
                    projected_mutation = self._projector.build_mutation(
                        action=action,
                        permit=phase.permit,
                        permit_projection=phase.projection,
                        state=active.lifecycle_state,
                        authority=active.authority,
                    )
                    if type(projected_mutation) is not MutationProjection:
                        raise SupervisorError("MUTATION_PROJECTION_REQUIRED")
                except BaseException:
                    self._kill_active_child()
                    raise SupervisorError("MUTATION_PROJECTION_FAILED") from None
                outcome = self._dispatch_mutation_with_phase(
                    active=active,
                    action=action,
                    phase=phase,
                    reserved_request=projected_mutation.reserved_request,
                    reservation_proof=projected_mutation.reservation_proof,
                    attempt=projected_mutation.attempt,
                )
                resolution = self._projector.project_mutation_outcome(
                    action=action,
                    attempt=outcome.attempt,
                    frontier=outcome.frontier,
                    dispatch_result=outcome.dispatch_result,
                    observed_at=self._observed_at(),
                    state=active.lifecycle_state,
                )
                if self._active is None:
                    return None
                self.apply_http_resolution(resolution=resolution)
                continue
            if action.kind is ActionKind.COMPLETE_CHILD:
                final_reference: FreshFinalReference | None = None
                if active.lifecycle_state.block_reason is None:
                    final_reference = self._projector.fresh_child_final_reference(
                        state=active.lifecycle_state,
                        evidence_log=self._evidence_log,
                    )
                    if type(final_reference) is not FreshFinalReference:
                        raise SupervisorError("FRESH_FINAL_REFERENCE_REQUIRED")
                return _DrivenSessionExit(
                    session_exit=self.finish(
                        final_evidence_sha256=(
                            None
                            if final_reference is None
                            else final_reference.final_evidence_sha256
                        )
                    ),
                    fresh_final_reference=final_reference,
                )
            raise SupervisorError("UNEXPECTED_LIFECYCLE_ACTION")

    def start_primary(
        self,
        *,
        authority: SessionAuthority,
    ) -> SessionStart:
        """Launch generation one and establish its exact primary authority."""

        self._require_launch_gate()
        if type(authority) is not SessionAuthority or authority.generation != 1:
            raise SupervisorError("PRIMARY_SESSION_BINDING_INVALID")
        child = self._launch(authority.generation, GenerationCapability.PRIMARY)
        try:
            self._validate_credential_ready(self._receive(child), child)
            self._execution_journal.establish_session_authority(authority)
            projection = self._projector.project_primary(authority)
            self._validate_projection_deadline(projection.timing.lifecycle_deadline)
            command = SessionInitCommand(
                authority=authority,
                capability=GenerationCapability.PRIMARY,
                execution_journal_path=self._execution_journal.path.resolve(),
            )
            child.channel.send("SESSION_INIT", command.to_payload())
            self._validate_session_ready(self._receive(child), child, authority)
            state = start_primary(projection)
            kernel = self._new_kernel(child)
        except BaseException:
            self._kill_launching_child()
            raise SupervisorError("PRIMARY_SESSION_START_FAILED") from None
        self._launching_child = None
        self._active = _ActiveSession(
            child=child,
            authority=authority,
            lifecycle_state=state,
            kernel=kernel,
        )
        self._primary_authority = authority
        return SessionStart(
            generation=child.generation,
            capability=child.capability,
            authority_sha256=authority.authority_sha256,
            reconstruction_sha256=state.reconstruction_sha256,
        )

    def start_recovery(
        self,
        *,
        primary_authority: SessionAuthority,
    ) -> SessionStart:
        """Launch only after reap from a concrete live-journal reconstruction."""

        self._require_launch_gate()
        if type(primary_authority) is not SessionAuthority:
            raise SupervisorError("RECOVERY_SESSION_BINDING_INVALID")
        try:
            persisted_intent = load_persisted_intent(
                self._execution_journal.path.parent / "intent.json"
            )
            source = self._projector.select_recovery_source(
                primary_authority=primary_authority,
                persisted_intent=persisted_intent,
            )
        except BaseException:
            raise SupervisorError("RECOVERY_SOURCE_REPLAY_FAILED") from None
        generation = self._process_lifecycle_journal.last_generation + 1
        child = self._launch(generation, GenerationCapability.RECOVERY)
        try:
            self._validate_credential_ready(self._receive(child), child)
            if source.source_attempt_id is None:
                record = self._execution_journal.issue_intent_bound_recovery_authority(
                    primary_authority_sha256=primary_authority.authority_sha256,
                )
                authority = getattr(record.event, "authority", None)
                if type(authority) is not IntentBoundRecoveryAuthority:
                    raise SupervisorError("RECOVERY_AUTHORITY_INVALID")
            else:
                record = self._execution_journal.issue_recovery_session_authority(
                    primary_authority_sha256=primary_authority.authority_sha256,
                    source_attempt_id=source.source_attempt_id,
                )
                authority = getattr(record.event, "authority", None)
                if type(authority) is not RecoverySessionAuthority:
                    raise SupervisorError("RECOVERY_AUTHORITY_INVALID")
            if authority.generation != generation:
                raise SupervisorError("RECOVERY_AUTHORITY_GENERATION_MISMATCH")
            projection = self._projector.project_recovery(authority, persisted_intent)
            self._validate_projection_deadline(projection.timing.lifecycle_deadline)
            recovery_reference = self._replay_intent_reference(
                primary_authority=primary_authority,
                persisted_intent=persisted_intent,
                generation=generation,
            )
            command = SessionInitCommand(
                authority=authority,
                capability=GenerationCapability.RECOVERY,
                execution_journal_path=self._execution_journal.path.resolve(),
                recovery_reference=recovery_reference,
            )
            child.channel.send("SESSION_INIT", command.to_payload())
            self._validate_session_ready(self._receive(child), child, authority)
            state = resume_recovery(projection)
            if state.capability is not Capability.RECOVERY_ONLY:
                raise SupervisorError("RECOVERY_CAPABILITY_INVALID")
            kernel = self._new_kernel(child)
        except BaseException:
            self._kill_launching_child()
            raise SupervisorError("RECOVERY_SESSION_START_FAILED") from None
        self._launching_child = None
        self._active = _ActiveSession(
            child=child,
            authority=authority,
            lifecycle_state=state,
            kernel=kernel,
        )
        self._persisted_intent = persisted_intent
        self._intent_reference = recovery_reference
        return SessionStart(
            generation=child.generation,
            capability=child.capability,
            authority_sha256=authority.authority_sha256,
            reconstruction_sha256=state.reconstruction_sha256,
        )

    def _require_primary_authority(self) -> SessionAuthority:
        authority = self._primary_authority
        if type(authority) is not SessionAuthority:
            raise SupervisorError("PRIMARY_AUTHORITY_REQUIRED")
        return authority

    def _bind_persisted_intent(
        self,
        primary_authority: SessionAuthority,
        persisted_intent: PersistedIntent,
        generation: int,
    ) -> IntentBindingReference:
        if (
            type(primary_authority) is not SessionAuthority
            or type(persisted_intent) is not PersistedIntent
        ):
            raise SupervisorError("PERSISTED_INTENT_BINDING_REQUIRED")
        try:
            record = self._execution_journal.bind_persisted_intent(
                primary_authority.authority_sha256,
                persisted_intent,
            )
            binding = getattr(record.event, "binding", None)
            if (
                type(binding) is not IntentChainBinding
                or binding.session_authority_sha256 != primary_authority.authority_sha256
                or binding.intent_sha256 != persisted_intent.intent.intent_sha256
                or binding.intent_file_sha256 != persisted_intent.file_sha256
            ):
                raise SupervisorError("INTENT_CHAIN_BINDING_INVALID")
            reference = IntentBindingReference.from_binding(
                binding,
                intent_path=persisted_intent.path,
                generation=generation,
            )
        except BaseException:
            raise SupervisorError("INTENT_CHAIN_BINDING_FAILED") from None
        self._persisted_intent = persisted_intent
        self._intent_reference = reference
        return reference

    def _replay_intent_reference(
        self,
        *,
        primary_authority: SessionAuthority,
        persisted_intent: PersistedIntent,
        generation: int,
    ) -> IntentBindingReference:
        bindings = tuple(
            getattr(record.event, "binding", None)
            for record in self._execution_journal.records()
            if type(getattr(record.event, "binding", None)) is IntentChainBinding
            and record.event.binding.session_authority_sha256 == primary_authority.authority_sha256
        )
        if (
            len(bindings) != 1
            or bindings[0].intent_sha256 != persisted_intent.intent.intent_sha256
            or bindings[0].intent_file_sha256 != persisted_intent.file_sha256
        ):
            raise SupervisorError("INTENT_CHAIN_REPLAY_MISMATCH")
        return IntentBindingReference.from_binding(
            bindings[0],
            intent_path=persisted_intent.path,
            generation=generation,
        )

    def record_persisted_intent(self, *, evidence_sha256: str) -> LifecycleState:
        """Project the credential-free local persistence step into lifecycle state."""

        active = self._require_command_ready()
        action = plan_next(active.lifecycle_state)
        if action.kind is not ActionKind.PERSIST_INTENT or not _is_sha256(evidence_sha256):
            raise SupervisorError("PERSIST_INTENT_ACTION_REQUIRED")
        active.lifecycle_state = apply_local(
            active.lifecycle_state,
            action,
            LocalResolution(
                action_sha256=action.action_sha256,
                evidence_sha256=evidence_sha256,
                disposition=LocalDisposition.SUCCEEDED,
            ),
        )
        return active.lifecycle_state

    def bind_intent(self, *, reference: IntentBindingReference) -> LifecycleState:
        """Consume a process permit before sending the exact BIND command."""

        active = self._require_command_ready()
        if type(reference) is not IntentBindingReference:
            raise SupervisorError("INTENT_REFERENCE_REQUIRED")
        action = plan_next(active.lifecycle_state)
        if action.kind is not ActionKind.BIND_INTENT:
            raise SupervisorError("BIND_INTENT_ACTION_REQUIRED")
        phase = self._issue_phase(active, action)
        active.lifecycle_state = self._consume_local_phase(active.lifecycle_state, phase)
        command = BindIntentCommand(reference=reference, phase_permit=phase.permit)
        try:
            active.child.channel.send("BIND_INTENT", command.to_payload())
            message = self._receive(active.child)
            self._validate_intent_bound(message, active, reference)
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("BIND_INTENT_FAILED") from None
        active.lifecycle_state = apply_local(
            active.lifecycle_state,
            action,
            LocalResolution(
                action_sha256=action.action_sha256,
                evidence_sha256=reference.binding.binding_sha256,
                disposition=LocalDisposition.SUCCEEDED,
            ),
        )
        return active.lifecycle_state

    def reserve_pre_intent_read(self) -> PendingRead:
        """fsync the exact pre-intent reservation before sending READ."""

        active = self._require_command_ready()
        if (
            type(active.authority) is not SessionAuthority
            or active.lifecycle_state.capability is not Capability.PRIMARY
        ):
            raise SupervisorError("PRE_INTENT_PRIMARY_ONLY")
        action = plan_next(active.lifecycle_state)
        if action.kind is not ActionKind.READ or action.path is None:
            raise SupervisorError("READ_ACTION_REQUIRED")
        phase = self._issue_phase(active, action)
        reserved_state = reserve_http(
            active.lifecycle_state,
            action,
            phase.projection,
        )
        try:
            elapsed = self._elapsed(reserved_state)
            prepared = self._execution_journal.reserve_pre_intent_read(
                authority_sha256=active.authority.authority_sha256,
                path=action.path,
                parameters=dict(action.parameters),
                elapsed_seconds=elapsed,
                deadline_ns=_seconds_to_ns(phase.permit.absolute_deadline),
                retry_index=action.retry_index,
            )
            command = ReadCommand.from_pre_intent(
                prepared.reservation,
                phase_permit=phase.permit,
            )
            pending = PendingRead(
                action=action,
                phase=phase,
                command=command,
                pre_intent=prepared,
                exact_read_proof=None,
            )
            active.child.channel.send("READ", command.to_payload())
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("PRE_INTENT_READ_DISPATCH_FAILED") from None
        active.lifecycle_state = reserved_state
        self._pending_read = pending
        return pending

    def reserve_exact_read(
        self,
        *,
        reserved_request: ReservedRequest,
        reservation_proof: ReadReservationProof,
    ) -> PendingRead:
        """fsync an exact post-intent GET and its proof before sending READ."""

        active = self._require_command_ready()
        action = plan_next(active.lifecycle_state)
        if (
            action.kind is not ActionKind.READ
            or type(reserved_request) is not ReservedRequest
            or type(reservation_proof) is not ReadReservationProof
        ):
            raise SupervisorError("EXACT_READ_BINDING_INVALID")
        phase = self._issue_phase(active, action)
        return self._reserve_exact_read_with_phase(
            active=active,
            action=action,
            phase=phase,
            reserved_request=reserved_request,
            reservation_proof=reservation_proof,
        )

    def _reserve_exact_read_with_phase(
        self,
        *,
        active: _ActiveSession,
        action: PlannedAction,
        phase: IssuedPhase,
        reserved_request: ReservedRequest,
        reservation_proof: ReadReservationProof,
    ) -> PendingRead:
        deadline_ns = _seconds_to_ns(phase.permit.absolute_deadline)
        try:
            if (
                reservation_proof.generation != active.child.generation
                or reservation_proof.deadline_ns != deadline_ns
            ):
                raise SupervisorError("EXACT_READ_PHASE_MISMATCH")
            reservation_proof.validate_reserved_request(reserved_request)
            reserved_state = reserve_http(active.lifecycle_state, action, phase.projection)
            self._execution_journal.record_exact_request_reservation(
                authority_sha256=active.authority.authority_sha256,
                generation=active.child.generation,
                deadline_ns=deadline_ns,
                reserved_request=reserved_request,
            )
            self._execution_journal.record_read_prepared(reservation_proof)
            command = ReadCommand.from_intent_bound(
                reserved_request,
                proof=reservation_proof,
                phase_permit=phase.permit,
            )
            pending = PendingRead(
                action=action,
                phase=phase,
                command=command,
                pre_intent=None,
                exact_read_proof=reservation_proof,
            )
            active.child.channel.send("READ", command.to_payload())
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("EXACT_READ_DISPATCH_FAILED") from None
        active.lifecycle_state = reserved_state
        self._pending_read = pending
        return pending

    def receive_read(
        self,
    ) -> DurableReadOutcome | DurableReadFailureOutcome | UndurableExactReadOutcome:
        """Receive one typed READ result; exact reads remain blocked until journaled."""

        active = self._require_active()
        pending = self._pending_read
        if pending is None:
            raise SupervisorError("PENDING_READ_REQUIRED")
        try:
            message = self._receive(active.child)
            if message.kind == "READ_FAILURE":
                failure = self._parse_read_failure(message, pending)
                if pending.command.binding_kind is ReadBindingKind.PRE_INTENT:
                    prepared = pending.pre_intent
                    if prepared is None:  # pragma: no cover - dataclass invariant.
                        raise SupervisorError("PRE_INTENT_RESERVATION_REQUIRED")
                    record = self._execution_journal.record_pre_intent_read_failure(
                        reservation_sha256=prepared.reservation.reservation_sha256,
                        failure=failure.failure_kind,
                        io_may_have_occurred=failure.io_may_have_occurred,
                        observed_at_ns=self._observed_ns(),
                    )
                else:
                    proof = pending.exact_read_proof
                    if proof is None:  # pragma: no cover - dataclass invariant.
                        raise SupervisorError("EXACT_READ_PROOF_REQUIRED")
                    record = self._execution_journal.record_exact_read_failure(
                        request_sha256=proof.request_sha256,
                        failure=failure.failure_kind,
                        io_may_have_occurred=failure.io_may_have_occurred,
                        observed_at_ns=self._observed_ns(),
                    )
                self._pending_read = None
                self._undurable_exact_read = None
                return DurableReadFailureOutcome(
                    pending=pending,
                    failure=failure,
                    journal_record_sequence=record.sequence,
                    journal_record_digest=record.digest,
                )
            result = self._parse_read_result(message, pending)
            if pending.command.binding_kind is ReadBindingKind.PRE_INTENT:
                prepared = pending.pre_intent
                if prepared is None:  # pragma: no cover - dataclass invariant.
                    raise SupervisorError("PRE_INTENT_RESERVATION_REQUIRED")
                record = self._execution_journal.record_pre_intent_read_result(
                    reservation_sha256=prepared.reservation.reservation_sha256,
                    result_sha256=result.result_sha256,
                    observed_at_ns=self._observed_ns(),
                )
                outcome: DurableReadOutcome | UndurableExactReadOutcome = DurableReadOutcome(
                    pending=pending,
                    transport_result=result,
                    journal_record_sequence=record.sequence,
                    journal_record_digest=record.digest,
                    result_proof_sha256=record.digest,
                )
                self._pending_read = None
                return outcome
            outcome = UndurableExactReadOutcome(
                pending=pending,
                transport_result=result,
            )
            self._undurable_exact_read = outcome
            return outcome
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("READ_RESULT_DURABILITY_FAILED") from None

    def record_exact_read_result(self, *, proof: ReadResultProof) -> DurableReadOutcome:
        """Durably close the exact read before another child command is legal."""

        if type(proof) is not ReadResultProof:
            raise SupervisorError("READ_RESULT_PROOF_REQUIRED")
        awaiting = self._undurable_exact_read
        if awaiting is None or self._pending_read is None:
            raise SupervisorError("UNDURABLE_EXACT_READ_REQUIRED")
        pending = awaiting.pending
        reservation_proof = pending.exact_read_proof
        if (
            reservation_proof is None
            or proof.request_sha256 != awaiting.transport_result.request_sha256
            or proof.result_sha256 != awaiting.transport_result.result_sha256
            or proof.generation != pending.command.generation
            or proof.monotonic_sequence != reservation_proof.monotonic_sequence
        ):
            raise SupervisorError("READ_RESULT_PROOF_MISMATCH")
        try:
            record = self._execution_journal.record_read_result(proof)
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("READ_RESULT_DURABILITY_FAILED") from None
        self._pending_read = None
        self._undurable_exact_read = None
        return DurableReadOutcome(
            pending=pending,
            transport_result=awaiting.transport_result,
            journal_record_sequence=record.sequence,
            journal_record_digest=record.digest,
            result_proof_sha256=proof.result_proof_sha256,
        )

    def _accept_exact_read_completion(
        self,
        completion: ExactReadCompletion,
    ) -> DurableReadOutcome:
        """Accept only the concrete projector's already-durable exact result."""

        awaiting = self._undurable_exact_read
        pending = self._pending_read
        if (
            type(completion) is not ExactReadCompletion
            or awaiting is None
            or pending is None
            or awaiting.pending != pending
        ):
            raise SupervisorError("EXACT_READ_COMPLETION_REQUIRED")
        proof = completion.result_proof
        reservation_proof = pending.exact_read_proof
        records = self._execution_journal.records()
        result_record_sequence = completion.result_record.sequence
        result_record_is_anchored = (
            0 < result_record_sequence <= len(records)
            and records[result_record_sequence - 1] == completion.result_record
        )
        resolution_proof_sha256 = completion.resolution.result_proof_sha256
        observation_is_exact = True
        if resolution_proof_sha256 != proof.result_proof_sha256:
            observation_index = result_record_sequence
            observation_is_exact = (
                result_record_is_anchored
                and len(records) == observation_index + 1
                and records[observation_index].sequence == result_record_sequence + 1
                and type(records[observation_index].event) is _ReconciliationObserved
                and type(records[observation_index].event.observation) is ReconciliationObservation
                and records[observation_index].event.observation.read_result_proof_sha256
                == proof.result_proof_sha256
                and records[observation_index].event.observation.observation_sha256
                == resolution_proof_sha256
            )
        if (
            reservation_proof is None
            or not records
            or not result_record_is_anchored
            or not observation_is_exact
            or proof.request_sha256 != awaiting.transport_result.request_sha256
            or proof.result_sha256 != awaiting.transport_result.result_sha256
            or proof.generation != pending.command.generation
            or proof.monotonic_sequence != reservation_proof.monotonic_sequence
        ):
            self._kill_active_child()
            raise SupervisorError("EXACT_READ_COMPLETION_MISMATCH")
        self._pending_read = None
        self._undurable_exact_read = None
        return DurableReadOutcome(
            pending=pending,
            transport_result=awaiting.transport_result,
            journal_record_sequence=completion.result_record.sequence,
            journal_record_digest=completion.result_record.digest,
            result_proof_sha256=proof.result_proof_sha256,
        )

    def apply_http_resolution(
        self,
        *,
        resolution: ReadResolution | MutationResolution,
    ) -> LifecycleState:
        """Apply only a sanitized typed economic projection after durability."""

        active = self._require_active()
        if type(resolution) not in {ReadResolution, MutationResolution}:
            raise SupervisorError("TYPED_HTTP_RESOLUTION_REQUIRED")
        if self._pending_read is not None or self._undurable_exact_read is not None:
            raise SupervisorError("READ_RESULT_NOT_DURABLE")
        active.lifecycle_state = resolve_http(active.lifecycle_state, resolution)
        return active.lifecycle_state

    def dispatch_mutation(
        self,
        *,
        reserved_request: ReservedRequest,
        reservation_proof: MutationReservationProof,
        attempt: MutationAttempt,
    ) -> MutationDispatchOutcome:
        """Reserve, PREPARE, durably authorize GO, then conservatively settle."""

        active = self._require_command_ready()
        action = plan_next(active.lifecycle_state)
        expected_kind = {
            ActionKind.CREATE: MutationKind.CREATE,
            ActionKind.CANCEL: MutationKind.CANCEL,
            ActionKind.EMERGENCY_CLOSE: MutationKind.EMERGENCY_CLOSE,
        }.get(action.kind)
        if (
            expected_kind is None
            or type(reserved_request) is not ReservedRequest
            or type(reservation_proof) is not MutationReservationProof
            or type(attempt) is not MutationAttempt
            or attempt.kind is not expected_kind
        ):
            raise SupervisorError("MUTATION_ACTION_BINDING_INVALID")
        if (
            active.lifecycle_state.capability is Capability.RECOVERY_ONLY
            and attempt.kind is MutationKind.CREATE
        ):
            raise SupervisorError("RECOVERY_CREATE_FORBIDDEN")
        phase = self._issue_phase(active, action)
        return self._dispatch_mutation_with_phase(
            active=active,
            action=action,
            phase=phase,
            reserved_request=reserved_request,
            reservation_proof=reservation_proof,
            attempt=attempt,
        )

    def _dispatch_mutation_with_phase(
        self,
        *,
        active: _ActiveSession,
        action: PlannedAction,
        phase: IssuedPhase,
        reserved_request: ReservedRequest,
        reservation_proof: MutationReservationProof,
        attempt: MutationAttempt,
    ) -> MutationDispatchOutcome:
        deadline_ns = _seconds_to_ns(phase.permit.absolute_deadline)
        try:
            if (
                attempt.generation != active.child.generation
                or attempt.deadline_ns != deadline_ns
                or attempt.reservation_sha256 != reserved_request.request_sha256
                or reservation_proof.generation != active.child.generation
                or reservation_proof.deadline_ns != deadline_ns
            ):
                raise SupervisorError("MUTATION_PHASE_BINDING_INVALID")
            reservation_proof.validate_dispatch_binding(reserved_request, attempt)
        except BaseException:
            self._kill_active_child()
            raise SupervisorError("MUTATION_RESERVATION_BINDING_INVALID") from None
        reserved_state = reserve_http(active.lifecycle_state, action, phase.projection)
        prepared = False
        dispatch_result: DispatchResult | None = None
        try:
            self._execution_journal.record_exact_request_reservation(
                authority_sha256=active.authority.authority_sha256,
                generation=active.child.generation,
                deadline_ns=deadline_ns,
                reserved_request=reserved_request,
            )
            self._execution_journal.record_mutation_reservation(reservation_proof)
            prepared_dispatch = active.kernel.prepare(
                attempt,
                reserved_request=reserved_request,
                phase_permit=phase.permit,
            )
            prepared = True
            self._evidence_log.append_prepared(attempt.attempt_id)
            command = active.kernel.authorize_go(prepared_dispatch)
            active.kernel.send_go(command)
            message = self._receive(active.child)
            dispatch_result = self._parse_dispatch_result(
                message,
                attempt=attempt,
                reserved_request=reserved_request,
            )
            frontier = active.kernel.confirm_result(command, message)
            if frontier is not FrontierState.CONFIRMED:
                raise DispatchKernelError("CONFIRMED_FRONTIER_REQUIRED")
            evidence_record = self._evidence_log.append_result(dispatch_result)
        except BaseException as failure:
            active.lifecycle_state = reserved_state
            if not prepared:
                self._kill_active_child()
                raise SupervisorError("MUTATION_PREPARE_FAILED") from None
            return self._settle_mutation_failure(
                active=active,
                action=action,
                attempt=attempt,
                dispatch_result=dispatch_result,
                failure=failure,
            )
        active.lifecycle_state = reserved_state
        return MutationDispatchOutcome(
            action=action,
            attempt=attempt,
            frontier=FrontierState.CONFIRMED,
            dispatch_result=dispatch_result,
            evidence_record=evidence_record,
            reap_attestation=None,
            evidence_eligible=self._final_evidence_eligible,
        )

    def finish(self, *, final_evidence_sha256: str | None) -> SessionExit:
        """Validate child pre-exit before exact reap; final adjudication is external."""

        active = self._require_command_ready()
        blocked = active.lifecycle_state.block_reason is not None
        if (blocked and final_evidence_sha256 is not None) or (
            not blocked and not _is_sha256(final_evidence_sha256)
        ):
            raise SupervisorError("FRESH_FINAL_EVIDENCE_REQUIRED")
        action = plan_next(active.lifecycle_state)
        if action.kind is not ActionKind.COMPLETE_CHILD:
            raise SupervisorError("COMPLETE_CHILD_ACTION_REQUIRED")
        phase = self._issue_phase(active, action)
        active.lifecycle_state = self._consume_local_phase(active.lifecycle_state, phase)
        command = SessionFinishCommand(
            generation=active.child.generation,
            final_state=(SessionFinalState.BLOCKED if blocked else SessionFinalState.COMPLETED),
            final_evidence_sha256=final_evidence_sha256,
            phase_permit=phase.permit,
        )
        try:
            active.child.channel.send("SESSION_FINISH", command.to_payload())
            message = self._receive(active.child)
            self._validate_session_finished(
                message,
                active,
                final_state=command.final_state,
                final_evidence_sha256=final_evidence_sha256,
            )
            preexit_path, preexit_sha256, loaded_module_paths = self._wait_for_child_preexit(
                active, phase
            )
            attestation = self._process_supervisor.reap(active.child)
            self._active = None
            self._pending_read = None
            self._undurable_exact_read = None
            if (
                type(attestation) is not ReapAttestation
                or attestation.generation != active.child.generation
                or attestation.returncode != 0
                or attestation.signal is not None
            ):
                raise SupervisorError("CLEAN_CHILD_REAP_REQUIRED")
            self._last_reap_attestation = attestation
            self._last_lifecycle_state = active.lifecycle_state
        except BaseException:
            if self._active is not None:
                self._kill_active_child()
            raise SupervisorError("SESSION_FINISH_FAILED") from None
        try:
            post_runtime_snapshot = verify_runtime_unchanged(
                self._projector.runtime_snapshot,
                loaded_project_module_paths=loaded_module_paths,
            )
            if type(post_runtime_snapshot) is not RuntimeSnapshot:
                raise SupervisorError("POST_RUNTIME_SNAPSHOT_REQUIRED")
        except BaseException:
            raise SupervisorError("RUNTIME_BINDING_FAILED") from None
        active.lifecycle_state = apply_local(
            active.lifecycle_state,
            action,
            LocalResolution(
                action_sha256=action.action_sha256,
                evidence_sha256=preexit_sha256,
                disposition=LocalDisposition.SUCCEEDED,
            ),
        )
        self._last_lifecycle_state = active.lifecycle_state
        return SessionExit(
            generation=active.child.generation,
            capability=active.child.capability,
            child_pre_exit_path=preexit_path,
            child_pre_exit_sha256=preexit_sha256,
            loaded_project_module_paths=loaded_module_paths,
            post_runtime_snapshot=post_runtime_snapshot,
            reap_attestation=attestation,
            lifecycle_state=active.lifecycle_state,
        )

    def abort(self) -> ReapAttestation:
        """Kill and exactly reap the active child without inventing a venue result."""

        if self._active is None and self._launching_child is None:
            raise SupervisorError("ACTIVE_CHILD_REQUIRED")
        if self._active is not None:
            return self._kill_active_child()
        return self._kill_launching_child()

    def _launch(
        self,
        generation: int,
        capability: GenerationCapability,
    ) -> ManagedChild:
        child = self._process_supervisor.launch(self._workload, generation=generation)
        if (
            type(child) is not ManagedChild
            or child.workload is not self._workload
            or child.generation != generation
            or child.capability is not capability
        ):
            raise SupervisorError("FIXED_CHILD_ADMISSION_INVALID")
        self._launching_child = child
        return child

    def _new_kernel(self, child: ManagedChild) -> DispatchKernel:
        return DispatchKernel(
            journal=self._execution_journal,
            process_journal_path=self._process_lifecycle_journal.path,
            channel=child.channel,
            lifecycle_deadline=self._process_supervisor.deadline,
        )

    def _require_launch_gate(self) -> None:
        if self._active is not None or self._launching_child is not None:
            raise SupervisorError("OLD_CREDENTIAL_CHILD_NOT_REAPED")
        if self._pending_read is not None or self._undurable_exact_read is not None:
            raise SupervisorError("OLD_SESSION_STATE_NOT_SETTLED")

    def _require_active(self) -> _ActiveSession:
        active = self._active
        if active is None:
            raise SupervisorError("ACTIVE_SESSION_REQUIRED")
        return active

    def _require_command_ready(self) -> _ActiveSession:
        active = self._require_active()
        if self._pending_read is not None or self._undurable_exact_read is not None:
            raise SupervisorError("PREVIOUS_READ_NOT_SETTLED")
        return active

    def _validate_projection_deadline(self, deadline: Decimal) -> None:
        if Decimal(str(self._process_supervisor.deadline.at)) != deadline:
            raise SupervisorError("LIFECYCLE_DEADLINE_BINDING_MISMATCH")

    def _issue_phase(self, active: _ActiveSession, action: PlannedAction) -> IssuedPhase:
        local = action.local_limit_seconds or _CONTROL_LOCAL_LIMIT_SECONDS
        before = self._process_supervisor.deadline.clock()
        cap = min(float(action.absolute_deadline_cap), self._process_supervisor.deadline.at)
        available = cap - before
        if available <= 0:
            raise SupervisorError("PHASE_DEADLINE_EXHAUSTED")
        local_limit = min(float(local), available)
        permit = self._process_supervisor.issue_phase_permit(
            active.child,
            local_limit=local_limit,
        )
        issued_at = self._process_supervisor.deadline.clock()
        projection = PhasePermitProjection(
            generation=permit.generation,
            sequence=permit.sequence,
            action_sha256=action.action_sha256,
            lifecycle_deadline=Decimal(str(permit.lifecycle_deadline)),
            issued_at=Decimal(str(issued_at)),
            absolute_deadline=Decimal(str(permit.absolute_deadline)),
            local_limit_seconds=Decimal(str(local_limit)),
        )
        if projection.absolute_deadline > action.absolute_deadline_cap:
            raise SupervisorError("PHASE_ACTION_CAP_EXCEEDED")
        return IssuedPhase(action=action, permit=permit, projection=projection)

    @staticmethod
    def _consume_local_phase(state: LifecycleState, phase: IssuedPhase) -> LifecycleState:
        projection = phase.projection
        if (
            projection.generation != state.generation
            or projection.sequence != state.last_permit_sequence + 1
            or projection.action_sha256 != phase.action.action_sha256
            or projection.lifecycle_deadline != state.timing.lifecycle_deadline
            or projection.issued_at < state.timing.lifecycle_started_at
            or projection.issued_at >= state.timing.lifecycle_deadline
            or projection.absolute_deadline <= projection.issued_at
            or projection.absolute_deadline > state.timing.lifecycle_deadline
            or projection.local_limit_seconds > _CONTROL_LOCAL_LIMIT_SECONDS
        ):
            raise SupervisorError("LOCAL_PHASE_PERMIT_INVALID")
        return replace(state, last_permit_sequence=projection.sequence)

    def _elapsed(self, state: LifecycleState) -> Decimal:
        return self._observe_lifecycle_clock() - state.timing.lifecycle_started_at

    def _observed_ns(self) -> int:
        return int(self._observe_lifecycle_clock() * _NS_PER_SECOND)

    def _observed_at(self) -> Decimal:
        return self._observe_lifecycle_clock()

    def _observe_lifecycle_clock(self) -> Decimal:
        try:
            observed = Decimal(str(self._process_supervisor.deadline.clock()))
        except BaseException:
            raise SupervisorError("LIFECYCLE_CLOCK_INVALID") from None
        started = Decimal(str(self._process_lifecycle_journal.lifecycle_started_at))
        deadline = Decimal(str(self._process_lifecycle_journal.lifecycle_deadline))
        if not observed.is_finite() or observed < started or observed > deadline:
            raise SupervisorError("LIFECYCLE_DEADLINE_EXHAUSTED")
        return observed

    def _settle_mutation_failure(
        self,
        *,
        active: _ActiveSession,
        action: PlannedAction,
        attempt: MutationAttempt,
        dispatch_result: DispatchResult | None,
        failure: BaseException,
    ) -> MutationDispatchOutcome:
        try:
            frontier = self._execution_journal.frontier(attempt.attempt_id)
        except BaseException:
            frontier = FrontierState.GO_DURABLE
        if frontier is FrontierState.CONFIRMED:
            self._block_final_evidence("SECONDARY_RESULT_EVIDENCE_DURABILITY_FAILED")
            attestation = self._kill_active_child()
            return MutationDispatchOutcome(
                action=action,
                attempt=attempt,
                frontier=FrontierState.CONFIRMED,
                dispatch_result=dispatch_result,
                evidence_record=None,
                reap_attestation=attestation,
                evidence_eligible=False,
            )
        attestation = self._kill_active_child()
        failure_kind = _dispatch_failure(failure)
        settled = active.kernel.settle_failure(
            attempt,
            failure=failure_kind,
            reap_attestation=attestation,
        )
        if settled not in {FrontierState.NOT_DISPATCHED, FrontierState.UNKNOWN}:
            raise SupervisorError("MUTATION_FRONTIER_SETTLEMENT_INVALID")
        evidence_record: ExecutionEvidenceRecord | None = None
        evidence_eligible = self._final_evidence_eligible
        try:
            evidence_record = self._evidence_log.append_failure(attempt.attempt_id)
        except BaseException:
            self._block_final_evidence("SECONDARY_FAILURE_EVIDENCE_DURABILITY_FAILED")
            evidence_eligible = False
        return MutationDispatchOutcome(
            action=action,
            attempt=attempt,
            frontier=settled,
            dispatch_result=dispatch_result,
            evidence_record=evidence_record,
            reap_attestation=attestation,
            evidence_eligible=evidence_eligible,
        )

    def _kill_active_child(self) -> ReapAttestation:
        active = self._require_active()
        try:
            attestation = self._process_supervisor.kill_and_reap(active.child)
        except BaseException:
            raise SupervisorError("PROCESS_REAP_NOT_PROVEN") from None
        if (
            type(attestation) is not ReapAttestation
            or attestation.generation != active.child.generation
        ):
            raise SupervisorError("EXACT_REAP_ATTESTATION_REQUIRED")
        self._last_lifecycle_state = active.lifecycle_state
        self._last_reap_attestation = attestation
        self._active = None
        self._pending_read = None
        self._undurable_exact_read = None
        return attestation

    def _kill_launching_child(self) -> ReapAttestation:
        child = self._launching_child
        if child is None:
            raise SupervisorError("ACTIVE_CHILD_REQUIRED")
        try:
            attestation = self._process_supervisor.kill_and_reap(child)
        except BaseException:
            raise SupervisorError("PROCESS_REAP_NOT_PROVEN") from None
        if type(attestation) is not ReapAttestation or attestation.generation != child.generation:
            raise SupervisorError("EXACT_REAP_ATTESTATION_REQUIRED")
        self._last_reap_attestation = attestation
        self._launching_child = None
        return attestation

    def _block_final_evidence(self, reason: str) -> None:
        self._final_evidence_eligible = False
        if reason not in self._evidence_block_reasons:
            self._evidence_block_reasons.append(reason)

    @staticmethod
    def _receive(child: ManagedChild) -> IPCMessage:
        message = child.channel.receive()
        if type(message) is not IPCMessage or message.version != IPC_VERSION:
            raise SupervisorError("IPC_MESSAGE_INVALID")
        return message

    @staticmethod
    def _validate_credential_ready(message: IPCMessage, child: ManagedChild) -> None:
        expected = {
            "schema_version",
            "status",
            "generation",
            "capability",
            "guard_installed",
        }
        if (
            message.kind != "CREDENTIAL_READY"
            or set(message.payload) != expected
            or message.payload["schema_version"] != _CHILD_SCHEMA_VERSION
            or message.payload["status"] != "READY"
            or message.payload["generation"] != child.generation
            or message.payload["capability"] != child.capability.value
            or message.payload["guard_installed"] is not True
        ):
            raise SupervisorError("CREDENTIAL_READY_INVALID")

    @staticmethod
    def _validate_session_ready(
        message: IPCMessage,
        child: ManagedChild,
        authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    ) -> None:
        expected = {
            "schema_version",
            "status",
            "generation",
            "capability",
            "authority_sha256",
        }
        if child.capability is GenerationCapability.RECOVERY:
            expected.add("intent_sha256")
        if (
            message.kind != "SESSION_READY"
            or set(message.payload) != expected
            or message.payload["schema_version"] != SESSION_SCHEMA_VERSION
            or message.payload["status"] != "READY"
            or message.payload["generation"] != child.generation
            or message.payload["capability"] != child.capability.value
            or message.payload["authority_sha256"] != authority.authority_sha256
            or (
                child.capability is GenerationCapability.RECOVERY
                and message.payload["intent_sha256"] != authority.source_intent_sha256
            )
        ):
            raise SupervisorError("SESSION_READY_INVALID")

    @staticmethod
    def _validate_intent_bound(
        message: IPCMessage,
        active: _ActiveSession,
        reference: IntentBindingReference,
    ) -> None:
        expected = {
            "schema_version",
            "status",
            "generation",
            "authority_sha256",
            "binding_sha256",
            "intent_sha256",
            "intent_file_sha256",
        }
        if (
            message.kind != "INTENT_BOUND"
            or set(message.payload) != expected
            or message.payload["schema_version"] != SESSION_SCHEMA_VERSION
            or message.payload["status"] != "BOUND"
            or message.payload["generation"] != active.child.generation
            or message.payload["authority_sha256"] != active.authority.authority_sha256
            or message.payload["binding_sha256"] != reference.binding.binding_sha256
            or message.payload["intent_sha256"] != reference.binding.intent_sha256
            or message.payload["intent_file_sha256"] != reference.binding.intent_file_sha256
        ):
            raise SupervisorError("INTENT_BOUND_RESULT_INVALID")

    @staticmethod
    def _parse_read_result(message: IPCMessage, pending: PendingRead) -> TransportResult:
        expected = {
            "schema_version",
            "binding_kind",
            "generation",
            "reservation_sha256",
            "read_proof_sha256",
            "result",
        }
        if (
            message.kind != "READ_RESULT"
            or set(message.payload) != expected
            or message.payload["schema_version"] != SESSION_SCHEMA_VERSION
            or message.payload["binding_kind"] != pending.command.binding_kind.value
            or message.payload["generation"] != pending.command.generation
        ):
            raise SupervisorError("READ_RESULT_INVALID")
        result = transport_result_from_payload(message.payload["result"])
        command = pending.command
        if command.binding_kind is ReadBindingKind.PRE_INTENT:
            reservation = command.pre_intent_reservation
            expected_request = reservation.reservation_sha256 if reservation is not None else None
            expected_logical = (
                reservation.logical_request_sha256 if reservation is not None else None
            )
            expected_proof = None
        else:
            reservation = command.reserved_request
            proof = command.read_proof
            expected_request = reservation.request_sha256 if reservation is not None else None
            expected_logical = (
                reservation.logical_request_sha256 if reservation is not None else None
            )
            expected_proof = proof.proof_sha256 if proof is not None else None
        if (
            message.payload["reservation_sha256"] != expected_request
            or message.payload["read_proof_sha256"] != expected_proof
            or result.request_sha256 != expected_request
            or result.logical_request_sha256 != expected_logical
        ):
            raise SupervisorError("READ_RESULT_BINDING_MISMATCH")
        return result

    @staticmethod
    def _parse_read_failure(message: IPCMessage, pending: PendingRead) -> ReadFailureResult:
        if message.kind != "READ_FAILURE":
            raise SupervisorError("READ_FAILURE_REQUIRED")
        failure = ReadFailureResult.from_payload(message.payload)
        command = pending.command
        if command.binding_kind is ReadBindingKind.PRE_INTENT:
            reservation = command.pre_intent_reservation
            expected_request = reservation.reservation_sha256 if reservation is not None else None
            expected_proof = None
        else:
            reservation = command.reserved_request
            proof = command.read_proof
            expected_request = reservation.request_sha256 if reservation is not None else None
            expected_proof = proof.proof_sha256 if proof is not None else None
        if (
            failure.binding_kind is not command.binding_kind
            or failure.generation != command.generation
            or failure.reservation_sha256 != expected_request
            or failure.read_proof_sha256 != expected_proof
        ):
            raise SupervisorError("READ_FAILURE_BINDING_MISMATCH")
        return failure

    @staticmethod
    def _parse_dispatch_result(
        message: IPCMessage,
        *,
        attempt: MutationAttempt,
        reserved_request: ReservedRequest,
    ) -> DispatchResult:
        if message.kind != "RESULT":
            raise SupervisorError("DISPATCH_RESULT_REQUIRED")
        result = DispatchResult.from_payload(message.payload)
        if (
            result.attempt_id != attempt.attempt_id
            or result.generation != attempt.generation
            or result.kind is not attempt.kind
            or result.client_id != attempt.client_id
            or result.transport_result.request_sha256 != reserved_request.request_sha256
            or result.transport_result.logical_request_sha256
            != reserved_request.logical_request_sha256
        ):
            raise SupervisorError("DISPATCH_RESULT_BINDING_MISMATCH")
        return result

    @staticmethod
    def _validate_session_finished(
        message: IPCMessage,
        active: _ActiveSession,
        *,
        final_state: SessionFinalState,
        final_evidence_sha256: str | None,
    ) -> None:
        expected = {
            "schema_version",
            "status",
            "generation",
            "final_state",
            "final_evidence_sha256",
        }
        if (
            message.kind != "SESSION_FINISHED"
            or set(message.payload) != expected
            or message.payload["schema_version"] != SESSION_SCHEMA_VERSION
            or message.payload["status"] != "FINISHED"
            or message.payload["generation"] != active.child.generation
            or message.payload["final_state"] != final_state.value
            or message.payload["final_evidence_sha256"] != final_evidence_sha256
        ):
            raise SupervisorError("SESSION_FINISHED_INVALID")

    def _wait_for_child_preexit(
        self,
        active: _ActiveSession,
        phase: IssuedPhase,
    ) -> tuple[Path, str, tuple[Path, ...]]:
        path = self._execution_journal.path.resolve().parent / "child-pre-exit.json"
        while True:
            try:
                info = path.lstat()
                break
            except FileNotFoundError:
                remaining = (
                    phase.permit.absolute_deadline - self._process_supervisor.deadline.clock()
                )
                if remaining <= 0:
                    raise SupervisorError("CHILD_PREEXIT_MISSING") from None
                time.sleep(min(0.01, remaining))
            except OSError:
                raise SupervisorError("CHILD_PREEXIT_INVALID") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SupervisorError("CHILD_PREEXIT_MODE_INVALID")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError):
            raise SupervisorError("CHILD_PREEXIT_INVALID") from None
        expected = {
            "capability",
            "generation",
            "local_exit_pending",
            "loaded_project_modules",
            "redaction_status",
            "schema_version",
            "session_finished",
            "status",
        }
        if (
            type(payload) is not dict
            or set(payload) != expected
            or payload["schema_version"] != _CHILD_PREEXIT_SCHEMA_VERSION
            or payload["status"] != "CHILD_COMPLETE"
            or payload["generation"] != active.child.generation
            or payload["capability"] != active.child.capability.value
            or payload["local_exit_pending"] is not True
            or payload["session_finished"] is not True
            or payload["redaction_status"] != "VERIFIED"
            or type(payload["loaded_project_modules"]) is not list
            or any(type(item) is not str for item in payload["loaded_project_modules"])
        ):
            raise SupervisorError("CHILD_PREEXIT_PAYLOAD_INVALID")
        loaded_module_paths = tuple(Path(item) for item in payload["loaded_project_modules"])
        if not loaded_module_paths or any(
            not item.is_absolute()
            or str(item.absolute()) != str(item)
            or "\0" in str(item)
            or len(str(item)) > 4096
            for item in loaded_module_paths
        ):
            raise SupervisorError("CHILD_PREEXIT_MODULE_PATHS_INVALID")
        return path, hashlib.sha256(raw).hexdigest(), loaded_module_paths


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _seconds_to_ns(value: float) -> int:
    if type(value) is not float or value <= 0:
        raise SupervisorError("DEADLINE_INVALID")
    return int(value * _NS_PER_SECOND)


def _dispatch_failure(failure: BaseException) -> DispatchFailure:
    if isinstance(failure, (TimeoutError, socket.timeout)):
        return DispatchFailure.TIMEOUT
    if isinstance(failure, (EOFError, BrokenPipeError, ConnectionResetError)):
        return DispatchFailure.EOF
    if isinstance(failure, IPCProtocolError):
        reason = str(failure).upper()
        if "TRUNCATED" in reason:
            return DispatchFailure.TRUNCATED
        if "OVERSIZED" in reason:
            return DispatchFailure.OVERSIZED
        if "VERSION" in reason:
            return DispatchFailure.VERSION
        if "SEQUENCE" in reason:
            return DispatchFailure.SEQUENCE
        if "DIGEST" in reason:
            return DispatchFailure.DIGEST
        return DispatchFailure.CORRUPT
    if isinstance(failure, ExecutionEvidenceLogError):
        return DispatchFailure.RESULT_DURABILITY
    if isinstance(failure, (DispatchKernelError, SupervisorError)):
        return DispatchFailure.RESULT_DURABILITY
    return DispatchFailure.FAULT
