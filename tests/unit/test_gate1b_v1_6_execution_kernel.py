from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal

import pytest

import global_quant.gate1b.execution_kernel as execution_kernel_module
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    GenerationCapability,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ProcessReapReceipt,
    SessionAuthority,
)
from global_quant.gate1b.execution_kernel import (
    ChildDispatcher,
    ConfirmedIO,
    DispatchFailure,
    DispatchKernel,
    DispatchKernelError,
    DispatchResult,
    GoCommand,
    KernelFaultPoint,
)
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
    build_client_order_id,
    build_emergency_client_order_id,
)
from global_quant.gate1b.process_boundary import (
    AbsoluteDeadline,
    IPCCodec,
    IPCMessage,
    IPCProtocolError,
    PhaseDeadlinePermit,
    ProcessIdentity,
    ProcessLifecycleJournal,
    ReapAttestation,
)

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
NOW_SECONDS = 100.0
LIFECYCLE_SECONDS = 110.0
ATTEMPT_DEADLINE_NS = 104_000_000_000


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _mutation_result(reserved: ReservedRequest) -> TransportResult:
    client_id = dict(reserved.parameters).get("newClientOrderId") or dict(reserved.parameters).get(
        "origClientOrderId"
    )
    assert client_id is not None
    return TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(("clientOrderId", client_id), ("status", "NEW")),
    )


def _durable_intent(*, persisted: bool) -> DurableIntent:
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
        observed_elapsed_seconds=Decimal("11"),
    )
    return DurableIntent(
        authorization_id=AUTHORIZATION_ID,
        protocol_commit="4" * 40,
        protocol_tag_object="5" * 40,
        protocol_sha256="7" * 64,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        order_derivation=derivation,
        persisted=persisted,
    )


INTENT_SHA256 = _durable_intent(persisted=True).intent_sha256


def _pre_intent_reads(client_id: str) -> tuple[tuple[str, dict[str, str]], ...]:
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


def _establish_exact_request_chain(journal: ExecutionJournal, tmp_path):
    from global_quant.gate1b.durable_intent import persist_intent

    authority = SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    journal.establish_session_authority(authority)
    for sequence, (path, parameters) in enumerate(
        _pre_intent_reads(authority.client_id),
        start=1,
    ):
        prepared = journal.reserve_pre_intent_read(
            authority_sha256=authority.authority_sha256,
            path=path,
            parameters=parameters,
            elapsed_seconds=Decimal(sequence),
            deadline_ns=ATTEMPT_DEADLINE_NS + sequence,
            retry_index=0,
        )
        journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=_sha(f"pre-intent-{sequence}"),
            observed_at_ns=ATTEMPT_DEADLINE_NS,
        )
    root = tmp_path / "intent"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    persisted = persist_intent(root / "intent.json", _durable_intent(persisted=False))
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    return authority, persisted


class FakeChannel:
    def __init__(self, incoming: list[IPCMessage | BaseException] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.send_error: BaseException | None = None

    def send(self, kind: str, payload) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((kind, dict(payload)))

    def receive(self) -> IPCMessage:
        if not self.incoming:
            raise IPCProtocolError("IPC_EOF")
        value = self.incoming.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeHardDeadline:
    def __init__(self) -> None:
        self.intact_checks = 0

    def assert_intact(self) -> None:
        self.intact_checks += 1


class PermitOnlyHardDeadline:
    def __init__(self) -> None:
        self.intact_checks = 0

    def assert_intact(self) -> None:
        self.intact_checks += 1


def _phase_permit(
    *,
    generation: int = 1,
    absolute_deadline: float = 104.0,
    sequence: int = 0,
) -> PhaseDeadlinePermit:
    return PhaseDeadlinePermit.issue(
        generation=generation,
        sequence=sequence,
        absolute_deadline=absolute_deadline,
        lifecycle_deadline=LIFECYCLE_SECONDS,
    )


def _message(kind: str, payload: dict[str, object], *, sequence: int = 0) -> IPCMessage:
    codec = IPCCodec()
    return codec.decode(
        codec.encode(kind, payload, sequence=sequence),
        expected_sequence=sequence,
    )


def _identity(generation: int) -> ProcessIdentity:
    return ProcessIdentity(
        pid=320 + generation,
        ppid=123,
        pgid=320 + generation,
        sid=320 + generation,
        start_token=f"test:{generation}",
    )


def _bound_attempt(
    kind: MutationKind = MutationKind.CREATE,
    *,
    generation: int = 1,
) -> tuple[ReservedRequest, MutationReservationProof, MutationAttempt]:
    probe_client_id = build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    close_client_id = build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    elapsed = Decimal("1")
    precondition = None if kind is MutationKind.CREATE else _sha("precondition")
    source_attempt_id = None if kind is MutationKind.CREATE else _sha("source-attempt")
    if kind is MutationKind.CREATE:
        request_purpose = RequestPurpose.CREATE
        journal_purpose = MutationPurpose.PRIMARY_CREATE
        method = "POST"
        client_id = probe_client_id
        ledger = MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=elapsed,
        )
        parameters = {
            "newClientOrderId": client_id,
            "newOrderRespType": "ACK",
            "positionSide": "BOTH",
            "price": "50000",
            "quantity": "0.001",
            "recvWindow": str(RECEIVE_WINDOW_MS),
            "reduceOnly": "false",
            "side": "BUY",
            "symbol": SYMBOL,
            "timeInForce": "GTX",
            "type": "LIMIT",
        }
    elif kind is MutationKind.CANCEL:
        request_purpose = RequestPurpose.CANCEL
        journal_purpose = MutationPurpose.PRIMARY_CANCEL
        method = "DELETE"
        client_id = probe_client_id
        ledger = MutationLedger(
            total_http_requests=2,
            create_requests=1,
            cancel_requests=1,
            stage=RequestStage.CANCEL_ATTEMPTED,
            last_elapsed_seconds=elapsed,
        )
        parameters = {
            "origClientOrderId": client_id,
            "recvWindow": str(RECEIVE_WINDOW_MS),
            "symbol": SYMBOL,
        }
    else:
        request_purpose = RequestPurpose.EMERGENCY_CLOSE
        journal_purpose = MutationPurpose.PRIMARY_EMERGENCY_CLOSE
        method = "POST"
        client_id = close_client_id
        ledger = MutationLedger(
            total_http_requests=2,
            create_requests=1,
            emergency_close_requests=1,
            stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
            last_elapsed_seconds=elapsed,
        )
        parameters = {
            "newClientOrderId": client_id,
            "newOrderRespType": "ACK",
            "positionSide": "BOTH",
            "quantity": "0.001",
            "recvWindow": str(RECEIVE_WINDOW_MS),
            "reduceOnly": "true",
            "side": "SELL",
            "symbol": SYMBOL,
            "type": "MARKET",
        }
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=INTENT_SHA256,
        origin=DEMO_HTTP_ORIGIN,
        method=method,
        path="/fapi/v1/order",
        purpose=request_purpose,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=elapsed,
        retry_index=0,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=journal_purpose,
        generation=generation,
        deadline_ns=ATTEMPT_DEADLINE_NS,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=source_attempt_id,
        precondition_sha256=precondition,
    )
    attempt = MutationAttempt.build(
        kind=kind,
        generation=generation,
        retry_index=0,
        deadline_ns=ATTEMPT_DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=(precondition if kind is MutationKind.CANCEL else None),
    )
    return reserved, proof, attempt


def _kernel(
    tmp_path,
    *,
    channel: FakeChannel | None = None,
    fault_hook=None,
):
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    process_journal = ProcessLifecycleJournal.start(
        tmp_path / "process.jsonl",
        lifecycle_started_at=NOW_SECONDS,
        lifecycle_deadline=LIFECYCLE_SECONDS,
        execution_journal_path=journal.path,
    )
    identity = _identity(1)
    process_journal.stage_identity(1, identity)
    _simulate_process_admission(journal, process_journal, identity, generation=1)
    kernel = DispatchKernel(
        journal=journal,
        process_journal_path=process_journal.path,
        channel=channel or FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        fault_hook=fault_hook,
    )
    authority, persisted = _establish_exact_request_chain(journal, tmp_path)
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("12"),
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(sorted(persisted.intent.probe_payload.items())),
        elapsed_seconds=Decimal("12"),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=ATTEMPT_DEADLINE_NS,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=ATTEMPT_DEADLINE_NS,
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
        deadline_ns=ATTEMPT_DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    return journal, process_journal, kernel, identity, reserved, proof, attempt


def _simulate_process_admission(
    journal: ExecutionJournal,
    process_journal: ProcessLifecycleJournal,
    identity: ProcessIdentity,
    *,
    generation: int,
) -> None:
    from global_quant.gate1b.execution_journal import DurableGenerationAdmission

    capability = GenerationCapability.PRIMARY if generation == 1 else GenerationCapability.RECOVERY
    admission = journal.admit_generation(
        DurableGenerationAdmission(
            generation=generation,
            process_identity_sha256=identity.sha256,
        ),
        capability,
    )
    process_journal.record_execution_admission(
        generation=generation,
        identity=identity,
        execution_journal=journal,
        admission_record=admission,
    )


def _prepare(kernel: DispatchKernel, attempt: MutationAttempt, reserved: ReservedRequest):
    return kernel.prepare(
        attempt,
        reserved_request=reserved,
        phase_permit=_phase_permit(),
    )


def _go(
    journal: ExecutionJournal,
    kernel: DispatchKernel,
    attempt: MutationAttempt,
    reserved: ReservedRequest,
):
    prepared = _prepare(kernel, attempt, reserved)
    command = kernel.authorize_go(prepared)
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    return prepared, command


def _durable_reap_attestation(
    journal: ExecutionJournal,
    process_journal: ProcessLifecycleJournal,
    identity: ProcessIdentity,
    *,
    generation: int = 1,
    returncode: int = 0,
    signal: int | None = None,
) -> ReapAttestation:
    admission = next(
        record
        for record in journal.records()
        if type(record.event).__name__ == "_GenerationAdmitted"
        and record.event.generation == generation
    )
    execution_reap = journal.reap_generation(
        ProcessReapReceipt(
            generation=generation,
            process_identity_sha256=identity.sha256,
            admission_record_sequence=admission.sequence,
            admission_record_digest=admission.digest,
            returncode=returncode,
            signal=signal,
            local_process_quiesced=True,
            venue_mutation_absent_proven=False,
        )
    )
    process_reap = process_journal.record_reap(
        generation=generation,
        identity=identity,
        returncode=returncode,
        signal_number=signal,
        execution_journal=journal,
        execution_reap_record=execution_reap,
    )
    event = process_reap.event
    return ReapAttestation(
        generation=generation,
        stage_ordinal=event["stage_ordinal"],
        identity=identity,
        process_identity_sha256=identity.sha256,
        waited_pid=identity.pid,
        returncode=returncode,
        signal=signal,
        process_journal_path=process_journal.path,
        attested_monotonic_ns=event["attested_monotonic_ns"],
        journal_sequence=process_reap.sequence,
        journal_digest=process_reap.digest,
        journal_head_sequence=process_reap.sequence,
        journal_head_digest=process_reap.digest,
        execution_journal_sequence=execution_reap.sequence,
        execution_journal_digest=execution_reap.digest,
        execution_head_sequence=execution_reap.sequence,
        execution_head_digest=execution_reap.digest,
    )


def test_dispatch_kernel_is_not_a_second_generation_admission_owner() -> None:
    assert not hasattr(DispatchKernel, "admit_generation")


@pytest.mark.parametrize(
    "kind",
    [MutationKind.CREATE, MutationKind.CANCEL, MutationKind.EMERGENCY_CLOSE],
)
def test_go_roundtrip_carries_exact_unsigned_reserved_request_and_proof(kind) -> None:
    reserved, proof, attempt = _bound_attempt(kind)
    permit = _phase_permit()
    command = GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=1,
        lifecycle_deadline_ns=110_000_000_000,
        local_deadline_ns=ATTEMPT_DEADLINE_NS,
        go_deadline_ns=ATTEMPT_DEADLINE_NS,
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=_sha("prepared"),
        go_record_digest=_sha("go"),
    )

    payload = command.to_payload()
    decoded = GoCommand.from_payload(payload)

    assert decoded == command
    assert decoded.reserved_request == reserved
    assert decoded.reservation_proof == proof
    assert decoded.attempt.reservation_sha256 == reserved.request_sha256
    assert "parameters" in payload["reserved_request"]
    encoded = json.dumps(payload, sort_keys=True).lower()
    for forbidden in ("api_key", "apikey", "secret", "signature", "credential"):
        assert forbidden not in encoded
    IPCCodec().encode("GO", payload, sequence=0)


def test_go_rejects_extra_fields_and_any_request_or_ledger_substitution() -> None:
    reserved, proof, attempt = _bound_attempt()
    permit = _phase_permit()
    command = GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=1,
        lifecycle_deadline_ns=110_000_000_000,
        local_deadline_ns=ATTEMPT_DEADLINE_NS,
        go_deadline_ns=ATTEMPT_DEADLINE_NS,
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=_sha("prepared"),
        go_record_digest=_sha("go"),
    )
    payload = command.to_payload()
    payload["request_payload"] = {"apiKey": "forbidden"}
    with pytest.raises(DispatchKernelError, match="GO_FIELDS_INVALID"):
        GoCommand.from_payload(payload)

    changed_parameters = dict(reserved.parameters)
    changed_parameters["quantity"] = "0.002"
    substituted = replace(
        reserved,
        parameters=tuple(sorted(changed_parameters.items())),
    )
    with pytest.raises(DispatchKernelError, match="GO_RESERVATION_BINDING_INVALID"):
        replace(command, reserved_request=substituted)

    changed_ledger = replace(reserved.ledger, total_http_requests=2)
    with pytest.raises(DispatchKernelError, match="GO_RESERVATION_BINDING_INVALID"):
        replace(command, reserved_request=replace(reserved, ledger=changed_ledger))


def test_go_rejects_retry_generation_and_reservation_deadline_mismatch() -> None:
    reserved, proof, attempt = _bound_attempt()
    permit = _phase_permit()
    arguments = {
        "attempt": attempt,
        "reserved_request": reserved,
        "reservation_proof": proof,
        "generation": 1,
        "lifecycle_deadline_ns": 110_000_000_000,
        "local_deadline_ns": ATTEMPT_DEADLINE_NS,
        "go_deadline_ns": ATTEMPT_DEADLINE_NS,
        "phase_permit_sequence": permit.sequence,
        "phase_permit_digest": permit.digest,
        "prepared_record_digest": _sha("prepared"),
        "go_record_digest": _sha("go"),
    }
    with pytest.raises(DispatchKernelError, match="UNSIGNED_MUTATION_REQUEST_INVALID"):
        GoCommand(**{**arguments, "reserved_request": replace(reserved, retry_index=1)})

    generation_two_proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=2,
        deadline_ns=ATTEMPT_DEADLINE_NS,
        client_id=attempt.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    with pytest.raises(DispatchKernelError, match="GO_RESERVATION_BINDING_INVALID"):
        GoCommand(**{**arguments, "reservation_proof": generation_two_proof})

    short_proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=ATTEMPT_DEADLINE_NS - 1,
        client_id=attempt.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    with pytest.raises(DispatchKernelError, match="GO_RESERVATION_BINDING_INVALID"):
        GoCommand(**{**arguments, "reservation_proof": short_proof})


def test_prepare_requires_the_exact_durable_reservation(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    process_journal = ProcessLifecycleJournal.start(
        tmp_path / "process.jsonl",
        lifecycle_started_at=NOW_SECONDS,
        lifecycle_deadline=LIFECYCLE_SECONDS,
        execution_journal_path=journal.path,
    )
    identity = _identity(1)
    process_journal.stage_identity(1, identity)
    _simulate_process_admission(journal, process_journal, identity, generation=1)
    kernel = DispatchKernel(
        journal=journal,
        process_journal_path=process_journal.path,
        channel=FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
    )
    reserved, _proof, attempt = _bound_attempt()

    with pytest.raises(DispatchKernelError, match="DURABLE_EXACT_REQUEST_REQUIRED"):
        _prepare(kernel, attempt, reserved)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_NOT_FOUND"):
        journal.frontier(attempt.attempt_id)


def test_local_and_go_deadlines_are_clamped_to_lifecycle_and_attempt(tmp_path) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)

    prepared = kernel.prepare(
        attempt,
        reserved_request=reserved,
        phase_permit=_phase_permit(absolute_deadline=LIFECYCLE_SECONDS),
    )
    command = kernel.authorize_go(prepared)

    assert prepared.local_deadline_ns == 110_000_000_000
    assert command.go_deadline_ns == ATTEMPT_DEADLINE_NS
    assert command.go_deadline_ns <= command.lifecycle_deadline_ns
    assert command.go_deadline_ns <= command.attempt.deadline_ns
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


def test_dispatch_order_is_prepare_fsync_go_fsync_then_send(tmp_path) -> None:
    points: list[KernelFaultPoint] = []
    channel = FakeChannel()
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(
        tmp_path,
        channel=channel,
        fault_hook=points.append,
    )

    command = kernel.dispatch(
        attempt,
        reserved_request=reserved,
        phase_permit=_phase_permit(),
    )

    assert points == [
        KernelFaultPoint.PREPARE,
        KernelFaultPoint.PREPARED_FSYNC,
        KernelFaultPoint.GO,
        KernelFaultPoint.GO_FSYNC,
        KernelFaultPoint.SEND,
        KernelFaultPoint.SENT,
    ]
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    assert channel.sent == [("GO", command.to_payload())]


def test_restart_replays_durable_go_then_real_reap_settles_unknown(tmp_path) -> None:
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(tmp_path)
    _go(journal, kernel, attempt, reserved)
    restarted = DispatchKernel(
        journal=ExecutionJournal(journal.path),
        process_journal_path=process.path,
        channel=FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
    )
    attestation = _durable_reap_attestation(journal, process, identity)

    assert (
        restarted.settle_failure(
            attempt,
            failure=DispatchFailure.EOF,
            reap_attestation=attestation,
        )
        is FrontierState.UNKNOWN
    )


def test_restart_prepared_requires_wal_verified_reap_for_not_dispatched(tmp_path) -> None:
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(tmp_path)
    _prepare(kernel, attempt, reserved)
    restarted = DispatchKernel(
        journal=ExecutionJournal(journal.path),
        process_journal_path=process.path,
        channel=FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
    )
    admission = next(
        record
        for record in journal.records()
        if type(record.event).__name__ == "_GenerationAdmitted"
    )
    caller_receipt = ProcessReapReceipt(
        generation=1,
        process_identity_sha256=identity.sha256,
        admission_record_sequence=admission.sequence,
        admission_record_digest=admission.digest,
        returncode=0,
        signal=None,
        local_process_quiesced=True,
        venue_mutation_absent_proven=False,
    )
    with pytest.raises(DispatchKernelError, match="REAL_REAP_ATTESTATION_REQUIRED"):
        restarted.settle_failure(
            attempt,
            failure=DispatchFailure.EOF,
            reap_attestation=caller_receipt,  # type: ignore[arg-type]
        )
    assert journal.frontier(attempt.attempt_id) is FrontierState.PREPARED

    attestation = _durable_reap_attestation(journal, process, identity)
    forged = replace(
        attestation,
        journal_digest=_sha("forged-process-record"),
        journal_head_digest=_sha("forged-process-record"),
    )
    with pytest.raises(
        DispatchKernelError,
        match="PROCESS_REAP_ATTESTATION_NOT_DURABLE",
    ):
        restarted.settle_failure(
            attempt,
            failure=DispatchFailure.EOF,
            reap_attestation=forged,
        )
    assert journal.frontier(attempt.attempt_id) is FrontierState.PREPARED
    assert (
        restarted.settle_failure(
            attempt,
            failure=DispatchFailure.EOF,
            reap_attestation=attestation,
        )
        is FrontierState.NOT_DISPATCHED
    )


@pytest.mark.parametrize(
    "failure",
    [
        DispatchFailure.FAULT,
        DispatchFailure.TIMEOUT,
        DispatchFailure.KILLED,
        DispatchFailure.EOF,
        DispatchFailure.CORRUPT,
        DispatchFailure.TRUNCATED,
        DispatchFailure.OVERSIZED,
        DispatchFailure.VERSION,
        DispatchFailure.SEQUENCE,
        DispatchFailure.DIGEST,
        DispatchFailure.PARTIAL_RESULT,
        DispatchFailure.WRITE_LOSS,
        DispatchFailure.PARSE,
        DispatchFailure.RESULT_DURABILITY,
    ],
)
def test_every_post_go_failure_is_unknown_after_verified_reap(tmp_path, failure) -> None:
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(tmp_path)
    _go(journal, kernel, attempt, reserved)
    attestation = _durable_reap_attestation(journal, process, identity)

    assert (
        kernel.settle_failure(
            attempt,
            failure=failure,
            reap_attestation=attestation,
        )
        is FrontierState.UNKNOWN
    )


@pytest.mark.parametrize(
    ("failure", "boundary"),
    [
        (DispatchFailure.FAULT, BoundaryResult.CORRUPT),
        (DispatchFailure.TIMEOUT, BoundaryResult.TIMEOUT),
        (DispatchFailure.KILLED, BoundaryResult.KILLED),
        (DispatchFailure.EOF, BoundaryResult.EOF),
        (DispatchFailure.CORRUPT, BoundaryResult.CORRUPT),
        (DispatchFailure.TRUNCATED, BoundaryResult.DECODE_FAILURE),
        (DispatchFailure.OVERSIZED, BoundaryResult.DECODE_FAILURE),
        (DispatchFailure.VERSION, BoundaryResult.DECODE_FAILURE),
        (DispatchFailure.SEQUENCE, BoundaryResult.DECODE_FAILURE),
        (DispatchFailure.DIGEST, BoundaryResult.DECODE_FAILURE),
        (DispatchFailure.PARTIAL_RESULT, BoundaryResult.PARTIAL_WRITE),
        (DispatchFailure.WRITE_LOSS, BoundaryResult.RESPONSE_LOSS),
        (DispatchFailure.PARSE, BoundaryResult.DECODE_FAILURE),
        (
            DispatchFailure.RESULT_DURABILITY,
            BoundaryResult.RESULT_DURABILITY_FAILURE,
        ),
    ],
)
def test_failure_mapping_is_precise(failure, boundary) -> None:
    assert DispatchKernel.boundary_result_for_failure(failure) is boundary


@pytest.mark.parametrize(
    ("fault_point", "expected_state"),
    [
        (KernelFaultPoint.PREPARED_FSYNC, FrontierState.NOT_DISPATCHED),
        (KernelFaultPoint.GO, FrontierState.NOT_DISPATCHED),
        (KernelFaultPoint.GO_FSYNC, FrontierState.UNKNOWN),
    ],
)
def test_fault_boundaries_resolve_only_from_durable_frontier(
    tmp_path,
    fault_point,
    expected_state,
) -> None:
    def fault(point: KernelFaultPoint) -> None:
        if point is fault_point:
            raise RuntimeError("injected")

    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(
        tmp_path,
        fault_hook=fault,
    )
    with pytest.raises(RuntimeError, match="injected"):
        kernel.dispatch(
            attempt,
            reserved_request=reserved,
            phase_permit=_phase_permit(),
        )
    attestation = _durable_reap_attestation(journal, process, identity)
    assert (
        kernel.settle_failure(
            attempt,
            failure=DispatchFailure.FAULT,
            reap_attestation=attestation,
        )
        is expected_state
    )


def test_send_failure_is_post_go_unknown(tmp_path) -> None:
    channel = FakeChannel()
    channel.send_error = IPCProtocolError("IPC_WRITE_FAILED")
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(
        tmp_path,
        channel=channel,
    )
    with pytest.raises(IPCProtocolError, match="IPC_WRITE_FAILED"):
        kernel.dispatch(
            attempt,
            reserved_request=reserved,
            phase_permit=_phase_permit(),
        )
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    attestation = _durable_reap_attestation(journal, process, identity)
    assert (
        kernel.settle_failure(
            attempt,
            failure=DispatchFailure.WRITE_LOSS,
            reap_attestation=attestation,
        )
        is FrontierState.UNKNOWN
    )


def test_child_never_calls_io_without_an_exact_go() -> None:
    channel = FakeChannel()
    dispatcher = ChildDispatcher(
        channel=channel,
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=FakeHardDeadline(),
    )
    calls = 0

    def io_callback(_reserved: ReservedRequest) -> ConfirmedIO:
        nonlocal calls
        calls += 1
        return ConfirmedIO(_mutation_result(_reserved))

    with pytest.raises(IPCProtocolError, match="IPC_EOF"):
        dispatcher.dispatch_once(io_callback, phase_permit=_phase_permit())
    assert calls == 0


def test_real_phase_permit_is_the_only_child_go_deadline_authority(tmp_path) -> None:
    _journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    permit = _phase_permit(absolute_deadline=103.0)
    prepared = kernel.prepare(
        attempt,
        reserved_request=reserved,
        phase_permit=permit,
    )
    command = kernel.authorize_go(prepared)
    channel = FakeChannel([_message("GO", command.to_payload())])
    hard_deadline = PermitOnlyHardDeadline()
    dispatcher = ChildDispatcher(
        channel=channel,
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=hard_deadline,
    )

    dispatcher.dispatch_once(
        lambda exact: ConfirmedIO(_mutation_result(exact)),
        phase_permit=permit,
    )

    assert command.go_deadline_ns == 103_000_000_000
    assert hard_deadline.intact_checks == 1


def test_child_callback_receives_only_exact_reserved_request_after_durable_go(
    tmp_path,
) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    channel = FakeChannel([_message("GO", command.to_payload())])
    hard_deadline = FakeHardDeadline()
    dispatcher = ChildDispatcher(
        channel=channel,
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=hard_deadline,
    )
    received: list[ReservedRequest] = []

    def io_callback(exact_reserved: ReservedRequest) -> ConfirmedIO:
        assert type(exact_reserved) is ReservedRequest
        assert exact_reserved is not attempt
        assert exact_reserved == reserved
        assert exact_reserved.request_sha256 == attempt.reservation_sha256
        assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
        received.append(exact_reserved)
        return ConfirmedIO(_mutation_result(exact_reserved))

    result = dispatcher.dispatch_once(io_callback, phase_permit=_phase_permit())

    assert received == [reserved]
    assert hard_deadline.intact_checks == 1
    assert DispatchResult.from_payload(result.to_payload()) == result
    assert channel.sent == [("RESULT", result.to_payload())]


@pytest.mark.parametrize("field", ["generation", "go_deadline_ns"])
def test_wrong_generation_or_late_go_never_calls_io(tmp_path, field) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    payload = command.to_payload()
    if field == "generation":
        payload["generation"] = 2
    else:
        payload["go_deadline_ns"] = 111_000_000_000
    channel = FakeChannel([_message("GO", payload)])
    dispatcher = ChildDispatcher(
        channel=channel,
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=FakeHardDeadline(),
    )
    calls = 0

    def io_callback(_reserved: ReservedRequest) -> ConfirmedIO:
        nonlocal calls
        calls += 1
        return ConfirmedIO(_mutation_result(_reserved))

    with pytest.raises(DispatchKernelError):
        dispatcher.dispatch_once(io_callback, phase_permit=_phase_permit())
    assert calls == 0


def test_tampered_reserved_parameters_never_reach_child_io(tmp_path) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    payload = command.to_payload()
    parameters = payload["reserved_request"]["parameters"]
    for pair in parameters:
        if pair[0] == "quantity":
            pair[1] = "0.002"
    channel = FakeChannel([_message("GO", payload)])
    dispatcher = ChildDispatcher(
        channel=channel,
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=FakeHardDeadline(),
    )
    calls = 0

    def io_callback(_reserved: ReservedRequest) -> ConfirmedIO:
        nonlocal calls
        calls += 1
        return ConfirmedIO(_mutation_result(_reserved))

    with pytest.raises(DispatchKernelError, match="RESERVED_REQUEST_DIGEST_MISMATCH"):
        dispatcher.dispatch_once(io_callback, phase_permit=_phase_permit())
    assert calls == 0


def test_only_exact_typed_result_digest_can_be_confirmed(tmp_path) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    result = DispatchResult.build(attempt, transport_result=_mutation_result(reserved))
    assert (
        kernel.confirm_result(command, _message("RESULT", result.to_payload()))
        is FrontierState.CONFIRMED
    )
    assert journal.frontier(attempt.attempt_id) is FrontierState.CONFIRMED


def test_mutation_result_preserves_only_allowlisted_optional_order_digest() -> None:
    reserved, _proof, attempt = _bound_attempt()
    result = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(
            ("clientOrderId", attempt.client_id),
            ("orderIdSha256", _sha("venue-order-id")),
            ("status", "NEW"),
        ),
    )

    replayed = DispatchResult.from_payload(
        DispatchResult.build(attempt, transport_result=result).to_payload()
    )

    assert replayed.transport_result == result
    assert replayed.transport_result.field("orderIdSha256") == _sha("venue-order-id")

    forbidden = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(
            ("clientOrderId", attempt.client_id),
            ("rawBody", "signed-response-material"),
            ("status", "NEW"),
        ),
    )
    with pytest.raises(DispatchKernelError, match="MUTATION_TRANSPORT_RESULT_INVALID"):
        DispatchResult.build(attempt, transport_result=forbidden)


def test_forged_result_is_rejected_before_confirmation(tmp_path) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    payload = DispatchResult.build(
        attempt,
        transport_result=_mutation_result(reserved),
    ).to_payload()
    payload["digest"] = "f" * 64
    with pytest.raises(DispatchKernelError, match="RESULT_DIGEST_MISMATCH"):
        kernel.confirm_result(command, _message("RESULT", payload))
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


def test_result_durability_failure_is_unknown_after_real_reap(
    tmp_path,
    monkeypatch,
) -> None:
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    result = DispatchResult.build(attempt, transport_result=_mutation_result(reserved))

    def fail_before_write(_attempt_id: str, _result_sha256: str):
        raise ExecutionJournalError("JOURNAL_FSYNC_FAILED")

    monkeypatch.setattr(journal, "record_confirmed", fail_before_write)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_FSYNC_FAILED"):
        kernel.confirm_result(command, _message("RESULT", result.to_payload()))
    monkeypatch.undo()
    attestation = _durable_reap_attestation(journal, process, identity)
    assert (
        kernel.settle_failure(
            attempt,
            failure=DispatchFailure.RESULT_DURABILITY,
            reap_attestation=attestation,
        )
        is FrontierState.UNKNOWN
    )


def test_process_owner_forbids_primary_generation_two_and_kernel_restores_recovery(
    tmp_path,
) -> None:
    journal, process, kernel, identity, reserved, _proof, attempt = _kernel(tmp_path)
    _go(journal, kernel, attempt, reserved)
    attestation = _durable_reap_attestation(journal, process, identity)
    kernel.settle_failure(
        attempt,
        failure=DispatchFailure.EOF,
        reap_attestation=attestation,
    )
    identity_two = _identity(2)
    process.stage_identity(2, identity_two)
    from global_quant.gate1b.execution_journal import DurableGenerationAdmission

    admission = DurableGenerationAdmission(
        generation=2,
        process_identity_sha256=identity_two.sha256,
    )
    with pytest.raises(ExecutionJournalError):
        journal.admit_generation(
            admission,
            GenerationCapability.PRIMARY,
        )
    _simulate_process_admission(journal, process, identity_two, generation=2)
    restarted = DispatchKernel(
        journal=ExecutionJournal(journal.path),
        process_journal_path=process.path,
        channel=FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
    )
    assert restarted is not None


def test_restart_replays_the_exact_reserved_request_not_only_its_proof(tmp_path) -> None:
    journal, process, kernel, _identity_one, reserved, proof, attempt = _kernel(tmp_path)
    _prepare(kernel, attempt, reserved)

    restarted = DispatchKernel(
        journal=ExecutionJournal(journal.path),
        process_journal_path=process.path,
        channel=FakeChannel(),
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
    )

    assert restarted.replayed_exact_request(attempt.attempt_id) == (reserved, proof)


def test_child_rejects_a_forged_earlier_go_deadline_instead_of_tightening_it(
    tmp_path,
) -> None:
    journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    _prepared, command = _go(journal, kernel, attempt, reserved)
    payload = command.to_payload()
    payload["go_deadline_ns"] = command.go_deadline_ns - 1
    dispatcher = ChildDispatcher(
        channel=FakeChannel([_message("GO", payload)]),
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=FakeHardDeadline(),
    )
    calls = 0

    def io_callback(_reserved: ReservedRequest) -> ConfirmedIO:
        nonlocal calls
        calls += 1
        return ConfirmedIO(_mutation_result(_reserved))

    with pytest.raises(DispatchKernelError, match="EXACT_GO_REQUIRED"):
        dispatcher.dispatch_once(io_callback, phase_permit=_phase_permit())
    assert calls == 0


def test_child_rejects_a_hard_phase_permit_later_than_exact_go_deadline(
    tmp_path,
) -> None:
    _journal, _process, kernel, _identity_one, reserved, _proof, attempt = _kernel(tmp_path)
    late_permit = _phase_permit(absolute_deadline=105.0)
    prepared = kernel.prepare(
        attempt,
        reserved_request=reserved,
        phase_permit=late_permit,
    )
    command = kernel.authorize_go(prepared)
    dispatcher = ChildDispatcher(
        channel=FakeChannel([_message("GO", command.to_payload())]),
        generation=1,
        lifecycle_deadline=AbsoluteDeadline(
            LIFECYCLE_SECONDS,
            clock=lambda: NOW_SECONDS,
        ),
        hard_deadline=FakeHardDeadline(),
    )
    calls = 0

    def io_callback(_reserved: ReservedRequest) -> ConfirmedIO:
        nonlocal calls
        calls += 1
        return ConfirmedIO(_mutation_result(_reserved))

    with pytest.raises(DispatchKernelError, match="EXACT_GO_REQUIRED"):
        dispatcher.dispatch_once(io_callback, phase_permit=late_permit)
    assert calls == 0


def test_obsolete_nominal_fresh_order_semantics_are_not_public() -> None:
    assert not hasattr(execution_kernel_module, "FreshOrderRead")
    assert not hasattr(execution_kernel_module, "FreshOrderStatus")
    assert not hasattr(DispatchKernel, "confirm_fresh_order_read")
    assert not hasattr(DispatchKernel, "new_conditional_cleanup_cancel")
