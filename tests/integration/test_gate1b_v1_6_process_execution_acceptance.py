from __future__ import annotations

import hashlib
import json
import os
import pty
import signal
import socket
import sys
import threading
import time
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b.durable_intent import PersistedIntent, persist_intent
from global_quant.gate1b.execution_evidence_log import ExecutionEvidenceLog
from global_quant.gate1b.execution_journal import (
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ReconciliationKeyKind,
    RecoveryMode,
    SessionAuthority,
)
from global_quant.gate1b.execution_kernel import (
    DispatchFailure,
    DispatchKernel,
    DispatchKernelError,
    GoCommand,
    KernelFaultPoint,
)
from global_quant.gate1b.execution_lifecycle import (
    FINAL_READ_STEPS,
    PRE_INTENT_READ_STEPS,
    Freshness,
    LifecycleError,
    LifecycleTiming,
    LocalDisposition,
    LocalResolution,
    MutationDisposition,
    MutationResolution,
    PhasePermitProjection,
    PrimaryJournalProjection,
    ReadDisposition,
    ReadResolution,
    Step,
    apply_local,
    plan_next,
    reserve_http,
    resolve_http,
    start_primary,
)
from global_quant.gate1b.final_evidence import (
    BlockedFinalizationCause,
    FinalEvidenceFinalizer,
    scan_evidence_tree,
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
    CredentialProcessSupervisor,
    CredentialWorkload,
    GenerationAdmissionError,
    IPCProtocolError,
    PhaseDeadlinePermit,
    ProcessLifecycleJournal,
    ReapAttestation,
    is_same_process_alive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
PROTOCOL_COMMIT = "4" * 40
PROTOCOL_TAG_OBJECT = "5" * 40
PROTOCOL_SHA256 = "7" * 64
SYNTHETIC_CANARY = "acceptance-credential-canary"

DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="native macOS Seatbelt process-containment acceptance",
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _child_environment() -> dict[str, str]:
    return {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(SOURCE_ROOT),
    }


def _workload(script: str, *arguments: str) -> CredentialWorkload:
    return CredentialWorkload.test_only((sys.executable, "-c", script, *arguments))


def _rotate_credential_tty(master_fd: int, stable_slave_fd: int) -> int:
    """Give the next isolated process session a fresh controlling terminal."""

    next_master_fd, next_slave_fd = pty.openpty()
    try:
        os.dup2(next_slave_fd, stable_slave_fd)
    finally:
        os.close(next_slave_fd)
    os.close(master_fd)
    return next_master_fd


def _controller(
    root: Path,
    *,
    credential_stdin: int | None = None,
    lifecycle_seconds: float = 20.0,
) -> tuple[
    CredentialProcessSupervisor,
    ExecutionJournal,
    ProcessLifecycleJournal,
]:
    journal = ExecutionJournal(root / "request-ledger.json")
    lifecycle_started_at = time.monotonic()
    lifecycle = ProcessLifecycleJournal.start(
        root / "lifecycle.jsonl",
        lifecycle_started_at=lifecycle_started_at,
        lifecycle_deadline=lifecycle_started_at + lifecycle_seconds,
        execution_journal_path=journal.path,
    )
    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=lifecycle,
        execution_journal=journal,
        parent_environment=_child_environment(),
        credential_stdin=credential_stdin,
        allow_test_workloads=True,
    )
    return supervisor, journal, lifecycle


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
        observed_elapsed_seconds=Decimal("0.11"),
    )
    return DurableIntent(
        authorization_id=AUTHORIZATION_ID,
        protocol_commit=PROTOCOL_COMMIT,
        protocol_tag_object=PROTOCOL_TAG_OBJECT,
        protocol_sha256=PROTOCOL_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        order_derivation=derivation,
        persisted=persisted,
    )


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


def _establish_create_chain(
    journal: ExecutionJournal,
    root: Path,
    *,
    deadline_ns: int,
) -> tuple[SessionAuthority, PersistedIntent, ReservedRequest, MutationAttempt]:
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
            elapsed_seconds=Decimal(sequence) / Decimal(100),
            deadline_ns=deadline_ns,
            retry_index=0,
        )
        journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=_sha(f"acceptance-pre-intent-{sequence}"),
            observed_at_ns=time.monotonic_ns() + sequence,
        )

    intent_root = root / "intent"
    intent_root.mkdir(mode=0o700)
    persisted = persist_intent(intent_root / "intent.json", _durable_intent(persisted=False))
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("0.12"),
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(sorted(persisted.intent.probe_payload.items())),
        elapsed_seconds=Decimal("0.12"),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=deadline_ns,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=deadline_ns,
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
        deadline_ns=deadline_ns,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    return authority, persisted, reserved, attempt


def _kernel_for_child(
    supervisor: CredentialProcessSupervisor,
    journal: ExecutionJournal,
    lifecycle: ProcessLifecycleJournal,
    child,
    *,
    fault_hook=None,
) -> DispatchKernel:
    return DispatchKernel(
        journal=journal,
        process_journal_path=lifecycle.path,
        channel=child.channel,
        lifecycle_deadline=supervisor.deadline,
        fault_hook=fault_hook,
    )


def _attempt_deadline_ns(supervisor: CredentialProcessSupervisor) -> int:
    local = min(supervisor.deadline.at, time.monotonic() + 8.0)
    return int(local * 1_000_000_000)


def _reap_and_verify(
    supervisor: CredentialProcessSupervisor,
    lifecycle: ProcessLifecycleJournal,
    child,
) -> ReapAttestation:
    attestation = supervisor.kill_and_reap(child, local_limit=1.0)
    ProcessLifecycleJournal.restore(lifecycle.path).verify_reap_attestation(attestation)
    assert attestation.waited_pid == child.identity.pid
    assert attestation.exact_pid_waited is True
    assert attestation.descendant_creation_denied is True
    assert attestation.local_process_quiesced is True
    assert attestation.venue_mutation_absent_proven is False
    assert is_same_process_alive(child.identity) is False
    return attestation


@DARWIN_ONLY
def test_a1_real_child_phase_permits_share_one_absolute_lifecycle_deadline(
    tmp_path: Path,
) -> None:
    script = """
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
for _ in range(3):
    permit = bootstrap.accept_phase_permit()
    bootstrap.channel.send(
        "PERMIT_SEEN",
        {
            "absolute_deadline": permit.absolute_deadline,
            "lifecycle_deadline": permit.lifecycle_deadline,
            "sequence": permit.sequence,
        },
    )
"""
    supervisor, journal, lifecycle = _controller(tmp_path, lifecycle_seconds=3.0)
    original_deadline = lifecycle.lifecycle_deadline
    child = supervisor.launch(_workload(script), generation=1)
    permits: list[PhaseDeadlinePermit] = []

    for sequence in range(3):
        permit = supervisor.issue_phase_permit(child, local_limit=5.0)
        permits.append(permit)
        observed = child.channel.receive()
        assert observed.kind == "PERMIT_SEEN"
        assert observed.payload == {
            "absolute_deadline": permit.absolute_deadline,
            "lifecycle_deadline": original_deadline,
            "sequence": sequence,
        }
        time.sleep(0.03)

    attestation = supervisor.reap(child, local_limit=1.0)
    ProcessLifecycleJournal.restore(lifecycle.path).verify_reap_attestation(attestation)
    assert tuple(permit.sequence for permit in permits) == (0, 1, 2)
    assert all(permit.lifecycle_deadline == original_deadline for permit in permits)
    assert all(permit.absolute_deadline == original_deadline for permit in permits)
    assert supervisor.deadline.at == original_deadline
    assert ProcessLifecycleJournal.restore(lifecycle.path).lifecycle_deadline == original_deadline
    assert any(type(record.event).__name__ == "_GenerationReaped" for record in journal.records())


class _InjectedKernelCrash(RuntimeError):
    pass


_A2_FAULT_MATRIX = (
    (KernelFaultPoint.PREPARE, None),
    (KernelFaultPoint.PREPARED_FSYNC, FrontierState.NOT_DISPATCHED),
    (KernelFaultPoint.GO, FrontierState.NOT_DISPATCHED),
    (KernelFaultPoint.GO_FSYNC, FrontierState.UNKNOWN),
    (KernelFaultPoint.SEND, FrontierState.UNKNOWN),
    (KernelFaultPoint.SENT, FrontierState.UNKNOWN),
)


@DARWIN_ONLY
@pytest.mark.parametrize(("fault_point", "settled_frontier"), _A2_FAULT_MATRIX)
def test_a2_real_controller_wal_and_go_fault_boundaries_are_conservative(
    tmp_path: Path,
    fault_point: KernelFaultPoint,
    settled_frontier: FrontierState | None,
) -> None:
    script = """
import time
from global_quant.gate1b.execution_journal import MutationKind
from global_quant.gate1b.execution_kernel import ChildDispatcher
from global_quant.gate1b.process_boundary import credential_child_bootstrap

bootstrap = credential_child_bootstrap()
permit = bootstrap.accept_phase_permit()
dispatcher = ChildDispatcher(
    channel=bootstrap.channel,
    generation=bootstrap.generation,
    lifecycle_deadline=bootstrap.deadline,
    hard_deadline=bootstrap.hard_deadline,
)

def execute(_reserved):
    bootstrap.assert_mutation_allowed(MutationKind.CREATE)
    time.sleep(30)

dispatcher.dispatch_once(execute, phase_permit=permit)
"""
    supervisor, journal, lifecycle = _controller(tmp_path)
    child = supervisor.launch(_workload(script), generation=1)
    deadline_ns = _attempt_deadline_ns(supervisor)
    _authority, _persisted, reserved, attempt = _establish_create_chain(
        journal,
        tmp_path,
        deadline_ns=deadline_ns,
    )

    def crash_at(point: KernelFaultPoint) -> None:
        if point is fault_point:
            raise _InjectedKernelCrash(point.value)

    kernel = _kernel_for_child(
        supervisor,
        journal,
        lifecycle,
        child,
        fault_hook=crash_at,
    )
    permit = supervisor.issue_phase_permit(child, local_limit=5.0)

    with pytest.raises(_InjectedKernelCrash, match=fault_point.value):
        kernel.dispatch(
            attempt,
            reserved_request=reserved,
            phase_permit=permit,
        )

    attestation = _reap_and_verify(supervisor, lifecycle, child)
    event_names = tuple(type(record.event).__name__ for record in journal.records())
    go_was_durable = fault_point in {
        KernelFaultPoint.GO_FSYNC,
        KernelFaultPoint.SEND,
        KernelFaultPoint.SENT,
    }
    assert ("_GoDurable" in event_names) is go_was_durable

    if settled_frontier is None:
        with pytest.raises(ExecutionJournalError, match="ATTEMPT_NOT_FOUND"):
            journal.frontier(attempt.attempt_id)
        with pytest.raises(DispatchKernelError, match="CALLBACK_GATE_NOT_PROVEN"):
            kernel.settle_failure(
                attempt,
                failure=DispatchFailure.FAULT,
                reap_attestation=attestation,
            )
    else:
        assert (
            kernel.settle_failure(
                attempt,
                failure=DispatchFailure.FAULT,
                reap_attestation=attestation,
            )
            is settled_frontier
        )


@DARWIN_ONLY
@pytest.mark.parametrize(
    "phase",
    ("dns", "connect", "tls", "pre-write", "partial-write", "read", "parse"),
)
def test_a3_a8_sigkill_exact_reap_and_descendant_denial_cover_every_stuck_phase(
    tmp_path: Path,
    phase: str,
) -> None:
    script = """
import sys
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("STUCK_PHASE", {"phase": sys.argv[1]})
time.sleep(30)
"""
    supervisor, journal, lifecycle = _controller(tmp_path)
    child = supervisor.launch(_workload(script, phase), generation=1)
    assert child.channel.receive().payload == {"phase": phase}

    attestation = _reap_and_verify(supervisor, lifecycle, child)

    assert attestation.signal == signal.SIGKILL
    assert attestation.returncode == -signal.SIGKILL
    assert child.identity.pid == child.identity.pgid == child.identity.sid
    assert child.launch_argv.count("/usr/bin/sandbox-exec") == 1
    assert "(deny process-fork)" in child.launch_argv[2]
    assert ProcessLifecycleJournal.restore(lifecycle.path).active_identity is None
    assert any(type(record.event).__name__ == "_GenerationReaped" for record in journal.records())
    with pytest.raises(ChildProcessError):
        os.waitpid(child.identity.pid, os.WNOHANG)


class _DelayedLoopbackVenue:
    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.received = threading.Event()
        self.allow_apply = threading.Event()
        self.applied = threading.Event()
        self.request_bytes = b""
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._listener.settimeout(5.0)
            connection, _address = self._listener.accept()
            with connection:
                connection.settimeout(5.0)
                self.request_bytes = connection.recv(16_384)
                if not self.request_bytes:
                    raise AssertionError("loopback venue received no mutation bytes")
                self.received.set()
                if not self.allow_apply.wait(5.0):
                    raise AssertionError("delayed venue apply gate was never released")
                self.applied.set()
        except BaseException as exc:  # surfaced by close()
            self.error = exc
            self.received.set()
            self.applied.set()

    def close(self) -> None:
        self.allow_apply.set()
        self._thread.join(timeout=5.0)
        self._listener.close()
        if self._thread.is_alive():
            raise AssertionError("loopback venue thread did not terminate")
        if self.error is not None:
            raise self.error


@DARWIN_ONLY
def test_a4_a7_delayed_venue_effect_stays_unknown_and_recovery_only_across_gen3(
    tmp_path: Path,
) -> None:
    mutation_script = """
import socket
import sys
import time
from global_quant.gate1b.execution_journal import MutationKind
from global_quant.gate1b.execution_kernel import ChildDispatcher
from global_quant.gate1b.process_boundary import credential_child_bootstrap

bootstrap = credential_child_bootstrap()
permit = bootstrap.accept_phase_permit()
canary = "-".join(("acceptance", "credential", "canary"))
bootstrap.install_credential_guard(canary)
dispatcher = ChildDispatcher(
    channel=bootstrap.channel,
    generation=bootstrap.generation,
    lifecycle_deadline=bootstrap.deadline,
    hard_deadline=bootstrap.hard_deadline,
)

def execute(reserved):
    bootstrap.assert_network_ready()
    bootstrap.assert_mutation_allowed(MutationKind.CREATE)
    client_id = dict(reserved.parameters)["newClientOrderId"]
    request = (
        "POST /fapi/v1/order HTTP/1.1\\r\\n"
        "Host: 127.0.0.1\\r\\n"
        f"X-Deterministic-Client-ID: {client_id}\\r\\n"
        "Content-Length: 0\\r\\n\\r\\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2.0) as sock:
        sock.sendall(request)
        time.sleep(30)

dispatcher.dispatch_once(execute, phase_permit=permit)
"""
    recovery_script = """
import time
from global_quant.gate1b.execution_journal import MutationKind
from global_quant.gate1b.mutation_protocol import (
    build_client_order_id,
    build_emergency_client_order_id,
)
from global_quant.gate1b.process_boundary import CredentialBoundaryError
from global_quant.gate1b.process_boundary import credential_child_bootstrap

bootstrap = credential_child_bootstrap()
try:
    bootstrap.assert_mutation_allowed(MutationKind.CREATE)
except CredentialBoundaryError:
    create_allowed = False
else:
    create_allowed = True
bootstrap.channel.send(
    "RECOVERY_IDENTITY",
    {
        "capability": bootstrap.capability.value,
        "close_client_id": build_emergency_client_order_id("1" * 40, "2" * 16),
        "create_allowed": create_allowed,
        "probe_client_id": build_client_order_id("1" * 40, "2" * 16),
    },
)
time.sleep(30)
"""
    venue = _DelayedLoopbackVenue()
    master_fd, slave_fd = pty.openpty()
    try:
        supervisor, journal, lifecycle = _controller(
            tmp_path,
            credential_stdin=slave_fd,
            lifecycle_seconds=30.0,
        )
        mutation_workload = _workload(mutation_script, str(venue.port))
        child = supervisor.launch(mutation_workload, generation=1)
        deadline_ns = _attempt_deadline_ns(supervisor)
        authority, _persisted, reserved, attempt = _establish_create_chain(
            journal,
            tmp_path,
            deadline_ns=deadline_ns,
        )
        evidence_log = ExecutionEvidenceLog(
            tmp_path / "requests.jsonl",
            execution_journal_path=journal.path,
            credential_canaries=(SYNTHETIC_CANARY,),
        )

        def project_prepared(point: KernelFaultPoint) -> None:
            if point is KernelFaultPoint.GO:
                evidence_log.append_prepared(attempt.attempt_id)

        kernel = _kernel_for_child(
            supervisor,
            journal,
            lifecycle,
            child,
            fault_hook=project_prepared,
        )
        permit = supervisor.issue_phase_permit(child, local_limit=5.0)
        kernel.dispatch(
            attempt,
            reserved_request=reserved,
            phase_permit=permit,
        )

        assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
        assert venue.received.wait(3.0)
        assert venue.error is None
        assert authority.client_id.encode("ascii") in venue.request_bytes
        assert venue.applied.is_set() is False
        with pytest.raises(GenerationAdmissionError, match="OLD_GENERATION_STILL_PRESENT"):
            supervisor.launch(_workload(recovery_script), generation=2)

        attestation = _reap_and_verify(supervisor, lifecycle, child)
        assert venue.applied.is_set() is False
        venue.allow_apply.set()
        assert venue.applied.wait(3.0)
        assert venue.error is None
        assert (
            kernel.settle_failure(
                attempt,
                failure=DispatchFailure.KILLED,
                reap_attestation=attestation,
            )
            is FrontierState.UNKNOWN
        )
        evidence_log.append_failure(attempt.attempt_id)
        assert tuple(record.attempt_id for record in evidence_log.replay()) == (
            attempt.attempt_id,
            attempt.attempt_id,
        )

        directive = journal.recovery_directive(attempt.attempt_id)
        assert directive.mode is RecoveryMode.QUERY_PROBE_CLIENT_ID
        assert directive.query_client_id == authority.client_id
        assert directive.allows_post_create is False
        ledger = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
        assert ledger.create_requests == 1
        assert tuple(
            event.attempt
            for record in journal.records()
            if type(event := record.event).__name__ == "_AttemptPrepared"
        ) == (attempt,)

        recovery_workload = _workload(recovery_script)
        master_fd = _rotate_credential_tty(master_fd, slave_fd)
        recovery_two = supervisor.launch(recovery_workload, generation=2)
        recovery_two_message = recovery_two.channel.receive()
        authority_two_record = journal.issue_recovery_session_authority(
            primary_authority_sha256=authority.authority_sha256,
            source_attempt_id=attempt.attempt_id,
        )
        authority_two = authority_two_record.event.authority
        assert recovery_two_message.payload["capability"] == "RECOVERY"
        assert recovery_two_message.payload["create_allowed"] is False
        _reap_and_verify(supervisor, lifecycle, recovery_two)

        master_fd = _rotate_credential_tty(master_fd, slave_fd)
        recovery_three = supervisor.launch(recovery_workload, generation=3)
        recovery_three_message = recovery_three.channel.receive()
        authority_three_record = journal.issue_recovery_session_authority(
            primary_authority_sha256=authority.authority_sha256,
            source_attempt_id=attempt.attempt_id,
        )
        authority_three = authority_three_record.event.authority
        assert recovery_three_message.payload == recovery_two_message.payload
        assert authority_two.source_attempt_id == authority_three.source_attempt_id
        assert authority_two.source_client_id == authority_three.source_client_id
        assert authority_two.source_intent_sha256 == authority_three.source_intent_sha256
        assert (authority_two.generation, authority_three.generation) == (2, 3)
        assert journal.frontier(attempt.attempt_id) is FrontierState.UNKNOWN
        _reap_and_verify(supervisor, lifecycle, recovery_three)

        assert SYNTHETIC_CANARY not in "\0".join(child.launch_argv)
        assert child.process.stdout is None
        assert child.process.stderr is None
        assert SYNTHETIC_CANARY.encode("ascii") not in journal.path.read_bytes()
        assert (
            scan_evidence_tree(
                tmp_path,
                canary_tokens=(SYNTHETIC_CANARY,),
            ).leak_count
            == 0
        )
    finally:
        venue.close()
        for descriptor in (master_fd, slave_fd):
            with suppress(OSError):
                os.close(descriptor)


_A5_CORRUPTION_MATRIX = (
    ("truncated", DispatchFailure.TRUNCATED, "IPC_TRUNCATED_BODY"),
    ("oversized", DispatchFailure.OVERSIZED, "IPC_FRAME_OVERSIZED"),
    ("version", DispatchFailure.VERSION, "IPC_VERSION_MISMATCH"),
    ("sequence", DispatchFailure.SEQUENCE, "IPC_SEQUENCE_MISMATCH"),
    ("digest", DispatchFailure.DIGEST, "IPC_DIGEST_MISMATCH"),
    ("eof", DispatchFailure.EOF, "IPC_EOF"),
)


@DARWIN_ONLY
@pytest.mark.parametrize(("case", "failure", "ipc_reason"), _A5_CORRUPTION_MATRIX)
def test_a5_every_corrupt_or_lost_post_go_ipc_result_settles_unknown(
    tmp_path: Path,
    case: str,
    failure: DispatchFailure,
    ipc_reason: str,
) -> None:
    script = """
import hashlib
import json
import os
import struct
import sys
import time
from global_quant.gate1b.execution_kernel import GoCommand
from global_quant.gate1b.process_boundary import credential_child_bootstrap

bootstrap = credential_child_bootstrap()
bootstrap.accept_phase_permit()
message = bootstrap.channel.receive()
assert message.kind == "GO"
GoCommand.from_payload(message.payload)
case = sys.argv[1]
descriptor = bootstrap.channel.fileno()

if case == "eof":
    frame = b""
elif case == "truncated":
    frame = struct.pack(">I", 16) + b"{}"
elif case == "oversized":
    frame = struct.pack(">I", 65537)
else:
    core = {
        "kind": "RESULT",
        "payload": {"status": "sanitized-but-invalid"},
        "sequence": 1,
        "version": 1,
    }
    if case == "version":
        core["version"] = 2
    if case == "sequence":
        core["sequence"] = 99
    canonical = lambda value: json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(canonical(core)).hexdigest()
    envelope = {**core, "digest": "0" * 64 if case == "digest" else digest}
    body = canonical(envelope)
    frame = struct.pack(">I", len(body)) + body

if frame:
    os.write(descriptor, frame)
os.close(descriptor)
time.sleep(30)
"""
    supervisor, journal, lifecycle = _controller(tmp_path)
    child = supervisor.launch(_workload(script, case), generation=1)
    deadline_ns = _attempt_deadline_ns(supervisor)
    authority, _persisted, reserved, attempt = _establish_create_chain(
        journal,
        tmp_path,
        deadline_ns=deadline_ns,
    )
    kernel = _kernel_for_child(supervisor, journal, lifecycle, child)
    permit = supervisor.issue_phase_permit(child, local_limit=5.0)

    kernel.dispatch(
        attempt,
        reserved_request=reserved,
        phase_permit=permit,
    )
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    with pytest.raises(IPCProtocolError, match=ipc_reason):
        child.channel.receive()

    attestation = _reap_and_verify(supervisor, lifecycle, child)
    assert (
        kernel.settle_failure(
            attempt,
            failure=failure,
            reap_attestation=attestation,
        )
        is FrontierState.UNKNOWN
    )
    directive = journal.recovery_directive(attempt.attempt_id)
    assert directive.mode is RecoveryMode.QUERY_PROBE_CLIENT_ID
    assert directive.query_client_id == authority.client_id
    assert directive.allows_post_create is False


def _typed_mutation_attempt(
    kind: MutationKind,
) -> tuple[ReservedRequest, MutationReservationProof, MutationAttempt]:
    intent = _durable_intent(persisted=True)
    probe_id = build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    close_id = build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    precondition = None if kind is MutationKind.CREATE else _sha(f"{kind.value}-proof")
    source = None if kind is MutationKind.CREATE else _sha(f"{kind.value}-source")
    if kind is MutationKind.CREATE:
        purpose = RequestPurpose.CREATE
        mutation_purpose = MutationPurpose.PRIMARY_CREATE
        method = "POST"
        parameters = intent.probe_payload
        ledger = MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("1"),
        )
        client_id = probe_id
    elif kind is MutationKind.CANCEL:
        purpose = RequestPurpose.CANCEL
        mutation_purpose = MutationPurpose.PRIMARY_CANCEL
        method = "DELETE"
        parameters = intent.cancel_parameters
        ledger = MutationLedger(
            total_http_requests=2,
            create_requests=1,
            cancel_requests=1,
            stage=RequestStage.CANCEL_ATTEMPTED,
            last_elapsed_seconds=Decimal("1"),
        )
        client_id = probe_id
    else:
        purpose = RequestPurpose.EMERGENCY_CLOSE
        mutation_purpose = MutationPurpose.PRIMARY_EMERGENCY_CLOSE
        method = "POST"
        parameters = intent.emergency_close_payload(Decimal("0.001"))
        ledger = MutationLedger(
            total_http_requests=2,
            create_requests=1,
            emergency_close_requests=1,
            stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
            last_elapsed_seconds=Decimal("1"),
        )
        client_id = close_id
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method=method,
        path="/fapi/v1/order",
        purpose=purpose,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=mutation_purpose,
        generation=1,
        deadline_ns=104_000_000_000,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=source,
        precondition_sha256=precondition,
    )
    attempt = MutationAttempt.build(
        kind=kind,
        generation=1,
        retry_index=0,
        deadline_ns=104_000_000_000,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=(precondition if kind is MutationKind.CANCEL else None),
    )
    return reserved, proof, attempt


@pytest.mark.parametrize(
    ("kind", "key_kind", "recovery_mode"),
    (
        (
            MutationKind.CREATE,
            ReconciliationKeyKind.PROBE_CLIENT_ID,
            RecoveryMode.QUERY_PROBE_CLIENT_ID,
        ),
        (
            MutationKind.CANCEL,
            ReconciliationKeyKind.PROBE_TERMINAL_STATE,
            RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL,
        ),
        (
            MutationKind.EMERGENCY_CLOSE,
            ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID,
            RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
        ),
    ),
)
def test_a6_all_mutations_share_retry_zero_and_kind_correct_reconciliation(
    kind: MutationKind,
    key_kind: ReconciliationKeyKind,
    recovery_mode: RecoveryMode,
) -> None:
    reserved, proof, attempt = _typed_mutation_attempt(kind)
    permit = PhaseDeadlinePermit.issue(
        generation=1,
        sequence=0,
        absolute_deadline=104.0,
        lifecycle_deadline=110.0,
    )
    command = GoCommand(
        attempt=attempt,
        reserved_request=reserved,
        reservation_proof=proof,
        generation=1,
        lifecycle_deadline_ns=110_000_000_000,
        local_deadline_ns=104_000_000_000,
        go_deadline_ns=104_000_000_000,
        phase_permit_sequence=permit.sequence,
        phase_permit_digest=permit.digest,
        prepared_record_digest=_sha(f"{kind.value}-prepared"),
        go_record_digest=_sha(f"{kind.value}-go"),
    )

    assert GoCommand.from_payload(command.to_payload()) == command
    assert attempt.retry_index == proof.retry_index == reserved.retry_index == 0
    assert attempt.reconciliation_key.kind is key_kind
    assert attempt.reconciliation_key.client_id == attempt.client_id
    assert {
        MutationKind.CREATE: RecoveryMode.QUERY_PROBE_CLIENT_ID,
        MutationKind.CANCEL: RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL,
        MutationKind.EMERGENCY_CLOSE: RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
    }[kind] is recovery_mode
    if kind is MutationKind.EMERGENCY_CLOSE:
        assert attempt.client_id == build_emergency_client_order_id(
            RUNTIME_COMMIT,
            SESSION_NONCE,
        )
    else:
        assert attempt.client_id == build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)

    with pytest.raises(ExecutionJournalError, match="MUTATION_RETRY_FORBIDDEN"):
        MutationAttempt.build(
            kind=kind,
            generation=1,
            retry_index=1,
            deadline_ns=104_000_000_000,
            reservation_sha256=reserved.request_sha256,
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=reserved.intent_sha256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
            fresh_open_proof_sha256=(
                _sha("retry-open-proof") if kind is MutationKind.CANCEL else None
            ),
        )


def _lifecycle_primary_state():
    return start_primary(
        PrimaryJournalProjection(
            reconstruction_sha256=_sha("acceptance-lifecycle-replay"),
            generation=1,
            timing=LifecycleTiming(
                lifecycle_started_at=Decimal("1000"),
                lifecycle_deadline=Decimal("1180"),
            ),
            probe_client_id=build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            close_client_id=build_emergency_client_order_id(
                RUNTIME_COMMIT,
                SESSION_NONCE,
            ),
        )
    )


def _lifecycle_permit(state, action, issued_at: Decimal) -> PhasePermitProjection:
    return PhasePermitProjection(
        generation=state.generation,
        sequence=state.last_permit_sequence + 1,
        action_sha256=action.action_sha256,
        lifecycle_deadline=state.timing.lifecycle_deadline,
        issued_at=issued_at,
        absolute_deadline=min(
            state.timing.lifecycle_deadline,
            action.absolute_deadline_cap,
            issued_at + Decimal("5"),
        ),
        local_limit_seconds=Decimal("5"),
    )


def _lifecycle_read(state, disposition: ReadDisposition, at: Decimal):
    action = plan_next(state)
    state = reserve_http(state, action, _lifecycle_permit(state, action, at))
    return resolve_http(
        state,
        ReadResolution(
            action_sha256=action.action_sha256,
            result_proof_sha256=_sha(f"read-result:{action.action_sha256}"),
            disposition=disposition,
            observed_at=at,
        ),
    )


def _lifecycle_mutation(
    state,
    disposition: MutationDisposition,
    at: Decimal,
    *,
    accepted_at: Decimal | None = None,
):
    action = plan_next(state)
    state = reserve_http(state, action, _lifecycle_permit(state, action, at))
    return resolve_http(
        state,
        MutationResolution(
            action_sha256=action.action_sha256,
            frontier_proof_sha256=_sha(f"frontier:{action.action_sha256}"),
            disposition=disposition,
            observed_at=at,
            accepted_at=accepted_at,
        ),
    )


def _lifecycle_local(state):
    action = plan_next(state)
    return apply_local(
        state,
        action,
        LocalResolution(
            action_sha256=action.action_sha256,
            evidence_sha256=_sha(f"local:{action.action_sha256}"),
            disposition=LocalDisposition.SUCCEEDED,
        ),
    )


def _advance_lifecycle_to_final():
    state = _lifecycle_primary_state()
    for offset, step in enumerate(PRE_INTENT_READ_STEPS):
        assert plan_next(state).step is step
        state = _lifecycle_read(
            state,
            (
                ReadDisposition.ORDER_NOT_FOUND
                if step is Step.PRE_DUPLICATE_ORDER
                else ReadDisposition.VALIDATED
            ),
            Decimal(1001 + offset),
        )
    state = _lifecycle_local(state)
    state = _lifecycle_local(state)
    state = _lifecycle_mutation(
        state,
        MutationDisposition.CONFIRMED,
        Decimal("1012"),
        accepted_at=Decimal("1012"),
    )
    state = _lifecycle_read(state, ReadDisposition.ORDER_NEW, Decimal("1013"))
    state = _lifecycle_mutation(
        state,
        MutationDisposition.CONFIRMED,
        Decimal("1014"),
    )
    assert plan_next(state).step is FINAL_READ_STEPS[0]
    return state


@DARWIN_ONLY
def test_a9_pre_intent_cache_cannot_impersonate_fresh_final_evidence(
    tmp_path: Path,
) -> None:
    initial = _lifecycle_primary_state()
    stale_preflight_action = plan_next(initial)
    final_state = _advance_lifecycle_to_final()
    final_action = plan_next(final_state)
    assert final_action.freshness is Freshness.FINAL_FRESH
    reserved = reserve_http(
        final_state,
        final_action,
        _lifecycle_permit(final_state, final_action, Decimal("1015")),
    )

    with pytest.raises(LifecycleError, match="RESULT_ACTION_MISMATCH"):
        resolve_http(
            reserved,
            ReadResolution(
                action_sha256=stale_preflight_action.action_sha256,
                result_proof_sha256=_sha(f"read-result:{stale_preflight_action.action_sha256}"),
                disposition=ReadDisposition.ORDER_CANCELED_ZERO_FILL,
                observed_at=Decimal("1015"),
            ),
        )
    assert reserved.normal_pass_candidate is False

    evidence = tmp_path / "sanitized-evidence"
    evidence.mkdir(mode=0o700)
    master_fd, slave_fd = pty.openpty()
    try:
        supervisor, journal, lifecycle = _controller(
            evidence,
            credential_stdin=slave_fd,
        )
        child = supervisor.launch(
            _workload(
                """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
canary = "-".join(("acceptance", "credential", "canary"))
bootstrap.install_credential_guard(canary)
bootstrap.channel.send("FINALIZATION_READY", {"ready": True})
time.sleep(30)
"""
            ),
            generation=1,
        )
        assert child.channel.receive().payload == {"ready": True}
        reap = _reap_and_verify(supervisor, lifecycle, child)
    finally:
        for descriptor in (master_fd, slave_fd):
            with suppress(OSError):
                os.close(descriptor)

    finalized = FinalEvidenceFinalizer(
        root=evidence,
        execution_journal_path=journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
        canary_tokens=(SYNTHETIC_CANARY,),
    ).finalize_blocked(
        reap,
        cause=BlockedFinalizationCause.FINAL_READ_SCHEDULE_INCOMPLETE,
    )
    verdict_text = finalized.verdict_path.read_text(encoding="utf-8")
    verdict = json.loads(verdict_text)
    assert verdict["status"] == "BLOCKED"
    assert verdict["review_eligible"] is False
    assert verdict["gate_pass_declared"] is False
    assert "READY_FOR_INDEPENDENT_REVIEW" not in verdict_text
    assert "PASS_GATE" not in verdict_text
    report = scan_evidence_tree(evidence, canary_tokens=(SYNTHETIC_CANARY,))
    assert report.leak_count == 0
    assert report.findings == ()
