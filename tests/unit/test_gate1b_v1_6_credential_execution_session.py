from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import global_quant.gate1b.credential_transport as credential_transport_module
import global_quant.gate1b.process_boundary as process_boundary_module
from global_quant.gate1b.credential_execution_session import (
    BindIntentCommand,
    CredentialExecutionSession,
    CredentialExecutionSessionError,
    IntentBindingReference,
    ReadCommand,
    ReadFailureKind,
    ReadFailureResult,
    SessionFinishCommand,
    SessionInitCommand,
    transport_result_from_payload,
)
from global_quant.gate1b.credential_transport import (
    CredentialTransportError,
    ProcessBoundCredentialTransport,
    ResponseKind,
    TransportResult,
)
from global_quant.gate1b.durable_intent import (
    PersistedIntent,
    load_persisted_intent,
    persist_intent,
)
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    DurableGenerationAdmission,
    ExecutionJournal,
    GenerationCapability,
    IntentBoundRecoveryAuthority,
    IntentChainBinding,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    PreIntentReadReservation,
    ProcessReapReceipt,
    ReadKind,
    ReadPurpose,
    ReadReservationProof,
    RecoverySessionAuthority,
    SessionAuthority,
)
from global_quant.gate1b.execution_journal import (
    ReadFailureKind as JournalReadFailureKind,
)
from global_quant.gate1b.execution_kernel import GoCommand
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    DurableIntent,
    LimitOrderFilters,
    MutationLedger,
    OrderDerivationProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
)
from global_quant.gate1b.process_boundary import (
    AbsoluteDeadline,
    ChildBootstrap,
    CredentialWorkloadKind,
    IPCCodec,
    IPCMessage,
    IPCProtocolError,
    PhaseDeadlinePermit,
    ProcessIdentity,
)

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "4" * 40
SESSION_NONCE = "5" * 16
NOW_SECONDS = 100.0
LIFECYCLE_SECONDS = 110.0
DEADLINE_NS = 104_000_000_000


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _message(kind: str, payload: dict[str, object], *, sequence: int) -> IPCMessage:
    codec = IPCCodec()
    return codec.decode(
        codec.encode(kind, payload, sequence=sequence),
        expected_sequence=sequence,
    )


class FakeChannel:
    def __init__(self, incoming: list[IPCMessage]) -> None:
        self.incoming = list(incoming)
        self.sent: list[tuple[str, dict[str, object]]] = []

    def receive(self) -> IPCMessage:
        if not self.incoming:
            raise IPCProtocolError("IPC_EOF")
        return self.incoming.pop(0)

    def send(self, kind: str, payload) -> None:
        self.sent.append((kind, dict(payload)))


class FakeHardDeadline:
    def __init__(self) -> None:
        self.permits: list[PhaseDeadlinePermit] = []
        self.intact_checks = 0

    def _arm_permit(self, permit: PhaseDeadlinePermit, *, generation: int) -> None:
        assert permit.generation == generation
        self.permits.append(permit)

    def assert_intact(self) -> None:
        self.intact_checks += 1


class FakeNetworkGate:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.authority_issued = False
        self.guard_attestation = (
            process_boundary_module._CREDENTIAL_GUARD_ATTESTATION if ready else None
        )


class FakeSignedClient:
    base_url = DEMO_HTTP_ORIGIN

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, dict[str, str]]] = []
        self.deadlines: list[int] = []
        self.failure: CredentialTransportError | None = None
        self.exception: BaseException | None = None

    def sign_request(
        self,
        method,
        path,
        payload=None,
        ratelimiter_keys=None,
        *,
        absolute_deadline_ns: int,
    ) -> bytes:
        self.calls.append((method, path, dict(payload or {})))
        self.deadlines.append(absolute_deadline_ns)
        if self.exception is not None:
            raise self.exception
        if self.failure is not None:
            raise self.failure
        if path == "/fapi/v1/time":
            return b'{"serverTime":1700000000000}'
        parameters = dict(payload or {})
        client_id = parameters.get(
            "newClientOrderId",
            parameters.get("origClientOrderId", ""),
        )
        if path == "/fapi/v1/order" and str(method) == "HttpMethod.GET":
            return json.dumps(
                {
                    "clientOrderId": client_id,
                    "executedQty": "0",
                    "orderId": 12345,
                    "origQty": "0.005",
                    "positionSide": "BOTH",
                    "price": "2000.00",
                    "reduceOnly": False,
                    "side": "BUY",
                    "status": "NEW",
                    "symbol": SYMBOL,
                    "timeInForce": "GTX",
                    "type": "LIMIT",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        return json.dumps(
            {"clientOrderId": client_id, "orderId": 12345, "status": "NEW"},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")


def _authority(*, generation: int = 1) -> SessionAuthority:
    return SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=generation,
    )


def _permit(
    *,
    generation: int = 1,
    sequence: int = 0,
    absolute_deadline: float = 104.0,
) -> PhaseDeadlinePermit:
    return PhaseDeadlinePermit.issue(
        generation=generation,
        sequence=sequence,
        absolute_deadline=absolute_deadline,
        lifecycle_deadline=LIFECYCLE_SECONDS,
    )


def _bootstrap(
    channel: FakeChannel,
    *,
    capability: GenerationCapability = GenerationCapability.PRIMARY,
    network_ready: bool = True,
    generation: int = 1,
) -> ChildBootstrap:
    return ChildBootstrap(
        generation=generation,
        capability=capability,
        deadline=AbsoluteDeadline(LIFECYCLE_SECONDS, clock=lambda: NOW_SECONDS),
        hard_deadline=FakeHardDeadline(),  # type: ignore[arg-type]
        identity=ProcessIdentity(
            pid=122 + generation,
            ppid=1,
            pgid=122 + generation,
            sid=122 + generation,
            start_token=f"test:{generation}",
        ),
        channel=channel,  # type: ignore[arg-type]
        workload_kind=CredentialWorkloadKind.TEST_ONLY,
        _network_gate=FakeNetworkGate(ready=network_ready),  # type: ignore[arg-type]
    )


def _init_payload(
    *,
    capability: GenerationCapability = GenerationCapability.PRIMARY,
    authority_generation: int = 1,
    execution_journal_path: Path = Path("/tmp/gate1b-test-execution.jsonl"),
) -> dict[str, object]:
    return SessionInitCommand(
        authority=_authority(generation=authority_generation),
        capability=capability,
        execution_journal_path=execution_journal_path,
    ).to_payload()


def _intent(*, persisted: bool = False) -> DurableIntent:
    filters = LimitOrderFilters(
        min_price=Decimal("1000.00"),
        max_price=Decimal("5000.00"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        percent_price_multiplier_down=Decimal("0.85"),
        percent_price_multiplier_up=Decimal("1.05"),
    )
    derivation = OrderDerivationProof(
        best_bid=Decimal("2000.00"),
        best_ask=Decimal("2000.01"),
        mark_price=Decimal("2000.00"),
        filters=filters,
        filter_snapshot_sha256="6" * 64,
        filter_contract_sha256=filters.canonical_sha256,
        book_age_ms=Decimal("100"),
        mark_age_ms=Decimal("100"),
        observed_elapsed_seconds=Decimal("1"),
    )
    return DurableIntent(
        authorization_id=AUTHORIZATION_ID,
        protocol_commit="1" * 40,
        protocol_tag_object="2" * 40,
        protocol_sha256="3" * 64,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        order_derivation=derivation,
        persisted=persisted,
    )


def _persisted(tmp_path: Path) -> PersistedIntent:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return persist_intent(root / "intent.json", _intent())


def _binding_reference(receipt: PersistedIntent) -> IntentBindingReference:
    path_sha256 = hashlib.sha256(
        json.dumps(
            {"path": str(receipt.path.absolute())},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    binding = IntentChainBinding.build(
        session_authority_sha256=_authority().authority_sha256,
        intent_sha256=receipt.intent.intent_sha256,
        intent_file_sha256=receipt.file_sha256,
        intent_path_sha256=path_sha256,
        pre_intent_chain_sha256=_sha("pre-intent-chain"),
        last_ledger_sha256=_sha("last-ledger"),
    )
    return IntentBindingReference.from_binding(
        binding,
        intent_path=receipt.path,
        generation=1,
    )


def _pre_intent_schedule(client_id: str) -> tuple[tuple[str, dict[str, str]], ...]:
    recv_window = {"recvWindow": str(RECEIVE_WINDOW_MS)}
    return (
        ("/fapi/v1/time", {}),
        ("/fapi/v1/positionSide/dual", recv_window),
        ("/fapi/v1/symbolConfig", {"symbol": SYMBOL, **recv_window}),
        ("/fapi/v2/account", recv_window),
        ("/fapi/v1/openOrders", recv_window),
        ("/fapi/v1/openAlgoOrders", recv_window),
        ("/fapi/v1/exchangeInfo", {}),
        (
            "/fapi/v1/order",
            {"symbol": SYMBOL, "origClientOrderId": client_id, **recv_window},
        ),
        ("/fapi/v1/userTrades", {"symbol": SYMBOL, **recv_window}),
        ("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL}),
        ("/fapi/v1/premiumIndex", {"symbol": SYMBOL}),
    )


def _exact_primary_go(
    tmp_path: Path,
    permit: PhaseDeadlinePermit,
    *,
    parameter_overrides: dict[str, str] | None = None,
):
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    admission = journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("primary-process"),
        ),
        GenerationCapability.PRIMARY,
    )
    authority = _authority()
    journal.establish_session_authority(authority)
    for sequence, (path, parameters) in enumerate(
        _pre_intent_schedule(authority.client_id),
        start=1,
    ):
        prepared = journal.reserve_pre_intent_read(
            authority_sha256=authority.authority_sha256,
            path=path,
            parameters=parameters,
            elapsed_seconds=Decimal(sequence),
            deadline_ns=DEADLINE_NS + sequence,
            retry_index=0,
        )
        journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=_sha(f"pre-intent-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    persisted = _persisted(tmp_path)
    binding_record = journal.bind_persisted_intent(authority.authority_sha256, persisted)
    reference = IntentBindingReference.from_binding(
        binding_record.event.binding,
        intent_path=persisted.path,
        generation=1,
    )
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    parameters = persisted.intent.probe_payload
    if parameter_overrides is not None:
        parameters = {**parameters, **parameter_overrides}
    reserved = ReservedRequest(
        ledger=MutationLedger(
            total_http_requests=previous.total_http_requests + 1,
            create_requests=1,
            cancel_requests=previous.cancel_requests,
            emergency_close_requests=previous.emergency_close_requests,
            read_retry_requests=previous.read_retry_requests,
            post_create_read_requests=previous.post_create_read_requests,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("12"),
            retryable_read_sha256=None,
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=Decimal("12"),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=DEADLINE_NS,
        client_id=authority.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    journal.record_mutation_reservation(proof)
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    prepared_record = journal.prepare_attempt(attempt)
    go_record = journal.record_go(attempt.attempt_id)
    command = GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=1,
        lifecycle_deadline_ns=int(LIFECYCLE_SECONDS * 1_000_000_000),
        local_deadline_ns=int(permit.absolute_deadline * 1_000_000_000),
        go_deadline_ns=DEADLINE_NS,
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=prepared_record.digest,
        go_record_digest=go_record.digest,
    )
    return journal, admission, authority, persisted, reference, command


def _reap_generation(
    journal: ExecutionJournal,
    admission,
    *,
    generation: int,
    process_identity_sha256: str,
) -> None:
    journal.reap_generation(
        ProcessReapReceipt(
            generation=generation,
            process_identity_sha256=process_identity_sha256,
            admission_record_sequence=admission.sequence,
            admission_record_digest=admission.digest,
            returncode=-9,
            signal=9,
            local_process_quiesced=True,
            venue_mutation_absent_proven=False,
        )
    )


def _attempt_recovery_fixture(
    tmp_path: Path,
    *,
    generation: int = 2,
):
    journal, admission1, primary, persisted, reference1, source_command = _exact_primary_go(
        tmp_path, _permit(sequence=0)
    )
    _reap_generation(
        journal,
        admission1,
        generation=1,
        process_identity_sha256=_sha("primary-process"),
    )
    journal.resolve_after_reap(source_command.attempt.attempt_id, BoundaryResult.EOF)
    admission = journal.admit_generation(
        DurableGenerationAdmission(
            generation=2,
            process_identity_sha256=_sha("recovery-process-2"),
        ),
        GenerationCapability.RECOVERY,
    )
    issued = journal.issue_recovery_session_authority(
        primary_authority_sha256=primary.authority_sha256,
        source_attempt_id=source_command.attempt.attempt_id,
    )
    authority = issued.event.authority
    if generation == 3:
        _reap_generation(
            journal,
            admission,
            generation=2,
            process_identity_sha256=_sha("recovery-process-2"),
        )
        admission = journal.admit_generation(
            DurableGenerationAdmission(
                generation=3,
                process_identity_sha256=_sha("recovery-process-3"),
            ),
            GenerationCapability.RECOVERY,
        )
        issued = journal.issue_recovery_session_authority(
            primary_authority_sha256=primary.authority_sha256,
            source_attempt_id=source_command.attempt.attempt_id,
        )
        authority = issued.event.authority
    reference = IntentBindingReference.from_binding(
        reference1.binding,
        intent_path=persisted.path,
        generation=generation,
    )
    init = SessionInitCommand(
        authority=authority,
        capability=GenerationCapability.RECOVERY,
        execution_journal_path=journal.path.absolute(),
        recovery_reference=reference,
    )
    return (
        journal,
        admission,
        primary,
        authority,
        persisted,
        reference,
        source_command,
        init,
    )


def _intent_bound_recovery_fixture(tmp_path: Path):
    journal = ExecutionJournal(tmp_path / "intent-bound-recovery.jsonl")
    admission1 = journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("intent-bound-primary-process"),
        ),
        GenerationCapability.PRIMARY,
    )
    primary = _authority()
    journal.establish_session_authority(primary)
    for sequence, (path, parameters) in enumerate(
        _pre_intent_schedule(primary.client_id),
        start=1,
    ):
        prepared = journal.reserve_pre_intent_read(
            authority_sha256=primary.authority_sha256,
            path=path,
            parameters=parameters,
            elapsed_seconds=Decimal(sequence),
            deadline_ns=DEADLINE_NS + sequence,
            retry_index=0,
        )
        journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=_sha(f"intent-bound-pre-intent-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    persisted = _persisted(tmp_path)
    binding_record = journal.bind_persisted_intent(primary.authority_sha256, persisted)
    _reap_generation(
        journal,
        admission1,
        generation=1,
        process_identity_sha256=_sha("intent-bound-primary-process"),
    )
    admission2 = journal.admit_generation(
        DurableGenerationAdmission(
            generation=2,
            process_identity_sha256=_sha("intent-bound-recovery-process-2"),
        ),
        GenerationCapability.RECOVERY,
    )
    issued = journal.issue_intent_bound_recovery_authority(
        primary_authority_sha256=primary.authority_sha256,
    )
    authority = issued.event.authority
    reference = IntentBindingReference.from_binding(
        binding_record.event.binding,
        intent_path=persisted.path,
        generation=2,
    )
    init = SessionInitCommand(
        authority=authority,
        capability=GenerationCapability.RECOVERY,
        execution_journal_path=journal.path.absolute(),
        recovery_reference=reference,
    )
    return journal, admission2, primary, authority, persisted, reference, init


def _recovery_order_read(
    journal: ExecutionJournal,
    *,
    primary: SessionAuthority,
    recovery: RecoverySessionAuthority,
    source: MutationAttempt,
    generation: int,
    permit: PhaseDeadlinePermit,
) -> ReadCommand:
    previous = journal.request_ledger_snapshot(primary.authority_sha256).last_ledger
    elapsed = previous.last_elapsed_seconds + Decimal("1")
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            post_create_read_requests=previous.post_create_read_requests + 1,
            last_elapsed_seconds=elapsed,
        ),
        intent_sha256=source.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=tuple(
            sorted(
                {
                    "symbol": SYMBOL,
                    "origClientOrderId": source.client_id,
                    "recvWindow": str(RECEIVE_WINDOW_MS),
                }.items()
            )
        ),
        elapsed_seconds=elapsed,
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=recovery.authority_sha256,
        generation=generation,
        deadline_ns=DEADLINE_NS,
        reserved_request=reserved,
    )
    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        generation=generation,
        deadline_ns=DEADLINE_NS,
        source_attempt_id=source.attempt_id,
        client_id=source.client_id,
        authorization_id=source.authorization_id,
    )
    journal.record_read_prepared(proof)
    return ReadCommand.from_intent_bound(
        reserved,
        proof=proof,
        phase_permit=permit,
    )


def _recovery_create_go(
    source_command: GoCommand,
    *,
    generation: int,
    permit: PhaseDeadlinePermit,
) -> GoCommand:
    reserved = source_command.reserved_request
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=generation,
        deadline_ns=DEADLINE_NS,
        client_id=source_command.attempt.client_id,
        authorization_id=source_command.attempt.authorization_id,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=generation,
        retry_index=0,
        deadline_ns=DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=source_command.attempt.authorization_id,
        intent_sha256=source_command.attempt.intent_sha256,
        runtime_commit=source_command.attempt.runtime_commit,
        session_nonce=source_command.attempt.session_nonce,
    )
    return _go_command(
        attempt=attempt,
        reserved=reserved,
        proof=proof,
        permit=permit,
        prepared_digest=_sha(f"forged-recovery-{generation}-prepared"),
        go_digest=_sha(f"forged-recovery-{generation}-go"),
    )


def _go_command(
    *,
    attempt: MutationAttempt,
    reserved: ReservedRequest,
    proof: MutationReservationProof,
    permit: PhaseDeadlinePermit,
    prepared_digest: str,
    go_digest: str,
) -> GoCommand:
    return GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=attempt.generation,
        lifecycle_deadline_ns=int(LIFECYCLE_SECONDS * 1_000_000_000),
        local_deadline_ns=int(permit.absolute_deadline * 1_000_000_000),
        go_deadline_ns=min(
            DEADLINE_NS,
            int(permit.absolute_deadline * 1_000_000_000),
        ),
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=prepared_digest,
        go_record_digest=go_digest,
    )


def _unjournaled_cleanup_go(
    tmp_path: Path,
    permit: PhaseDeadlinePermit,
    *,
    kind: MutationKind,
):
    journal, admission, authority, persisted, reference, create_command = _exact_primary_go(
        tmp_path, permit
    )
    source = create_command.attempt
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    precondition_sha256 = _sha(f"unconsumed-{kind.value}")
    if kind is MutationKind.CANCEL:
        request_purpose = RequestPurpose.CANCEL
        mutation_purpose = MutationPurpose.PRIMARY_CANCEL
        method = "DELETE"
        parameters = persisted.intent.cancel_parameters
        client_id = source.client_id
        ledger = MutationLedger(
            total_http_requests=previous.total_http_requests + 1,
            create_requests=previous.create_requests,
            cancel_requests=previous.cancel_requests + 1,
            emergency_close_requests=previous.emergency_close_requests,
            read_retry_requests=previous.read_retry_requests,
            post_create_read_requests=previous.post_create_read_requests,
            stage=RequestStage.CANCEL_ATTEMPTED,
            last_elapsed_seconds=Decimal("13"),
            retryable_read_sha256=None,
        )
    else:
        request_purpose = RequestPurpose.EMERGENCY_CLOSE
        mutation_purpose = MutationPurpose.PRIMARY_EMERGENCY_CLOSE
        method = "POST"
        base_quantity = persisted.intent.probe_order.quantity
        parameters = {
            **persisted.intent.emergency_close_payload(base_quantity),
            "quantity": format(base_quantity + Decimal("0.001"), "f"),
        }
        client_id = persisted.intent.emergency_client_order_id
        ledger = MutationLedger(
            total_http_requests=previous.total_http_requests + 1,
            create_requests=previous.create_requests,
            cancel_requests=previous.cancel_requests,
            emergency_close_requests=previous.emergency_close_requests + 1,
            read_retry_requests=previous.read_retry_requests,
            post_create_read_requests=previous.post_create_read_requests,
            stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
            last_elapsed_seconds=Decimal("13"),
            retryable_read_sha256=None,
        )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method=method,
        path="/fapi/v1/order",
        purpose=request_purpose,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=Decimal("13"),
        retry_index=0,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=mutation_purpose,
        generation=1,
        deadline_ns=DEADLINE_NS,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=source.attempt_id,
        precondition_sha256=precondition_sha256,
    )
    attempt = MutationAttempt.build(
        kind=kind,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=(precondition_sha256 if kind is MutationKind.CANCEL else None),
    )
    command = _go_command(
        attempt=attempt,
        reserved=reserved,
        proof=proof,
        permit=permit,
        prepared_digest=_sha(f"forged-{kind.value}-prepared"),
        go_digest=_sha(f"forged-{kind.value}-go"),
    )
    return journal, admission, authority, persisted, reference, command


def _pre_intent_read() -> PreIntentReadReservation:
    return PreIntentReadReservation.build(
        session_authority_sha256=_authority().authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS,
        path="/fapi/v1/time",
        parameters={},
        ledger=MutationLedger(
            total_http_requests=1,
            last_elapsed_seconds=Decimal("1"),
        ),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )


def _journaled_pre_intent_read(
    tmp_path: Path,
) -> tuple[ExecutionJournal, PreIntentReadReservation]:
    journal = ExecutionJournal(tmp_path / "pre-intent-execution.jsonl")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("pre-intent-primary-process"),
        ),
        GenerationCapability.PRIMARY,
    )
    authority = _authority()
    journal.establish_session_authority(authority)
    prepared = journal.reserve_pre_intent_read(
        authority_sha256=authority.authority_sha256,
        path="/fapi/v1/time",
        parameters={},
        elapsed_seconds=Decimal("1"),
        deadline_ns=DEADLINE_NS,
        retry_index=0,
    )
    return journal, prepared.reservation


def _bound_read(intent_sha256: str) -> tuple[ReservedRequest, ReadReservationProof]:
    ledger = MutationLedger(
        total_http_requests=12,
        last_elapsed_seconds=Decimal("2"),
    )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/time",
        purpose=RequestPurpose.READ,
        parameters=(),
        elapsed_seconds=Decimal("2"),
        retry_index=0,
    )
    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=ReadKind.GENERAL,
        purpose=ReadPurpose.EVIDENCE,
        generation=1,
        deadline_ns=DEADLINE_NS,
        source_attempt_id=None,
        client_id=None,
        authorization_id=AUTHORIZATION_ID,
    )
    return reserved, proof


def _bound_create(
    intent_sha256: str,
) -> tuple[ReservedRequest, MutationReservationProof, MutationAttempt]:
    client_id = _authority().client_id
    ledger = MutationLedger(
        total_http_requests=12,
        create_requests=1,
        stage=RequestStage.CREATE_ATTEMPTED,
        last_elapsed_seconds=Decimal("2"),
    )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(
            sorted(
                {
                    "newClientOrderId": client_id,
                    "newOrderRespType": "ACK",
                    "positionSide": "BOTH",
                    "price": "2000.00",
                    "quantity": "0.005",
                    "recvWindow": str(RECEIVE_WINDOW_MS),
                    "reduceOnly": "false",
                    "side": "BUY",
                    "symbol": SYMBOL,
                    "timeInForce": "GTX",
                    "type": "LIMIT",
                }.items()
            )
        ),
        elapsed_seconds=Decimal("2"),
        retry_index=0,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=DEADLINE_NS,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=None,
    )
    return reserved, proof, attempt


def _go_payload(intent_sha256: str, permit: PhaseDeadlinePermit) -> dict[str, object]:
    reserved, proof, attempt = _bound_create(intent_sha256)
    command = GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=1,
        lifecycle_deadline_ns=int(LIFECYCLE_SECONDS * 1_000_000_000),
        local_deadline_ns=int(permit.absolute_deadline * 1_000_000_000),
        go_deadline_ns=DEADLINE_NS,
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=_sha("prepared"),
        go_record_digest=_sha("go"),
    )
    return command.to_payload()


def _session(
    channel: FakeChannel,
    *,
    capability: GenerationCapability = GenerationCapability.PRIMARY,
    resolver=None,
    network_ready: bool = True,
    generation: int = 1,
) -> tuple[CredentialExecutionSession, FakeSignedClient]:
    signed_client = FakeSignedClient()
    identity = process_boundary_module.read_process_identity(os.getpid())
    assert identity is not None
    authority_bootstrap = ChildBootstrap(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        deadline=AbsoluteDeadline(LIFECYCLE_SECONDS, clock=lambda: NOW_SECONDS),
        hard_deadline=FakeHardDeadline(),  # type: ignore[arg-type]
        identity=identity,
        channel=object(),  # type: ignore[arg-type]
        workload_kind=CredentialWorkloadKind.PRODUCTION,
        _network_gate=process_boundary_module._NetworkGate(
            ready=True,
            guard_attestation=process_boundary_module._CREDENTIAL_GUARD_ATTESTATION,
        ),
        _bootstrap_attestation=process_boundary_module._CHILD_BOOTSTRAP_ATTESTATION,
    )
    transport = ProcessBoundCredentialTransport(
        signed_client,
        io_authority=authority_bootstrap.issue_io_authority(),
        _construction_token=(credential_transport_module._PRODUCTION_TRANSPORT_CONSTRUCTION_TOKEN),
        monotonic_ns=lambda: int(NOW_SECONDS * 1_000_000_000),
    )
    return (
        CredentialExecutionSession(
            bootstrap=_bootstrap(
                channel,
                capability=capability,
                network_ready=network_ready,
                generation=generation,
            ),
            transport=transport,
            verified_intent_resolver=(resolver or _resolve_intent),
        ),
        signed_client,
    )


def _pre_intent_result(reservation: PreIntentReadReservation) -> TransportResult:
    return TransportResult.build(
        request_sha256=reservation.reservation_sha256,
        logical_request_sha256=reservation.logical_request_sha256,
        kind=ResponseKind.SERVER_TIME,
        fields=(("serverTime", 1_700_000_000_000),),
    )


def _resolve_intent(reference: IntentBindingReference) -> PersistedIntent:
    return load_persisted_intent(reference.intent_path)


def test_session_init_is_exact_and_projects_only_sanitized_ready_state() -> None:
    channel = FakeChannel([_message("SESSION_INIT", _init_payload(), sequence=0)])
    session, _client = _session(channel)

    session.start()

    assert channel.sent == [
        (
            "SESSION_READY",
            {
                "authority_sha256": _authority().authority_sha256,
                "capability": "PRIMARY",
                "generation": 1,
                "schema_version": "gate1b.credential-execution-session.v1",
                "status": "READY",
            },
        )
    ]


def test_session_init_rejects_extra_field_and_bootstrap_capability_mismatch() -> None:
    payload = _init_payload()
    payload["unexpected"] = "value"
    channel = FakeChannel([_message("SESSION_INIT", payload, sequence=0)])
    session, _client = _session(channel)

    with pytest.raises(CredentialExecutionSessionError, match="SESSION_INIT_FIELDS_INVALID"):
        session.start()

    mismatch_payload = _init_payload()
    mismatch_payload["capability"] = GenerationCapability.RECOVERY.value
    mismatch = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                mismatch_payload,
                sequence=0,
            )
        ]
    )
    session, _client = _session(mismatch)
    with pytest.raises(CredentialExecutionSessionError, match="SESSION_INIT_INVALID"):
        session.start()


def test_pre_intent_read_uses_exact_typed_binding_and_same_phase_permit(
    tmp_path: Path,
) -> None:
    permit = _permit()
    journal, reservation = _journaled_pre_intent_read(tmp_path)
    read = ReadCommand.from_pre_intent(reservation, phase_permit=permit)
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", read.to_payload(), sequence=2),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    assert session.run_next() is True

    assert len(signed_client.calls) == 1
    assert signed_client.calls[0][1:] == ("/fapi/v1/time", {})
    kind, result_payload = channel.sent[-1]
    assert kind == "READ_RESULT"
    result = transport_result_from_payload(result_payload["result"])
    assert result.request_sha256 == reservation.reservation_sha256
    assert result.kind is ResponseKind.SERVER_TIME


def test_pre_intent_read_requires_replayed_durable_prepared_record_before_io(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    permit = _permit()
    read = ReadCommand.from_pre_intent(_pre_intent_read(), phase_permit=permit)
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", read.to_payload(), sequence=2),
        ]
    )
    session, signed_client = _session(channel)
    session.start()

    with pytest.raises(
        CredentialExecutionSessionError,
        match="PRE_INTENT_DURABLE_PREPARED_REQUIRED",
    ):
        session.run_next()

    assert signed_client.calls == []


def test_read_rejects_generation_sequence_deadline_or_digest_drift_before_io() -> None:
    permit = _permit()
    command = ReadCommand.from_pre_intent(_pre_intent_read(), phase_permit=permit)
    base = command.to_payload()

    mutations = (
        {**base, "generation": 2},
        {**base, "phase_permit_sequence": 1},
        {**base, "phase_permit_digest": _sha("wrong")},
        {**base, "deadline_ns": 106 * 1_000_000_000},
    )
    for payload in mutations:
        channel = FakeChannel(
            [
                _message("SESSION_INIT", _init_payload(), sequence=0),
                _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
                _message("READ", payload, sequence=2),
            ]
        )
        session, signed_client = _session(channel)
        session.start()
        with pytest.raises(CredentialExecutionSessionError):
            session.run_next()
        assert signed_client.calls == []


def test_bind_intent_replays_exact_receipt_and_enables_bound_read(tmp_path: Path) -> None:
    persisted = _persisted(tmp_path)
    reference = _binding_reference(persisted)
    bind_permit = _permit(sequence=0)
    read_permit = _permit(sequence=1)
    reserved, proof = _bound_read(persisted.intent.intent_sha256)
    read = ReadCommand.from_intent_bound(
        reserved,
        proof=proof,
        phase_permit=read_permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", read_permit.to_payload(), sequence=3),
            _message("READ", read.to_payload(), sequence=4),
        ]
    )
    session, signed_client = _session(channel)

    session.start()
    assert session.run_next() is True
    assert session.run_next() is True

    assert [item[0] for item in channel.sent] == [
        "SESSION_READY",
        "INTENT_BOUND",
        "READ_RESULT",
    ]
    assert signed_client.calls[0][1] == "/fapi/v1/time"


def test_nominal_binding_or_resolver_mismatch_cannot_grant_bound_capability(
    tmp_path: Path,
) -> None:
    persisted = _persisted(tmp_path)
    reference = _binding_reference(persisted)
    permit = _permit()
    payload = BindIntentCommand(reference, permit).to_payload()
    payload["reference"]["intent_file_sha256"] = _sha("forged")
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("BIND_INTENT", payload, sequence=2),
        ]
    )
    session, _client = _session(channel)
    session.start()
    with pytest.raises(CredentialExecutionSessionError):
        session.run_next()

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = _persisted(other_root)
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, permit).to_payload(),
                sequence=2,
            ),
        ]
    )
    session, _client = _session(channel, resolver=lambda _reference: other)
    session.start()
    with pytest.raises(CredentialExecutionSessionError, match="INTENT_RECEIPT_MISMATCH"):
        session.run_next()


def test_pre_intent_after_bind_and_bound_read_before_bind_are_rejected(
    tmp_path: Path,
) -> None:
    persisted = _persisted(tmp_path)
    bound_reserved, bound_proof = _bound_read(persisted.intent.intent_sha256)
    permit = _permit()
    bound = ReadCommand.from_intent_bound(
        bound_reserved,
        proof=bound_proof,
        phase_permit=permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", bound.to_payload(), sequence=2),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    with pytest.raises(CredentialExecutionSessionError, match="INTENT_BINDING_REQUIRED"):
        session.run_next()
    assert signed_client.calls == []

    bind_permit = _permit(sequence=0)
    read_permit = _permit(sequence=1)
    pre = ReadCommand.from_pre_intent(_pre_intent_read(), phase_permit=read_permit)
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(
                    _binding_reference(persisted),
                    bind_permit,
                ).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", read_permit.to_payload(), sequence=3),
            _message("READ", pre.to_payload(), sequence=4),
        ]
    )
    session, _client = _session(channel)
    session.start()
    session.run_next()
    with pytest.raises(CredentialExecutionSessionError, match="PRE_INTENT_PHASE_CLOSED"):
        session.run_next()


def test_go_uses_buffered_exact_message_and_already_accepted_same_permit(
    tmp_path: Path,
) -> None:
    bind_permit = _permit(sequence=0)
    go_permit = _permit(sequence=1)
    journal, _admission, _authority_value, persisted, reference, command = _exact_primary_go(
        tmp_path, go_permit
    )
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(
                    reference,
                    bind_permit,
                ).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", go_permit.to_payload(), sequence=3),
            _message("GO", command.to_payload(), sequence=4),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    session.run_next()
    assert session.run_next() is True

    assert signed_client.calls[-1][1] == "/fapi/v1/order"
    assert channel.sent[-1][0] == "RESULT"
    transport_payload = channel.sent[-1][1]["transport_result"]
    assert set(transport_payload) == {
        "fields",
        "kind",
        "logical_request_sha256",
        "request_sha256",
        "result_sha256",
    }
    assert transport_payload["fields"] == [
        ["clientOrderId", persisted.intent.client_order_id],
        ["orderIdSha256", _sha("binance-demo-order-id\x0012345")],
        ["status", "NEW"],
    ]
    encoded_result = json.dumps(channel.sent[-1][1], sort_keys=True).casefold()
    for forbidden in ("raw_body", "signed", "signature", "header", "api_key", "secret"):
        assert forbidden not in encoded_result
    assert session.bootstrap.hard_deadline.permits == [bind_permit, go_permit]


@pytest.mark.parametrize(
    "economic_override",
    [
        {"price": "2999.99"},
        {"quantity": "0.006"},
    ],
)
def test_child_reopens_journal_and_rejects_self_consistent_wrong_create_before_io(
    tmp_path: Path,
    economic_override: dict[str, str],
) -> None:
    bind_permit = _permit(sequence=0)
    go_permit = _permit(sequence=1)
    journal, _admission, _authority_value, _persisted_value, reference, command = _exact_primary_go(
        tmp_path,
        go_permit,
        parameter_overrides=economic_override,
    )
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", go_permit.to_payload(), sequence=3),
            _message("GO", command.to_payload(), sequence=4),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    session.run_next()

    with pytest.raises(
        CredentialExecutionSessionError,
        match="MUTATION_EXECUTION_FAILED",
    ):
        session.run_next()

    assert signed_client.calls == []
    assert all(kind != "RESULT" for kind, _payload in channel.sent)


@pytest.mark.parametrize("kind", [MutationKind.CANCEL, MutationKind.EMERGENCY_CLOSE])
def test_child_rejects_unconsumed_cancel_observation_or_close_above_intent_before_io(
    tmp_path: Path,
    kind: MutationKind,
) -> None:
    bind_permit = _permit(sequence=0)
    go_permit = _permit(sequence=1)
    journal, _admission, _authority_value, _persisted_value, reference, command = (
        _unjournaled_cleanup_go(tmp_path, go_permit, kind=kind)
    )
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", go_permit.to_payload(), sequence=3),
            _message("GO", command.to_payload(), sequence=4),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    session.run_next()

    with pytest.raises(CredentialExecutionSessionError, match="MUTATION_EXECUTION_FAILED"):
        session.run_next()

    assert signed_client.calls == []


def test_child_rejects_wrong_cancel_parameters_during_exact_go_decode_before_io(
    tmp_path: Path,
) -> None:
    bind_permit = _permit(sequence=0)
    go_permit = _permit(sequence=1)
    journal, _admission, _authority_value, _persisted_value, reference, command = (
        _unjournaled_cleanup_go(tmp_path, go_permit, kind=MutationKind.CANCEL)
    )
    payload = command.to_payload()
    for pair in payload["reserved_request"]["parameters"]:
        if pair[0] == "origClientOrderId":
            pair[1] = "wrong-client-id"
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", go_permit.to_payload(), sequence=3),
            _message("GO", payload, sequence=4),
        ]
    )
    session, signed_client = _session(channel)
    session.start()
    session.run_next()

    with pytest.raises(CredentialExecutionSessionError, match="GO_COMMAND_INVALID"):
        session.run_next()

    assert signed_client.calls == []


def test_recovery_session_init_replays_exact_intent_and_enters_read_directly(
    tmp_path: Path,
) -> None:
    (
        journal,
        _admission,
        primary,
        recovery,
        persisted,
        _reference,
        source_command,
        init,
    ) = _attempt_recovery_fixture(tmp_path)
    permit = _permit(generation=2)
    read = _recovery_order_read(
        journal,
        primary=primary,
        recovery=recovery,
        source=source_command.attempt,
        generation=2,
        permit=permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", init.to_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", read.to_payload(), sequence=2),
        ]
    )
    session, signed_client = _session(
        channel,
        capability=GenerationCapability.RECOVERY,
        generation=2,
    )

    assert SessionInitCommand.from_payload(init.to_payload()) == init
    session.start()

    assert channel.sent[-1] == (
        "SESSION_READY",
        {
            "authority_sha256": recovery.authority_sha256,
            "capability": "RECOVERY",
            "generation": 2,
            "intent_sha256": persisted.intent.intent_sha256,
            "schema_version": "gate1b.credential-execution-session.v1",
            "status": "READY",
        },
    )
    assert session.run_next() is True
    assert signed_client.calls[0][1] == "/fapi/v1/order"
    assert channel.sent[-1][0] == "READ_RESULT", channel.sent[-1]


def test_recovery_session_forbids_pre_intent_create_and_rebind_before_io(
    tmp_path: Path,
) -> None:
    (
        _journal,
        _admission,
        _primary,
        recovery,
        _persisted,
        reference,
        source_command,
        init,
    ) = _attempt_recovery_fixture(tmp_path)
    pre_permit = _permit(generation=2, sequence=0)
    create_permit = _permit(generation=2, sequence=1)
    bind_permit = _permit(generation=2, sequence=2)
    pre_reservation = PreIntentReadReservation.build(
        session_authority_sha256=recovery.authority_sha256,
        generation=2,
        deadline_ns=DEADLINE_NS,
        path="/fapi/v1/time",
        parameters={},
        ledger=MutationLedger(
            total_http_requests=1,
            last_elapsed_seconds=Decimal("1"),
        ),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )
    create = _recovery_create_go(
        source_command,
        generation=2,
        permit=create_permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", init.to_payload(), sequence=0),
            _message("PHASE_PERMIT", pre_permit.to_payload(), sequence=1),
            _message(
                "READ",
                ReadCommand.from_pre_intent(
                    pre_reservation,
                    phase_permit=pre_permit,
                ).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", create_permit.to_payload(), sequence=3),
            _message("GO", create.to_payload(), sequence=4),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=5),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=6,
            ),
        ]
    )
    session, signed_client = _session(
        channel,
        capability=GenerationCapability.RECOVERY,
        generation=2,
    )
    session.start()

    with pytest.raises(
        CredentialExecutionSessionError,
        match="RECOVERY_PRE_INTENT_FORBIDDEN",
    ):
        session.run_next()
    with pytest.raises(
        CredentialExecutionSessionError,
        match="RECOVERY_CREATE_CAPABILITY_FORBIDDEN",
    ):
        session.run_next()
    with pytest.raises(
        CredentialExecutionSessionError,
        match="RECOVERY_BIND_INTENT_FORBIDDEN",
    ):
        session.run_next()
    assert signed_client.calls == []


def test_recovery_gen3_reissues_same_source_lineage_after_gen2_reap(
    tmp_path: Path,
) -> None:
    (
        _journal,
        _admission,
        _primary,
        recovery3,
        persisted,
        _reference,
        source_command,
        init,
    ) = _attempt_recovery_fixture(tmp_path, generation=3)
    channel = FakeChannel([_message("SESSION_INIT", init.to_payload(), sequence=0)])
    session, _client = _session(
        channel,
        capability=GenerationCapability.RECOVERY,
        generation=3,
    )

    session.start()

    assert recovery3.generation == 3
    assert recovery3.source_attempt_id == source_command.attempt.attempt_id
    assert recovery3.source_intent_sha256 == persisted.intent.intent_sha256
    assert channel.sent[-1][1]["intent_sha256"] == persisted.intent.intent_sha256


def test_intent_bound_recovery_authority_is_typed_and_bound_before_ready(
    tmp_path: Path,
) -> None:
    (
        _journal,
        _admission,
        _primary,
        recovery,
        persisted,
        _reference,
        init,
    ) = _intent_bound_recovery_fixture(tmp_path)
    channel = FakeChannel([_message("SESSION_INIT", init.to_payload(), sequence=0)])
    session, _client = _session(
        channel,
        capability=GenerationCapability.RECOVERY,
        generation=2,
    )

    replayed = SessionInitCommand.from_payload(init.to_payload())
    assert type(replayed.authority) is IntentBoundRecoveryAuthority
    assert replayed == init
    session.start()

    assert recovery.allows_create is False
    assert recovery.allows_mutation is False
    assert channel.sent[-1][1]["intent_sha256"] == persisted.intent.intent_sha256


def test_recovery_generation_cannot_reuse_primary_session_authority() -> None:
    payload = _init_payload(authority_generation=1)
    payload["capability"] = GenerationCapability.RECOVERY.value
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                payload,
                sequence=0,
            ),
        ]
    )
    session, _signed_client = _session(
        channel,
        capability=GenerationCapability.RECOVERY,
        generation=2,
    )

    with pytest.raises(
        CredentialExecutionSessionError,
        match="SESSION_INIT_INVALID",
    ):
        session.start()

    assert channel.sent == []


def test_transport_exception_text_and_credentials_never_enter_error_ipc_or_repr(
    tmp_path: Path,
) -> None:
    permit = _permit()
    journal, reservation = _journaled_pre_intent_read(tmp_path)
    read = ReadCommand.from_pre_intent(reservation, phase_permit=permit)
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=journal.path.absolute()),
                sequence=0,
            ),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", read.to_payload(), sequence=2),
        ]
    )

    session, signed_client = _session(channel)
    signed_client.exception = RuntimeError("credential-canary X-MBX-APIKEY signature=secret")
    session.start()
    assert session.run_next() is True

    kind, payload = channel.sent[-1]
    failure = ReadFailureResult.from_payload(payload)
    assert kind == "READ_FAILURE"
    assert failure.failure_kind is ReadFailureKind.IO_AMBIGUOUS
    assert failure.io_may_have_occurred is True
    assert failure.reservation_sha256 == reservation.reservation_sha256
    assert failure.read_proof_sha256 is None
    rendered = repr(session) + repr(failure) + str(channel.sent)
    assert "credential-canary" not in rendered
    assert "X-MBX-APIKEY" not in rendered
    assert "signature=secret" not in rendered


def test_network_guard_is_required_before_any_read_executor_or_transport() -> None:
    permit = _permit()
    read = ReadCommand.from_pre_intent(_pre_intent_read(), phase_permit=permit)
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("READ", read.to_payload(), sequence=2),
        ]
    )
    session, signed_client = _session(channel, network_ready=False)
    session.start()

    assert session.run_next() is True

    assert signed_client.calls == []
    kind, payload = channel.sent[-1]
    failure = ReadFailureResult.from_payload(payload)
    assert kind == "READ_FAILURE"
    assert failure.failure_kind is ReadFailureKind.NETWORK_GUARD
    assert failure.io_may_have_occurred is False


def test_transport_read_failure_is_typed_and_keeps_exact_bound_proof(
    tmp_path: Path,
) -> None:
    persisted = _persisted(tmp_path)
    reference = _binding_reference(persisted)
    bind_permit = _permit(sequence=0)
    read_permit = _permit(sequence=1)
    reserved, proof = _bound_read(persisted.intent.intent_sha256)
    read = ReadCommand.from_intent_bound(
        reserved,
        proof=proof,
        phase_permit=read_permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", bind_permit.to_payload(), sequence=1),
            _message(
                "BIND_INTENT",
                BindIntentCommand(reference, bind_permit).to_payload(),
                sequence=2,
            ),
            _message("PHASE_PERMIT", read_permit.to_payload(), sequence=3),
            _message("READ", read.to_payload(), sequence=4),
        ]
    )
    session, signed_client = _session(channel)
    signed_client.failure = CredentialTransportError(
        "READ_IO_AMBIGUOUS",
        post_dispatch=True,
    )
    session.start()
    session.run_next()

    assert session.run_next() is True
    kind, payload = channel.sent[-1]
    failure = ReadFailureResult.from_payload(payload)
    assert kind == "READ_FAILURE"
    assert failure.failure_kind is ReadFailureKind.IO_AMBIGUOUS
    assert failure.io_may_have_occurred is True
    assert failure.reservation_sha256 == reserved.request_sha256
    assert failure.read_proof_sha256 == proof.proof_sha256


def test_transport_result_payload_is_exact_and_digest_recomputed() -> None:
    result = _pre_intent_result(_pre_intent_read())
    permit = _permit()
    command = ReadCommand.from_pre_intent(_pre_intent_read(), phase_permit=permit)
    payload = command.result_payload(result)

    replayed = transport_result_from_payload(payload["result"])
    assert replayed == result

    tampered = dict(payload["result"])
    tampered["fields"] = {"serverTime": 1}
    with pytest.raises(CredentialExecutionSessionError, match="TRANSPORT_RESULT_INVALID"):
        transport_result_from_payload(tampered)


@pytest.mark.parametrize("failure_kind", tuple(JournalReadFailureKind))
def test_read_failure_projection_uses_complete_journal_allowlist(
    failure_kind: JournalReadFailureKind,
) -> None:
    command = ReadCommand.from_pre_intent(
        _pre_intent_read(),
        phase_permit=_permit(),
    )

    replayed = ReadFailureResult.from_payload(
        ReadFailureResult.build(
            command,
            failure_kind=failure_kind,
            io_may_have_occurred=True,
        ).to_payload()
    )

    assert ReadFailureKind is JournalReadFailureKind
    assert replayed.failure_kind is failure_kind


def test_session_finish_is_permit_bound_exact_and_terminal() -> None:
    permit = _permit()
    finish = SessionFinishCommand(
        generation=1,
        final_state="BLOCKED",
        final_evidence_sha256=None,
        phase_permit=permit,
    )
    channel = FakeChannel(
        [
            _message("SESSION_INIT", _init_payload(), sequence=0),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("SESSION_FINISH", finish.to_payload(), sequence=2),
        ]
    )
    session, _client = _session(channel)
    session.start()

    assert session.run_next() is False
    assert channel.sent[-1] == (
        "SESSION_FINISHED",
        {
            "final_evidence_sha256": None,
            "final_state": "BLOCKED",
            "generation": 1,
            "schema_version": "gate1b.credential-execution-session.v1",
            "status": "FINISHED",
        },
    )
    with pytest.raises(CredentialExecutionSessionError, match="SESSION_ALREADY_FINISHED"):
        session.run_next()


def test_source_has_no_retry_cache_thread_future_or_background_execution() -> None:
    source = Path("src/global_quant/gate1b/credential_execution_session.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("asyncio", "concurrent.futures", "threading", "multiprocessing"):
        assert forbidden not in source


def test_primary_session_init_binds_an_absolute_execution_journal_path() -> None:
    command = SessionInitCommand(
        authority=_authority(),
        capability=GenerationCapability.PRIMARY,
        execution_journal_path=Path("/tmp/gate1b-execution.jsonl"),
    )

    replayed = SessionInitCommand.from_payload(command.to_payload())

    assert replayed == command
    assert replayed.execution_journal_path == Path("/tmp/gate1b-execution.jsonl")


def test_session_exposes_only_validated_journal_path_and_read_only_finish_state() -> None:
    path = Path("/tmp/gate1b-execution.jsonl")
    permit = _permit()
    finish = SessionFinishCommand(
        generation=1,
        final_state="BLOCKED",
        final_evidence_sha256=None,
        phase_permit=permit,
    )
    channel = FakeChannel(
        [
            _message(
                "SESSION_INIT",
                _init_payload(execution_journal_path=path),
                sequence=0,
            ),
            _message("PHASE_PERMIT", permit.to_payload(), sequence=1),
            _message("SESSION_FINISH", finish.to_payload(), sequence=2),
        ]
    )
    session, _client = _session(channel)

    with pytest.raises(
        CredentialExecutionSessionError,
        match="SESSION_NOT_STARTED",
    ):
        _ = session.execution_journal_path
    assert session.finished is False

    session.start()
    assert session.execution_journal_path == path
    assert session.finished is False
    assert session.run_next() is False
    assert session.finished is True
    with pytest.raises(AttributeError):
        session.finished = False  # type: ignore[misc]
