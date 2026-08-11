"""Counterexamples for the credential-free Gate 1B execution supervisor."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest

import global_quant.gate1b.supervisor as subject
from global_quant.gate1b.credential_execution_session import (
    ReadCommand,
    ReadFailureResult,
    SessionInitCommand,
)
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.execution_evidence_log import (
    ExecutionEvidenceLog,
    ExecutionEvidenceLogError,
)
from global_quant.gate1b.execution_journal import (
    DurableGenerationAdmission,
    ExecutionJournal,
    FrontierState,
    GenerationCapability,
    IntentBoundRecoveryAuthority,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ProcessReapReceipt,
    ReadFailureKind,
    ReadKind,
    ReadPurpose,
    ReadReservationProof,
    RecoverySessionAuthority,
    SessionAuthority,
)
from global_quant.gate1b.execution_kernel import (
    DispatchKernelError,
    DispatchResult,
    GoCommand,
)
from global_quant.gate1b.execution_lifecycle import (
    ActionKind,
    Freshness,
    LifecycleTiming,
    LocalDisposition,
    PlannedAction,
    PrimaryJournalProjection,
    ReservedAction,
    Step,
)
from global_quant.gate1b.execution_projection import ExecutionProjector
from global_quant.gate1b.final_evidence import (
    BlockedFinalizationCause,
    FinalEvidenceFinalizer,
    FinalizedEvidence,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    MutationLedger,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    build_client_order_id,
    build_emergency_client_order_id,
)
from global_quant.gate1b.process_boundary import (
    AbsoluteDeadline,
    CredentialProcessSupervisor,
    CredentialWorkload,
    IPCCodec,
    IPCMessage,
    ManagedChild,
    PhaseDeadlinePermit,
    ProcessIdentity,
    ProcessLifecycleJournal,
    ReapAttestation,
)
from global_quant.gate1b.runtime_binding import RuntimeSnapshot, SourceBinding

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
INTENT_SHA256 = "3" * 64
NOW = 100.0
LIFECYCLE_DEADLINE = 110.0
PHASE_DEADLINE_NS = 105_000_000_000


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _message(kind: str, payload: dict[str, object], *, sequence: int = 0) -> IPCMessage:
    codec = IPCCodec()
    return codec.decode(
        codec.encode(kind, payload, sequence=sequence),
        expected_sequence=sequence,
    )


class _FakeChannel:
    def __init__(self, tracker: list[str]) -> None:
        self.tracker = tracker
        self.incoming: list[IPCMessage | BaseException] = []
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.before_receive: Any = None

    def send(self, kind: str, payload: Any) -> None:
        self.tracker.append(f"send:{kind}")
        self.sent.append((kind, dict(payload)))

    def receive(self) -> IPCMessage:
        if not self.incoming:
            raise EOFError
        value = self.incoming.pop(0)
        if self.before_receive is not None:
            self.before_receive(value)
        if isinstance(value, BaseException):
            raise value
        self.tracker.append(f"receive:{value.kind}")
        return value


class _FullLoopChannel(_FakeChannel):
    """Typed child double that never owns lifecycle or mutation semantics."""

    def __init__(
        self,
        tracker: list[str],
        *,
        root: Path,
        authority: SessionAuthority,
        read_specs: list[tuple[str, ResponseKind, tuple[tuple[str, object], ...]]],
        capability: GenerationCapability = GenerationCapability.PRIMARY,
        dynamic_session_ready: bool = False,
        fail_go_kind: MutationKind | None = None,
    ) -> None:
        super().__init__(tracker)
        self.root = root
        self.authority = authority
        self.read_specs = read_specs
        self.capability = capability
        self.dynamic_session_ready = dynamic_session_ready
        self.fail_go_kind = fail_go_kind
        self.mutations: list[MutationKind] = []
        self.mutation_attempts: list[MutationAttempt] = []

    def send(self, kind: str, payload: Any) -> None:
        super().send(kind, payload)
        if kind == "READ":
            command = ReadCommand.from_payload(payload)
            expected_path, response_kind, fields = self.read_specs.pop(0)
            reservation = (
                command.pre_intent_reservation
                if command.pre_intent_reservation is not None
                else command.reserved_request
            )
            assert reservation is not None
            assert reservation.path == expected_path
            request_sha256 = (
                reservation.reservation_sha256
                if command.pre_intent_reservation is not None
                else reservation.request_sha256
            )
            result = TransportResult.build(
                request_sha256=request_sha256,
                logical_request_sha256=reservation.logical_request_sha256,
                kind=response_kind,
                fields=fields,
            )
            self.incoming.append(_message("READ_RESULT", command.result_payload(result)))
        elif kind == "GO":
            command = GoCommand.from_payload(payload)
            self.mutations.append(command.attempt.kind)
            self.mutation_attempts.append(command.attempt)
            if command.attempt.kind is self.fail_go_kind:
                self.incoming.append(EOFError())
                return
            status = {
                MutationKind.CREATE: "NEW",
                MutationKind.CANCEL: "CANCELED",
                MutationKind.EMERGENCY_CLOSE: "FILLED",
            }[command.attempt.kind]
            order_id_sha256 = (
                _sha("close-venue-order")
                if command.attempt.kind is MutationKind.EMERGENCY_CLOSE
                else _sha("normal-venue-order")
            )
            transport_result = TransportResult.build(
                request_sha256=command.reserved_request.request_sha256,
                logical_request_sha256=command.reserved_request.logical_request_sha256,
                kind=ResponseKind.MUTATION_ACK,
                fields=(
                    ("clientOrderId", command.attempt.client_id),
                    ("orderIdSha256", order_id_sha256),
                    ("status", status),
                ),
            )
            result = DispatchResult.build(
                command.attempt,
                transport_result=transport_result,
            )
            self.incoming.append(_message("RESULT", result.to_payload()))
        elif kind == "SESSION_INIT" and self.dynamic_session_ready:
            command = SessionInitCommand.from_payload(payload)
            self.incoming.append(_session_ready(command.authority, command.capability))
        elif kind == "BIND_INTENT":
            reference = cast(dict[str, object], payload["reference"])
            self.incoming.append(
                _message(
                    "INTENT_BOUND",
                    {
                        "schema_version": "gate1b.credential-execution-session.v1",
                        "status": "BOUND",
                        "generation": reference["generation"],
                        "authority_sha256": reference["session_authority_sha256"],
                        "binding_sha256": reference["binding_sha256"],
                        "intent_sha256": reference["intent_sha256"],
                        "intent_file_sha256": reference["intent_file_sha256"],
                    },
                )
            )
        elif kind == "SESSION_FINISH":
            preexit = self.root / "child-pre-exit.json"
            preexit.write_text(
                json.dumps(
                    {
                        "capability": self.capability.value,
                        "generation": payload["generation"],
                        "local_exit_pending": True,
                        "loaded_project_modules": [str(Path(subject.__file__).resolve())],
                        "redaction_status": "VERIFIED",
                        "schema_version": "gate1b.credential-child-pre-exit.v1",
                        "session_finished": True,
                        "status": "CHILD_COMPLETE",
                    },
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="ascii",
            )
            os.chmod(preexit, 0o600)
            self.incoming.append(
                _message(
                    "SESSION_FINISHED",
                    {
                        "schema_version": "gate1b.credential-execution-session.v1",
                        "status": "FINISHED",
                        "generation": payload["generation"],
                        "final_state": payload["final_state"],
                        "final_evidence_sha256": payload["final_evidence_sha256"],
                    },
                )
            )


class _FakeProcess:
    def poll(self) -> None:
        return None


@dataclass
class _Harness:
    root: Path
    controller: Any
    journal: ExecutionJournal
    process_journal: ProcessLifecycleJournal
    process_supervisor: CredentialProcessSupervisor
    evidence_log: ExecutionEvidenceLog
    projector: ExecutionProjector
    finalizer: FinalEvidenceFinalizer
    workload: CredentialWorkload
    authority: SessionAuthority
    projection: PrimaryJournalProjection
    child: ManagedChild
    channel: _FakeChannel
    tracker: list[str]
    frontier: dict[str, FrontierState]


def _credential_ready(generation: int, capability: GenerationCapability) -> IPCMessage:
    return _message(
        "CREDENTIAL_READY",
        {
            "schema_version": "gate1b.credential-child.v1",
            "status": "READY",
            "generation": generation,
            "capability": capability.value,
            "guard_installed": True,
        },
    )


def _session_ready(
    authority: SessionAuthority | RecoverySessionAuthority | IntentBoundRecoveryAuthority,
    capability: GenerationCapability = GenerationCapability.PRIMARY,
) -> IPCMessage:
    payload: dict[str, object] = {
        "schema_version": "gate1b.credential-execution-session.v1",
        "status": "READY",
        "generation": authority.generation,
        "capability": capability.value,
        "authority_sha256": authority.authority_sha256,
    }
    if capability is GenerationCapability.RECOVERY:
        payload["intent_sha256"] = authority.source_intent_sha256
    return _message(
        "SESSION_READY",
        payload,
    )


def _reap(
    child: ManagedChild,
    process_journal: ProcessLifecycleJournal,
    *,
    clean: bool = False,
) -> ReapAttestation:
    digest = _sha(f"reap:{child.generation}")
    execution_digest = _sha(f"execution-reap:{child.generation}")
    return ReapAttestation(
        generation=child.generation,
        stage_ordinal=child.generation,
        identity=child.identity,
        process_identity_sha256=child.identity.sha256,
        waited_pid=child.identity.pid,
        returncode=0 if clean else -9,
        signal=None if clean else 9,
        process_journal_path=process_journal.path,
        attested_monotonic_ns=101_000_000_000,
        journal_sequence=2,
        journal_digest=digest,
        journal_head_sequence=2,
        journal_head_digest=digest,
        execution_journal_sequence=2,
        execution_journal_digest=execution_digest,
        execution_head_sequence=2,
        execution_head_digest=execution_digest,
    )


def _kernel_type(
    tracker: list[str],
    frontier: dict[str, FrontierState],
    *,
    authorize_failure: bool = False,
    confirm_failure: bool = False,
) -> type:
    @dataclass
    class _Prepared:
        attempt: MutationAttempt

    @dataclass
    class _Go:
        attempt: MutationAttempt

    class _Kernel:
        def __init__(self, *, channel: _FakeChannel, **_kwargs: object) -> None:
            self.channel = channel

        def prepare(self, attempt: MutationAttempt, **_kwargs: object) -> _Prepared:
            tracker.append("journal:PREPARED")
            frontier[attempt.attempt_id] = FrontierState.PREPARED
            return _Prepared(attempt)

        def authorize_go(self, prepared: _Prepared) -> _Go:
            tracker.append("authorize:GO")
            if authorize_failure:
                raise DispatchKernelError("INJECTED_PRE_GO_FAILURE")
            frontier[prepared.attempt.attempt_id] = FrontierState.GO_DURABLE
            tracker.append("journal:GO_DURABLE")
            return _Go(prepared.attempt)

        def send_go(self, command: _Go) -> None:
            self.channel.send("GO", {"attempt_id": command.attempt.attempt_id})

        def confirm_result(self, command: _Go, _message: IPCMessage) -> FrontierState:
            tracker.append("journal:record_confirmed")
            if confirm_failure:
                raise DispatchKernelError("INJECTED_CONFIRM_DURABILITY_FAILURE")
            frontier[command.attempt.attempt_id] = FrontierState.CONFIRMED
            return FrontierState.CONFIRMED

        def settle_failure(
            self,
            attempt: MutationAttempt,
            **_kwargs: object,
        ) -> FrontierState:
            tracker.append("journal:settle_after_reap")
            current = frontier[attempt.attempt_id]
            settled = (
                FrontierState.NOT_DISPATCHED
                if current is FrontierState.PREPARED
                else FrontierState.UNKNOWN
            )
            frontier[attempt.attempt_id] = settled
            return settled

    return _Kernel


def _components(
    tmp_path: Path,
) -> tuple[
    Path,
    ExecutionJournal,
    ProcessLifecycleJournal,
    CredentialProcessSupervisor,
    ExecutionEvidenceLog,
    CredentialWorkload,
]:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    process_journal = ProcessLifecycleJournal.start(
        root / "lifecycle.jsonl",
        lifecycle_started_at=NOW,
        lifecycle_deadline=LIFECYCLE_DEADLINE,
        execution_journal_path=journal.path,
    )
    process_supervisor = CredentialProcessSupervisor(
        lifecycle_journal=process_journal,
        execution_journal=journal,
        parent_environment={"PATH": "/usr/bin:/bin"},
    )
    process_supervisor.deadline = AbsoluteDeadline(
        LIFECYCLE_DEADLINE,
        clock=lambda: NOW,
    )
    evidence_log = ExecutionEvidenceLog(
        root / "requests.jsonl",
        execution_journal_path=journal.path,
    )
    runtime = Path(subject.__file__).with_name("credential_session.py")
    workload = CredentialWorkload.production(
        runtime,
        runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
    )
    return root, journal, process_journal, process_supervisor, evidence_log, workload


def _normal_read_specs(
    authority: SessionAuthority,
) -> list[tuple[str, ResponseKind, tuple[tuple[str, object], ...]]]:
    wall = 1_786_370_000_000

    def timing(sequence: int) -> tuple[tuple[str, object], ...]:
        monotonic_before = 1_000_000_000 + sequence * 1_000_000
        return (
            ("localMonotonicAfterNs", monotonic_before + 100_000),
            ("localMonotonicBeforeNs", monotonic_before),
            ("localWallAfterMs", wall),
            ("localWallBeforeMs", wall),
        )

    account = (
        (
            "balances",
            [
                {
                    "asset": "USDT",
                    "availableBalance": "100",
                    "walletBalance": "100",
                }
            ],
        ),
        ("canTrade", True),
        ("multiAssetsMargin", False),
        ("nonzeroPositions", []),
    )
    symbol_config = (
        ("isAutoAddMargin", False),
        ("leverage", 1),
        ("marginType", "ISOLATED"),
        ("symbol", "ETHUSDT"),
    )
    exchange_info = (
        ("contractType", "PERPETUAL"),
        (
            "filterTypeCounts",
            {
                "LOT_SIZE": 1,
                "MARKET_LOT_SIZE": 1,
                "MIN_NOTIONAL": 1,
                "PERCENT_PRICE": 1,
                "PRICE_FILTER": 1,
            },
        ),
        (
            "limitLotSize",
            {"maxQuantity": "100", "minQuantity": "0.001", "stepSize": "0.001"},
        ),
        ("marginAsset", "USDT"),
        (
            "marketLotSize",
            {"maxQuantity": "50", "minQuantity": "0.001", "stepSize": "0.001"},
        ),
        ("minNotional", "5"),
        ("orderTypes", ["LIMIT", "MARKET"]),
        ("percentPrice", {"multiplierDown": "0.85", "multiplierUp": "1.05"}),
        ("priceFilter", {"maxPrice": "5000", "minPrice": "1000", "tickSize": "0.01"}),
        ("quoteAsset", "USDT"),
        ("status", "TRADING"),
        ("symbol", "ETHUSDT"),
        ("timeInForce", ["GTC", "GTX"]),
        ("uninterpretedFilterTypes", []),
    )
    probe_or_final_order = (
        ("clientOrderId", authority.client_id),
        ("executedQty", "0"),
        ("orderIdSha256", _sha("normal-venue-order")),
        ("origQty", "0.005"),
        ("positionSide", "BOTH"),
        ("price", "1980"),
        ("reduceOnly", False),
        ("side", "BUY"),
        ("status", "NEW"),
        ("symbol", "ETHUSDT"),
        ("timeInForce", "GTX"),
        ("type", "LIMIT"),
    )
    final_order = tuple(
        (name, "CANCELED" if name == "status" else value) for name, value in probe_or_final_order
    )
    return [
        ("/fapi/v1/time", ResponseKind.SERVER_TIME, (("serverTime", wall), *timing(1))),
        (
            "/fapi/v1/positionSide/dual",
            ResponseKind.POSITION_MODE,
            (("dualSidePosition", False),),
        ),
        ("/fapi/v1/symbolConfig", ResponseKind.SYMBOL_CONFIG, symbol_config),
        ("/fapi/v2/account", ResponseKind.ACCOUNT, account),
        ("/fapi/v1/openOrders", ResponseKind.OPEN_ORDERS, (("count", 0), ("orders", []))),
        (
            "/fapi/v1/openAlgoOrders",
            ResponseKind.OPEN_ALGO_ORDERS,
            (("count", 0), ("orders", [])),
        ),
        ("/fapi/v1/exchangeInfo", ResponseKind.EXCHANGE_INFO, exchange_info),
        (
            "/fapi/v1/order",
            ResponseKind.ORDER_NOT_FOUND,
            (
                ("clientOrderId", authority.client_id),
                ("outcome", "CONFIRMED_NOT_FOUND"),
                ("venueCode", -2013),
            ),
        ),
        ("/fapi/v1/userTrades", ResponseKind.USER_TRADES, (("count", 0), ("trades", []))),
        (
            "/fapi/v1/ticker/bookTicker",
            ResponseKind.BOOK_TICKER,
            (
                ("askPrice", "2000.01"),
                ("askQty", "1"),
                ("bidPrice", "2000"),
                ("bidQty", "1"),
                ("lastUpdateId", 1234),
                ("symbol", "ETHUSDT"),
                ("time", wall - 100),
                *timing(10),
            ),
        ),
        (
            "/fapi/v1/premiumIndex",
            ResponseKind.MARK_PRICE,
            (
                ("markPrice", "2000"),
                ("symbol", "ETHUSDT"),
                ("time", wall - 100),
                *timing(11),
            ),
        ),
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, probe_or_final_order),
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, final_order),
        ("/fapi/v1/openOrders", ResponseKind.OPEN_ORDERS, (("count", 0), ("orders", []))),
        (
            "/fapi/v1/openAlgoOrders",
            ResponseKind.OPEN_ALGO_ORDERS,
            (("count", 0), ("orders", [])),
        ),
        ("/fapi/v1/userTrades", ResponseKind.USER_TRADES, (("count", 0), ("trades", []))),
        ("/fapi/v2/account", ResponseKind.ACCOUNT, account),
        ("/fapi/v1/symbolConfig", ResponseKind.SYMBOL_CONFIG, symbol_config),
        (
            "/fapi/v1/positionSide/dual",
            ResponseKind.POSITION_MODE,
            (("dualSidePosition", False),),
        ),
    ]


def _filled_read_specs(
    authority: SessionAuthority,
) -> list[tuple[str, ResponseKind, tuple[tuple[str, object], ...]]]:
    normal = _normal_read_specs(authority)
    preflight = normal[:11]
    original_order = dict(normal[11][2])
    original_order.update(executedQty="0.003", origQty="0.003", status="FILLED")
    filled_order = tuple(sorted(original_order.items()))
    trade = {
        "commission": "0.001",
        "orderIdSha256": _sha("normal-venue-order"),
        "quantity": "0.003",
        "realizedPnl": "0",
        "tradeIdSha256": _sha("owned-fill-trade"),
    }
    owned_trades = (("count", 1), ("trades", [trade]))
    account = dict(normal[3][2])
    account["nonzeroPositions"] = [
        {
            "positionAmt": "0.003",
            "positionSide": "BOTH",
            "symbol": "ETHUSDT",
        }
    ]
    residual_account = tuple(sorted(account.items()))
    close_order = (
        ("clientOrderId", build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)),
        ("executedQty", "0.003"),
        ("orderIdSha256", _sha("close-venue-order")),
        ("origQty", "0.003"),
        ("positionSide", "BOTH"),
        ("price", "0"),
        ("reduceOnly", True),
        ("side", "SELL"),
        ("status", "FILLED"),
        ("symbol", "ETHUSDT"),
        ("timeInForce", "GTC"),
        ("type", "MARKET"),
    )
    return [
        *preflight,
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, filled_order),
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, filled_order),
        ("/fapi/v1/userTrades", ResponseKind.USER_TRADES, owned_trades),
        ("/fapi/v2/account", ResponseKind.ACCOUNT, residual_account),
        normal[6],
        normal[10],
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, close_order),
        ("/fapi/v1/order", ResponseKind.ORDER_OBSERVATION, filled_order),
        normal[13],
        normal[14],
        ("/fapi/v1/userTrades", ResponseKind.USER_TRADES, owned_trades),
        normal[16],
        normal[17],
        normal[18],
    ]


def _recovery_read_specs(
    authority: SessionAuthority,
) -> list[tuple[str, ResponseKind, tuple[tuple[str, object], ...]]]:
    normal = _normal_read_specs(authority)
    return [normal[11], normal[12], *normal[13:19]]


def _runtime_snapshot(tmp_path: Path) -> RuntimeSnapshot:
    protocol = SourceBinding(
        relative_path="protocols/NT_GATE_1B_V1_6.md",
        git_blob="3" * 40,
        sha256=_sha("protocol"),
        device=1,
        inode=2,
        size=3,
        mtime_ns=4,
        ctime_ns=5,
    )
    module = SourceBinding(
        relative_path="src/global_quant/gate1b/supervisor.py",
        git_blob="4" * 40,
        sha256=_sha("supervisor"),
        device=1,
        inode=6,
        size=7,
        mtime_ns=8,
        ctime_ns=9,
    )
    return RuntimeSnapshot.build(
        project_root=tmp_path,
        runtime_commit=RUNTIME_COMMIT,
        runtime_tree="5" * 40,
        branch="codex/test",
        protocol_commit="6" * 40,
        protocol_tag_object="7" * 40,
        protocol_sha256=protocol.sha256,
        protocol_source=protocol,
        required_project_modules=(module.relative_path,),
        sources=(protocol, module),
    )


def _finalizer(root: Path, journal: ExecutionJournal) -> FinalEvidenceFinalizer:
    return FinalEvidenceFinalizer(
        root=root,
        execution_journal_path=journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    )


def _full_normal_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filled: bool = False,
    fail_go_kind: MutationKind | None = None,
) -> _Harness:
    root, journal, process_journal, process_supervisor, evidence_log, workload = _components(
        tmp_path
    )
    authority = SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    tracker: list[str] = []
    frontier: dict[str, FrontierState] = {}
    channel = _FullLoopChannel(
        tracker,
        root=root,
        authority=authority,
        read_specs=(_filled_read_specs(authority) if filled else _normal_read_specs(authority)),
        fail_go_kind=fail_go_kind,
    )
    channel.incoming.extend(
        [_credential_ready(1, GenerationCapability.PRIMARY), _session_ready(authority)]
    )
    identity = ProcessIdentity(pid=1901, ppid=1, pgid=1901, sid=1901, start_token="full:1")
    child = ManagedChild(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        process=cast(Any, _FakeProcess()),
        identity=identity,
        channel=cast(Any, channel),
        launch_argv=workload.argv,
        workload=workload,
        admission_receipt=None,
    )
    admission_records: dict[int, Any] = {}
    stage_receipts: dict[int, Any] = {}
    observed = [NOW]

    def clock() -> float:
        observed[0] += 0.001
        return observed[0]

    process_supervisor.deadline = AbsoluteDeadline(LIFECYCLE_DEADLINE, clock=clock)

    def launch(
        bound: CredentialProcessSupervisor,
        launched_workload: CredentialWorkload,
        *,
        generation: int,
    ) -> ManagedChild:
        tracker.append("process:launch")
        assert launched_workload is workload
        assert generation == 1
        stage = process_journal.stage_identity(generation, identity)
        admission = journal.admit_generation(
            DurableGenerationAdmission(generation, identity.sha256),
            GenerationCapability.PRIMARY,
        )
        process_journal.record_execution_admission(
            generation=generation,
            identity=identity,
            execution_journal=journal,
            admission_record=admission,
        )
        stage_receipts[generation] = stage
        admission_records[generation] = admission
        child.admission_receipt = stage
        bound._active = child
        return child

    def issue_phase_permit(
        _bound: CredentialProcessSupervisor,
        issued_child: ManagedChild,
        *,
        local_limit: float,
    ) -> PhaseDeadlinePermit:
        tracker.append("process:issue_phase_permit")
        issued_at = process_supervisor.deadline.clock()
        permit = PhaseDeadlinePermit.issue(
            generation=issued_child.generation,
            sequence=issued_child._next_phase_sequence,
            absolute_deadline=min(issued_at + local_limit, LIFECYCLE_DEADLINE),
            lifecycle_deadline=LIFECYCLE_DEADLINE,
        )
        issued_child._next_phase_sequence += 1
        issued_child.channel.send("PHASE_PERMIT", permit.to_payload())
        return permit

    def durable_reap(
        bound: CredentialProcessSupervisor,
        reaped_child: ManagedChild,
        *,
        clean: bool,
    ) -> ReapAttestation:
        tracker.append("process:exact_reap" if clean else "process:kill_and_exact_reap")
        returncode = 0 if clean else -9
        signal = None if clean else 9
        admission = admission_records[reaped_child.generation]
        execution_reap = journal.reap_generation(
            ProcessReapReceipt(
                generation=reaped_child.generation,
                process_identity_sha256=reaped_child.identity.sha256,
                admission_record_sequence=admission.sequence,
                admission_record_digest=admission.digest,
                returncode=returncode,
                signal=signal,
                local_process_quiesced=True,
                venue_mutation_absent_proven=False,
            )
        )
        process_reap = process_journal.record_reap(
            generation=reaped_child.generation,
            identity=reaped_child.identity,
            returncode=returncode,
            signal_number=signal,
            execution_journal=journal,
            execution_reap_record=execution_reap,
        )
        event = process_reap.event
        reaped_child._reaped = True
        bound._active = None
        return ReapAttestation(
            generation=reaped_child.generation,
            stage_ordinal=stage_receipts[reaped_child.generation].stage_ordinal,
            identity=reaped_child.identity,
            process_identity_sha256=reaped_child.identity.sha256,
            waited_pid=reaped_child.identity.pid,
            returncode=returncode,
            signal=signal,
            process_journal_path=process_journal.path,
            attested_monotonic_ns=event["attested_monotonic_ns"],
            journal_sequence=process_reap.sequence,
            journal_digest=process_reap.digest,
            journal_head_sequence=process_reap.sequence,
            journal_head_digest=process_reap.digest,
            execution_journal_sequence=event["execution_journal_sequence"],
            execution_journal_digest=event["execution_journal_digest"],
            execution_head_sequence=event["execution_head_sequence"],
            execution_head_digest=event["execution_head_digest"],
        )

    monkeypatch.setattr(process_supervisor, "launch", MethodType(launch, process_supervisor))
    monkeypatch.setattr(
        process_supervisor,
        "issue_phase_permit",
        MethodType(issue_phase_permit, process_supervisor),
    )
    monkeypatch.setattr(
        process_supervisor,
        "reap",
        MethodType(
            lambda bound, reaped_child, **_kwargs: durable_reap(
                bound,
                reaped_child,
                clean=True,
            ),
            process_supervisor,
        ),
    )
    monkeypatch.setattr(
        process_supervisor,
        "kill_and_reap",
        MethodType(
            lambda bound, reaped_child, **_kwargs: durable_reap(
                bound,
                reaped_child,
                clean=False,
            ),
            process_supervisor,
        ),
    )
    snapshot = _runtime_snapshot(tmp_path)
    projector = ExecutionProjector(
        runtime_snapshot=snapshot,
        execution_journal=journal,
        process_journal=process_journal,
    )
    monkeypatch.setattr(
        subject,
        "verify_runtime_unchanged",
        lambda before, *, loaded_project_module_paths: before,
    )
    finalizer = _finalizer(root, journal)
    controller = subject.ExecutionSupervisor.production(
        workload=workload,
        process_supervisor=process_supervisor,
        execution_journal=journal,
        process_lifecycle_journal=process_journal,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=finalizer,
    )
    projection = PrimaryJournalProjection(
        reconstruction_sha256=_sha("unused-full-projection"),
        generation=1,
        timing=LifecycleTiming(
            lifecycle_started_at=Decimal(str(NOW)),
            lifecycle_deadline=Decimal(str(LIFECYCLE_DEADLINE)),
        ),
        probe_client_id=authority.client_id,
        close_client_id=build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
    )
    return _Harness(
        root=root,
        controller=controller,
        journal=journal,
        process_journal=process_journal,
        process_supervisor=process_supervisor,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=finalizer,
        workload=workload,
        authority=authority,
        projection=projection,
        child=child,
        channel=channel,
        tracker=tracker,
        frontier=frontier,
    )


def _started_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    authorize_failure: bool = False,
    confirm_failure: bool = False,
    start: bool = True,
) -> _Harness:
    (
        root,
        journal,
        process_journal,
        process_supervisor,
        evidence_log,
        workload,
    ) = _components(tmp_path)
    tracker: list[str] = []
    frontier: dict[str, FrontierState] = {}
    monkeypatch.setattr(
        subject,
        "DispatchKernel",
        _kernel_type(
            tracker,
            frontier,
            authorize_failure=authorize_failure,
            confirm_failure=confirm_failure,
        ),
    )
    authority = SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    projection = PrimaryJournalProjection(
        reconstruction_sha256=_sha("primary-projection"),
        generation=1,
        timing=LifecycleTiming(
            lifecycle_started_at=Decimal(str(NOW)),
            lifecycle_deadline=Decimal(str(LIFECYCLE_DEADLINE)),
        ),
        probe_client_id=build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
        close_client_id=build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
    )
    channel = _FakeChannel(tracker)
    channel.incoming.extend(
        [_credential_ready(1, GenerationCapability.PRIMARY), _session_ready(authority)]
    )
    identity = ProcessIdentity(pid=901, ppid=1, pgid=901, sid=901, start_token="fake:1")
    child = ManagedChild(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        process=cast(Any, _FakeProcess()),
        identity=identity,
        channel=cast(Any, channel),
        launch_argv=workload.argv,
        workload=workload,
        admission_receipt=None,
    )

    def launch(
        bound: CredentialProcessSupervisor,
        launched_workload: CredentialWorkload,
        *,
        generation: int,
    ) -> ManagedChild:
        tracker.append("process:launch")
        assert launched_workload is workload
        assert generation == 1
        stage = process_journal.stage_identity(generation, identity)
        admission = journal.admit_generation(
            DurableGenerationAdmission(
                generation=generation,
                process_identity_sha256=identity.sha256,
            ),
            GenerationCapability.PRIMARY,
        )
        process_journal.record_execution_admission(
            generation=generation,
            identity=identity,
            execution_journal=journal,
            admission_record=admission,
        )
        child.admission_receipt = stage
        bound._active = child
        return child

    def issue_phase_permit(
        _bound: CredentialProcessSupervisor,
        issued_child: ManagedChild,
        *,
        local_limit: float,
    ) -> PhaseDeadlinePermit:
        tracker.append("process:issue_phase_permit")
        permit = PhaseDeadlinePermit.issue(
            generation=issued_child.generation,
            sequence=issued_child._next_phase_sequence,
            absolute_deadline=min(NOW + local_limit, LIFECYCLE_DEADLINE),
            lifecycle_deadline=LIFECYCLE_DEADLINE,
        )
        issued_child._next_phase_sequence += 1
        issued_child.channel.send("PHASE_PERMIT", permit.to_payload())
        return permit

    def kill_and_reap(
        bound: CredentialProcessSupervisor,
        killed_child: ManagedChild,
        **_kwargs: object,
    ) -> ReapAttestation:
        tracker.append("process:kill_and_exact_reap")
        bound._active = None
        return _reap(killed_child, process_journal)

    def reap(
        bound: CredentialProcessSupervisor,
        reaped_child: ManagedChild,
        **_kwargs: object,
    ) -> ReapAttestation:
        tracker.append("process:exact_reap")
        bound._active = None
        return _reap(reaped_child, process_journal, clean=True)

    monkeypatch.setattr(process_supervisor, "launch", MethodType(launch, process_supervisor))
    monkeypatch.setattr(
        process_supervisor,
        "issue_phase_permit",
        MethodType(issue_phase_permit, process_supervisor),
    )
    monkeypatch.setattr(
        process_supervisor,
        "kill_and_reap",
        MethodType(kill_and_reap, process_supervisor),
    )
    monkeypatch.setattr(process_supervisor, "reap", MethodType(reap, process_supervisor))

    projector = ExecutionProjector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=journal,
        process_journal=process_journal,
    )
    finalizer = _finalizer(root, journal)
    controller = subject.ExecutionSupervisor.production(
        workload=workload,
        process_supervisor=process_supervisor,
        execution_journal=journal,
        process_lifecycle_journal=process_journal,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=finalizer,
    )
    if start:
        controller.start_primary(authority=authority)
        tracker.clear()
    return _Harness(
        root=root,
        controller=controller,
        journal=journal,
        process_journal=process_journal,
        process_supervisor=process_supervisor,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=finalizer,
        workload=workload,
        authority=authority,
        projection=projection,
        child=child,
        channel=channel,
        tracker=tracker,
        frontier=frontier,
    )


def _create_action() -> PlannedAction:
    return PlannedAction(
        generation=1,
        ordinal=1,
        kind=ActionKind.CREATE,
        step=Step.CREATE,
        method="POST",
        path="/fapi/v1/order",
        parameters=(("requestProfile", "PERSISTED_EXACT_PROBE"),),
        retry_index=0,
        retry_of_action_sha256=None,
        reconciliation_key=None,
        freshness=Freshness.NOT_APPLICABLE,
        final_evidence_claims=(),
        requires_durable_reservation=True,
        requires_fresh_open_proof=False,
        precondition_action_sha256=_sha("intent-binding"),
        local_limit_seconds=Decimal("5"),
        absolute_deadline_cap=Decimal(str(LIFECYCLE_DEADLINE)),
        pass_deadline=None,
        action_sha256=_sha("create-action"),
    )


def _create_attempt() -> tuple[ReservedRequest, MutationReservationProof, MutationAttempt]:
    client_id = build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    ledger = MutationLedger(
        total_http_requests=1,
        create_requests=1,
        stage=RequestStage.CREATE_ATTEMPTED,
        last_elapsed_seconds=Decimal("1"),
    )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=INTENT_SHA256,
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
                    "price": "50000",
                    "quantity": "0.001",
                    "recvWindow": "5000",
                    "reduceOnly": "false",
                    "side": "BUY",
                    "symbol": "ETHUSDT",
                    "timeInForce": "GTX",
                    "type": "LIMIT",
                }.items()
            )
        ),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=PHASE_DEADLINE_NS,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=PHASE_DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    return reserved, proof, attempt


def _install_mutation_seams(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    evidence_result_failure: bool = False,
) -> PlannedAction:
    action = _create_action()
    monkeypatch.setattr(subject, "plan_next", lambda _state: action)
    monkeypatch.setattr(
        subject,
        "reserve_http",
        lambda state, _action, permit: replace(
            state,
            pending=ReservedAction(action=action, permit=permit),
            last_permit_sequence=permit.sequence,
        ),
    )

    def record_exact(_bound: ExecutionJournal, **_kwargs: object) -> object:
        harness.tracker.append("journal:exact_request_fsync")
        return object()

    def record_proof(_bound: ExecutionJournal, _proof: object) -> object:
        harness.tracker.append("journal:mutation_proof_fsync")
        return object()

    def frontier(_bound: ExecutionJournal, attempt_id: str) -> FrontierState:
        return harness.frontier[attempt_id]

    def append_prepared(_bound: ExecutionEvidenceLog, _attempt_id: str) -> object:
        harness.tracker.append("evidence:PREPARED")
        return object()

    def append_result(_bound: ExecutionEvidenceLog, _result: DispatchResult) -> object:
        harness.tracker.append("evidence:RESULT")
        if evidence_result_failure:
            raise ExecutionEvidenceLogError("INJECTED_EVIDENCE_RESULT_FAILURE")
        return object()

    def append_failure(_bound: ExecutionEvidenceLog, _attempt_id: str) -> object:
        harness.tracker.append("evidence:FAILURE")
        return object()

    monkeypatch.setattr(
        harness.journal,
        "record_exact_request_reservation",
        MethodType(record_exact, harness.journal),
    )
    monkeypatch.setattr(
        harness.journal, "record_mutation_reservation", MethodType(record_proof, harness.journal)
    )
    monkeypatch.setattr(harness.journal, "frontier", MethodType(frontier, harness.journal))
    monkeypatch.setattr(
        harness.evidence_log, "append_prepared", MethodType(append_prepared, harness.evidence_log)
    )
    monkeypatch.setattr(
        harness.evidence_log, "append_result", MethodType(append_result, harness.evidence_log)
    )
    monkeypatch.setattr(
        harness.evidence_log, "append_failure", MethodType(append_failure, harness.evidence_log)
    )
    return action


def test_legacy_arbitrary_process_runner_surface_is_gone() -> None:
    source = inspect.getsource(subject)
    assert not hasattr(subject, "run_supervised_session")
    assert not hasattr(subject, "ChildResult")
    assert "child_argv" not in source
    assert "subprocess.run" not in source
    assert "import subprocess" not in source


def test_typed_production_controller_is_the_only_entrypoint() -> None:
    assert hasattr(subject, "ExecutionSupervisor")
    signature = inspect.signature(subject.ExecutionSupervisor.production)
    assert "workload" in signature.parameters
    assert "process_supervisor" in signature.parameters
    assert "execution_journal" in signature.parameters
    assert "process_lifecycle_journal" in signature.parameters
    assert "evidence_log" in signature.parameters
    assert "projector" in signature.parameters
    assert "finalizer" in signature.parameters
    assert "runner" not in signature.parameters
    assert "child_argv" not in signature.parameters
    start_signature = inspect.signature(subject.ExecutionSupervisor.start_primary)
    assert "projection" not in start_signature.parameters
    assert subject.ExecutionCompletion.__dataclass_fields__.keys() >= {
        "fresh_final_reference",
        "finalized_evidence",
    }


def test_supervisor_source_never_writes_a_final_verdict() -> None:
    source = Path(subject.__file__).read_text(encoding="utf-8")
    assert "verdict.json" not in source
    assert "PASS_GATE1B" not in source


def test_primary_execution_has_one_closed_supervisor_owned_entry() -> None:
    execute_primary = subject.ExecutionSupervisor.execute_primary
    signature = inspect.signature(execute_primary)

    assert tuple(signature.parameters) == ("self", "authority")
    assert signature.parameters["authority"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(execute_primary)
    assert "_drive_active_session" in source
    assert "finally:" in source
    assert "_kill_active_child" in source
    assert "while" in inspect.getsource(subject.ExecutionSupervisor._drive_active_session)


def test_primary_full_loop_projects_exact_normal_21_and_finalizes_after_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _full_normal_harness(tmp_path, monkeypatch)

    completion = harness.controller.execute_primary(authority=harness.authority)

    channel = cast(_FullLoopChannel, harness.channel)
    assert channel.read_specs == []
    assert channel.mutations == [MutationKind.CREATE, MutationKind.CANCEL]
    assert completion.lifecycle_state.completed
    assert completion.lifecycle_state.normal_pass_candidate
    assert completion.lifecycle_state.budget.total_http_attempts == 21
    assert completion.lifecycle_state.budget.mutation_requests == 2
    assert completion.fresh_final_reference is not None
    assert completion.finalized_evidence.verification.review_eligible
    assert harness.process_journal.active_generation is None
    assert harness.tracker.index("send:SESSION_FINISH") < harness.tracker.index(
        "process:exact_reap"
    )
    verdict = json.loads(completion.finalized_evidence.verdict_path.read_text(encoding="ascii"))
    assert verdict["status"] == "READY_FOR_INDEPENDENT_REVIEW"
    assert verdict["gate_pass_declared"] is False


def test_primary_full_loop_contains_owned_fill_before_one_emergency_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _full_normal_harness(tmp_path, monkeypatch, filled=True)

    completion = harness.controller.execute_primary(authority=harness.authority)

    channel = cast(_FullLoopChannel, harness.channel)
    assert channel.read_specs == []
    assert channel.mutations == [MutationKind.CREATE, MutationKind.EMERGENCY_CLOSE]
    assert completion.lifecycle_state.completed
    assert not completion.lifecycle_state.normal_pass_candidate
    assert completion.lifecycle_state.budget.total_http_attempts == 27
    assert completion.lifecycle_state.budget.mutation_requests == 2
    assert completion.lifecycle_state.budget.close_requests == 1
    assert completion.fresh_final_reference is not None
    assert not completion.finalized_evidence.verification.review_eligible
    events = [type(record.event).__name__ for record in harness.journal.records()]
    assert events.index("_OwnedFillCloseProven") < max(
        index for index, name in enumerate(events) if name == "_AttemptPrepared"
    )
    assert harness.process_journal.active_generation is None
    assert harness.tracker.index("send:SESSION_FINISH") < harness.tracker.index(
        "process:exact_reap"
    )


def test_new_projector_recovers_durable_create_unknown_without_reposting_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _full_normal_harness(
        tmp_path,
        monkeypatch,
        fail_go_kind=MutationKind.CREATE,
    )
    primary.controller.start_primary(authority=primary.authority)

    assert primary.controller._drive_active_session() is None

    primary_channel = cast(_FullLoopChannel, primary.channel)
    create_attempt = primary_channel.mutation_attempts[0]
    assert primary_channel.mutations == [MutationKind.CREATE]
    assert primary.journal.frontier(create_attempt.attempt_id) is FrontierState.UNKNOWN
    assert primary.process_journal.active_generation is None
    assert primary.controller.active_generation is None

    journal = ExecutionJournal(primary.journal.path)
    process_journal = ProcessLifecycleJournal.restore(primary.process_journal.path)
    process_supervisor = CredentialProcessSupervisor(
        lifecycle_journal=process_journal,
        execution_journal=journal,
        parent_environment={"PATH": "/usr/bin:/bin"},
    )
    observed = [101.0]

    def clock() -> float:
        observed[0] += 0.001
        return observed[0]

    process_supervisor.deadline = AbsoluteDeadline(LIFECYCLE_DEADLINE, clock=clock)
    evidence_log = ExecutionEvidenceLog(
        primary.root / "requests.jsonl",
        execution_journal_path=journal.path,
    )
    workload = CredentialWorkload.production(
        primary.workload.runtime_path,
        runtime_sha256=primary.workload.runtime_sha256,
    )
    projector = ExecutionProjector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=journal,
        process_journal=process_journal,
    )
    assert projector is not primary.projector
    tracker: list[str] = []
    channel = _FullLoopChannel(
        tracker,
        root=primary.root,
        authority=primary.authority,
        read_specs=_recovery_read_specs(primary.authority),
        capability=GenerationCapability.RECOVERY,
        dynamic_session_ready=True,
    )
    channel.incoming.append(_credential_ready(2, GenerationCapability.RECOVERY))
    identity = ProcessIdentity(pid=1902, ppid=1, pgid=1902, sid=1902, start_token="full:2")
    child = ManagedChild(
        generation=2,
        capability=GenerationCapability.RECOVERY,
        process=cast(Any, _FakeProcess()),
        identity=identity,
        channel=cast(Any, channel),
        launch_argv=workload.argv,
        workload=workload,
        admission_receipt=None,
    )
    admission_records: dict[int, Any] = {}
    stage_receipts: dict[int, Any] = {}

    def launch(
        bound: CredentialProcessSupervisor,
        launched_workload: CredentialWorkload,
        *,
        generation: int,
    ) -> ManagedChild:
        tracker.append("process:launch")
        assert launched_workload is workload
        assert generation == 2
        stage = process_journal.stage_identity(generation, identity)
        admission = journal.admit_generation(
            DurableGenerationAdmission(generation, identity.sha256),
            GenerationCapability.RECOVERY,
        )
        process_journal.record_execution_admission(
            generation=generation,
            identity=identity,
            execution_journal=journal,
            admission_record=admission,
        )
        stage_receipts[generation] = stage
        admission_records[generation] = admission
        child.admission_receipt = stage
        bound._active = child
        return child

    def issue_phase_permit(
        _bound: CredentialProcessSupervisor,
        issued_child: ManagedChild,
        *,
        local_limit: float,
    ) -> PhaseDeadlinePermit:
        tracker.append("process:issue_phase_permit")
        issued_at = process_supervisor.deadline.clock()
        permit = PhaseDeadlinePermit.issue(
            generation=issued_child.generation,
            sequence=issued_child._next_phase_sequence,
            absolute_deadline=min(issued_at + local_limit, LIFECYCLE_DEADLINE),
            lifecycle_deadline=LIFECYCLE_DEADLINE,
        )
        issued_child._next_phase_sequence += 1
        issued_child.channel.send("PHASE_PERMIT", permit.to_payload())
        return permit

    def durable_reap(
        bound: CredentialProcessSupervisor,
        reaped_child: ManagedChild,
        *,
        clean: bool,
    ) -> ReapAttestation:
        tracker.append("process:exact_reap" if clean else "process:kill_and_exact_reap")
        returncode = 0 if clean else -9
        signal = None if clean else 9
        admission = admission_records[reaped_child.generation]
        execution_reap = journal.reap_generation(
            ProcessReapReceipt(
                generation=reaped_child.generation,
                process_identity_sha256=reaped_child.identity.sha256,
                admission_record_sequence=admission.sequence,
                admission_record_digest=admission.digest,
                returncode=returncode,
                signal=signal,
                local_process_quiesced=True,
                venue_mutation_absent_proven=False,
            )
        )
        process_reap = process_journal.record_reap(
            generation=reaped_child.generation,
            identity=reaped_child.identity,
            returncode=returncode,
            signal_number=signal,
            execution_journal=journal,
            execution_reap_record=execution_reap,
        )
        event = process_reap.event
        reaped_child._reaped = True
        bound._active = None
        return ReapAttestation(
            generation=reaped_child.generation,
            stage_ordinal=stage_receipts[reaped_child.generation].stage_ordinal,
            identity=reaped_child.identity,
            process_identity_sha256=reaped_child.identity.sha256,
            waited_pid=reaped_child.identity.pid,
            returncode=returncode,
            signal=signal,
            process_journal_path=process_journal.path,
            attested_monotonic_ns=event["attested_monotonic_ns"],
            journal_sequence=process_reap.sequence,
            journal_digest=process_reap.digest,
            journal_head_sequence=process_reap.sequence,
            journal_head_digest=process_reap.digest,
            execution_journal_sequence=event["execution_journal_sequence"],
            execution_journal_digest=event["execution_journal_digest"],
            execution_head_sequence=event["execution_head_sequence"],
            execution_head_digest=event["execution_head_digest"],
        )

    monkeypatch.setattr(process_supervisor, "launch", MethodType(launch, process_supervisor))
    monkeypatch.setattr(
        process_supervisor,
        "issue_phase_permit",
        MethodType(issue_phase_permit, process_supervisor),
    )
    monkeypatch.setattr(
        process_supervisor,
        "reap",
        MethodType(
            lambda bound, reaped_child, **_kwargs: durable_reap(
                bound,
                reaped_child,
                clean=True,
            ),
            process_supervisor,
        ),
    )
    monkeypatch.setattr(
        process_supervisor,
        "kill_and_reap",
        MethodType(
            lambda bound, reaped_child, **_kwargs: durable_reap(
                bound,
                reaped_child,
                clean=False,
            ),
            process_supervisor,
        ),
    )
    controller = subject.ExecutionSupervisor.production(
        workload=workload,
        process_supervisor=process_supervisor,
        execution_journal=journal,
        process_lifecycle_journal=process_journal,
        evidence_log=evidence_log,
        projector=projector,
        finalizer=_finalizer(primary.root, journal),
    )

    completion = controller.execute_recovery(primary_authority=primary.authority)

    assert channel.read_specs == []
    assert channel.mutations == [MutationKind.CANCEL]
    cancel_attempt = channel.mutation_attempts[0]
    assert cancel_attempt.client_id == create_attempt.client_id == primary.authority.client_id
    assert cancel_attempt.recovery_of_attempt_id == create_attempt.attempt_id
    assert completion.session_start.capability is GenerationCapability.RECOVERY
    assert completion.recovery_generations == (2,)
    assert completion.lifecycle_state.completed
    assert completion.lifecycle_state.budget.total_http_attempts == 21
    assert completion.fresh_final_reference is not None
    assert not completion.finalized_evidence.verification.review_eligible
    assert process_journal.active_generation is None
    assert tracker.index("send:SESSION_FINISH") < tracker.index("process:exact_reap")


def test_primary_execution_finally_exactly_reaps_on_projector_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, start=False)

    def fail_drive(_controller: subject.ExecutionSupervisor) -> subject.SessionExit:
        raise RuntimeError("injected projector failure")

    monkeypatch.setattr(subject.ExecutionSupervisor, "_drive_active_session", fail_drive)
    finalized = FinalizedEvidence(
        verification=cast(Any, object()),
        process_exit_path=harness.root / "process-exit.json",
        manifest_path=harness.root / "manifest.json",
        manifest_hash_path=harness.root / "manifest.json.sha256",
        verdict_path=harness.root / "verdict.json",
        verdict_hash_path=harness.root / "verdict.json.sha256",
    )

    def finalize_blocked(
        _bound: FinalEvidenceFinalizer,
        _reap: ReapAttestation,
        *,
        cause: object,
    ) -> FinalizedEvidence:
        harness.tracker.append("finalizer:crash_blocked")
        assert cause is not None
        return finalized

    monkeypatch.setattr(
        harness.finalizer,
        "finalize_blocked",
        MethodType(finalize_blocked, harness.finalizer),
    )

    with pytest.raises(subject.SupervisorError, match="PRIMARY_EXECUTION_FAILED"):
        harness.controller.execute_primary(authority=harness.authority)

    assert "process:kill_and_exact_reap" in harness.tracker
    assert harness.tracker.index("process:kill_and_exact_reap") < harness.tracker.index(
        "finalizer:crash_blocked"
    )
    assert harness.controller.active_generation is None


def test_primary_handshake_failure_finalizes_blocked_only_after_exact_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, start=False)
    harness.channel.incoming[:] = [
        _credential_ready(1, GenerationCapability.PRIMARY),
        EOFError(),
    ]
    finalized = FinalizedEvidence(
        verification=cast(Any, object()),
        process_exit_path=harness.root / "process-exit.json",
        manifest_path=harness.root / "manifest.json",
        manifest_hash_path=harness.root / "manifest.json.sha256",
        verdict_path=harness.root / "verdict.json",
        verdict_hash_path=harness.root / "verdict.json.sha256",
    )

    def finalize_blocked(
        _bound: FinalEvidenceFinalizer,
        _reap: ReapAttestation,
        *,
        cause: object,
    ) -> FinalizedEvidence:
        assert cause is not None
        harness.tracker.append("finalizer:handshake_blocked")
        return finalized

    monkeypatch.setattr(
        harness.finalizer,
        "finalize_blocked",
        MethodType(finalize_blocked, harness.finalizer),
    )

    with pytest.raises(subject.SupervisorError, match="PRIMARY_SESSION_START_FAILED"):
        harness.controller.execute_primary(authority=harness.authority)

    assert harness.tracker.index("process:kill_and_exact_reap") < harness.tracker.index(
        "finalizer:handshake_blocked"
    )
    assert harness.controller.active_generation is None


def test_recovery_loop_relaunches_generation_three_with_same_primary_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, start=False)
    starts: list[SessionAuthority] = []
    generations = iter((2, 3))
    terminal = cast(subject._DrivenSessionExit, object())
    outcomes: list[subject._DrivenSessionExit | None] = [None, terminal]

    def start_recovery(
        _controller: subject.ExecutionSupervisor,
        *,
        primary_authority: SessionAuthority,
    ) -> subject.SessionStart:
        starts.append(primary_authority)
        generation = next(generations)
        return subject.SessionStart(
            generation=generation,
            capability=GenerationCapability.RECOVERY,
            authority_sha256=_sha(f"recovery:{generation}"),
            reconstruction_sha256=_sha(f"projection:{generation}"),
        )

    monkeypatch.setattr(subject.ExecutionSupervisor, "start_recovery", start_recovery)
    monkeypatch.setattr(
        subject.ExecutionSupervisor,
        "_drive_active_session",
        lambda _controller: outcomes.pop(0),
    )

    result, launched = harness.controller._recover_until_terminal(
        primary_authority=harness.authority,
    )

    assert result is terminal
    assert tuple(item.generation for item in launched) == (2, 3)
    assert starts == [harness.authority, harness.authority]
    assert len({authority.client_id for authority in starts}) == 1


def test_recovery_loop_continues_after_reaped_session_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, start=False)
    starts: list[SessionAuthority] = []
    terminal = cast(subject._DrivenSessionExit, object())

    def start_recovery(
        controller: subject.ExecutionSupervisor,
        *,
        primary_authority: SessionAuthority,
    ) -> subject.SessionStart:
        starts.append(primary_authority)
        if len(starts) == 1:
            controller._last_reap_attestation = _reap(
                harness.child,
                harness.process_journal,
            )
            raise subject.SupervisorError("RECOVERY_SESSION_START_FAILED")
        return subject.SessionStart(
            generation=3,
            capability=GenerationCapability.RECOVERY,
            authority_sha256=_sha("recovery:3"),
            reconstruction_sha256=_sha("projection:3"),
        )

    monkeypatch.setattr(subject.ExecutionSupervisor, "start_recovery", start_recovery)
    monkeypatch.setattr(
        subject.ExecutionSupervisor,
        "_drive_active_session",
        lambda _controller: terminal,
    )

    result, launched = harness.controller._recover_until_terminal(
        primary_authority=harness.authority,
    )

    assert result is terminal
    assert tuple(item.generation for item in launched) == (3,)
    assert starts == [harness.authority, harness.authority]
    assert len({authority.client_id for authority in starts}) == 1


def test_production_factory_rejects_test_only_workload(tmp_path: Path) -> None:
    (
        root,
        journal,
        process_journal,
        process_supervisor,
        evidence_log,
        _workload,
    ) = _components(tmp_path)
    projector = ExecutionProjector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=journal,
        process_journal=process_journal,
    )
    finalizer = _finalizer(root, journal)
    with pytest.raises(subject.SupervisorError, match="PRODUCTION_WORKLOAD_REQUIRED"):
        subject.ExecutionSupervisor.production(
            workload=CredentialWorkload.test_only(("python", "fake.py")),
            process_supervisor=process_supervisor,
            execution_journal=journal,
            process_lifecycle_journal=process_journal,
            evidence_log=evidence_log,
            projector=projector,
            finalizer=finalizer,
        )


def test_production_factory_rejects_projector_subclass(tmp_path: Path) -> None:
    (
        root,
        journal,
        process_journal,
        process_supervisor,
        evidence_log,
        workload,
    ) = _components(tmp_path)

    class _Projector(ExecutionProjector):
        pass

    projector = _Projector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=journal,
        process_journal=process_journal,
    )
    finalizer = _finalizer(root, journal)
    with pytest.raises(subject.SupervisorError, match="EXECUTION_PROJECTOR_REQUIRED"):
        subject.ExecutionSupervisor.production(
            workload=workload,
            process_supervisor=process_supervisor,
            execution_journal=journal,
            process_lifecycle_journal=process_journal,
            evidence_log=evidence_log,
            projector=projector,
            finalizer=finalizer,
        )


def test_production_factory_rejects_finalizer_subclass(tmp_path: Path) -> None:
    (
        root,
        journal,
        process_journal,
        process_supervisor,
        evidence_log,
        workload,
    ) = _components(tmp_path)

    class _Finalizer(FinalEvidenceFinalizer):
        pass

    projector = ExecutionProjector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=journal,
        process_journal=process_journal,
    )
    finalizer = _Finalizer(
        root=root,
        execution_journal_path=journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    )
    with pytest.raises(
        subject.SupervisorError,
        match="FINAL_EVIDENCE_FINALIZER_REQUIRED",
    ):
        subject.ExecutionSupervisor.production(
            workload=workload,
            process_supervisor=process_supervisor,
            execution_journal=journal,
            process_lifecycle_journal=process_journal,
            evidence_log=evidence_log,
            projector=projector,
            finalizer=finalizer,
        )


def test_first_read_uses_permit_sequence_zero_and_is_durable_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    original = harness.journal.reserve_pre_intent_read

    def reserve(_bound: ExecutionJournal, **kwargs: object) -> object:
        harness.tracker.append("journal:pre_intent_reservation_fsync")
        return original(**kwargs)

    monkeypatch.setattr(
        harness.journal,
        "reserve_pre_intent_read",
        MethodType(reserve, harness.journal),
    )
    pending = harness.controller.reserve_pre_intent_read()

    assert pending.phase.permit.sequence == 0
    assert pending.phase.projection.sequence == 0
    assert pending.phase.projection.absolute_deadline == Decimal("105.0")
    assert pending.command.deadline_ns == PHASE_DEADLINE_NS
    assert harness.tracker.index("process:issue_phase_permit") < harness.tracker.index(
        "journal:pre_intent_reservation_fsync"
    )
    assert harness.tracker.index("journal:pre_intent_reservation_fsync") < harness.tracker.index(
        "send:READ"
    )


def test_terminal_observation_cannot_arrive_after_lifecycle_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    harness.process_supervisor.deadline = AbsoluteDeadline(
        LIFECYCLE_DEADLINE,
        clock=lambda: LIFECYCLE_DEADLINE + 0.001,
    )

    with pytest.raises(subject.SupervisorError, match="LIFECYCLE_DEADLINE_EXHAUSTED"):
        harness.controller._observed_at()


def test_typed_read_failure_is_journaled_without_killing_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    pending = harness.controller.reserve_pre_intent_read()
    failure = ReadFailureResult.build(
        pending.command,
        failure_kind=ReadFailureKind.TIMEOUT,
        io_may_have_occurred=True,
    )
    harness.channel.incoming.append(_message("READ_FAILURE", failure.to_payload()))
    original = harness.journal.record_pre_intent_read_failure

    def record_failure(_bound: ExecutionJournal, **kwargs: object) -> object:
        harness.tracker.append("journal:read_failure_fsync")
        return original(**kwargs)

    monkeypatch.setattr(
        harness.journal,
        "record_pre_intent_read_failure",
        MethodType(record_failure, harness.journal),
    )

    outcome = harness.controller.receive_read()

    assert isinstance(outcome, subject.DurableReadFailureOutcome)
    assert outcome.failure.failure_kind is ReadFailureKind.TIMEOUT
    assert outcome.failure.io_may_have_occurred is True
    assert "journal:read_failure_fsync" in harness.tracker
    assert "process:kill_and_exact_reap" not in harness.tracker
    assert harness.controller.active_generation == 1


def test_exact_read_failure_is_durably_closed_without_mutation_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    client_id = build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    source_attempt_id = _sha("source-create-attempt")
    action = PlannedAction(
        generation=1,
        ordinal=1,
        kind=ActionKind.READ,
        step=Step.PROBE_ORDER,
        method="GET",
        path="/fapi/v1/order",
        parameters=(("origClientOrderId", client_id),),
        retry_index=0,
        retry_of_action_sha256=None,
        reconciliation_key=client_id,
        freshness=Freshness.POST_MUTATION,
        final_evidence_claims=(),
        requires_durable_reservation=True,
        requires_fresh_open_proof=False,
        precondition_action_sha256=source_attempt_id,
        local_limit_seconds=Decimal("5"),
        absolute_deadline_cap=Decimal(str(LIFECYCLE_DEADLINE)),
        pass_deadline=None,
        action_sha256=_sha("probe-order-action"),
    )
    ledger = MutationLedger(
        total_http_requests=2,
        create_requests=1,
        post_create_read_requests=1,
        stage=RequestStage.CREATE_ATTEMPTED,
        last_elapsed_seconds=Decimal("1"),
    )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=INTENT_SHA256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=(("origClientOrderId", client_id),),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )
    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        generation=1,
        deadline_ns=PHASE_DEADLINE_NS,
        source_attempt_id=source_attempt_id,
        client_id=client_id,
        authorization_id=AUTHORIZATION_ID,
    )
    monkeypatch.setattr(subject, "plan_next", lambda _state: action)
    monkeypatch.setattr(
        subject,
        "reserve_http",
        lambda state, _action, permit: replace(
            state,
            pending=ReservedAction(action=action, permit=permit),
            last_permit_sequence=permit.sequence,
        ),
    )
    monkeypatch.setattr(
        harness.journal,
        "record_exact_request_reservation",
        MethodType(lambda _bound, **_kwargs: object(), harness.journal),
    )
    monkeypatch.setattr(
        harness.journal,
        "record_read_prepared",
        MethodType(lambda _bound, _proof: object(), harness.journal),
    )
    pending = harness.controller.reserve_exact_read(
        reserved_request=reserved,
        reservation_proof=proof,
    )
    failure = ReadFailureResult.build(
        pending.command,
        failure_kind=ReadFailureKind.TIMEOUT,
        io_may_have_occurred=True,
    )
    harness.channel.incoming.append(_message("READ_FAILURE", failure.to_payload()))

    def record_failure(_bound: ExecutionJournal, **kwargs: object) -> object:
        harness.tracker.append("journal:exact_read_failure_fsync")
        assert kwargs["request_sha256"] == reserved.request_sha256
        return SimpleNamespace(sequence=41, digest=_sha("exact-read-failure"))

    monkeypatch.setattr(
        harness.journal,
        "record_exact_read_failure",
        MethodType(record_failure, harness.journal),
    )

    outcome = harness.controller.receive_read()

    assert type(outcome) is subject.DurableReadFailureOutcome
    assert outcome.failure.failure_kind is ReadFailureKind.TIMEOUT
    assert outcome.journal_record_sequence == 41
    assert "journal:exact_read_failure_fsync" in harness.tracker
    assert "journal:settle_after_reap" not in harness.tracker
    assert "process:kill_and_exact_reap" not in harness.tracker
    assert harness.controller.active_generation == 1


def test_go_eof_kills_exact_child_then_settles_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    _install_mutation_seams(harness, monkeypatch)
    reserved, proof, attempt = _create_attempt()
    harness.channel.incoming.append(EOFError())

    outcome = harness.controller.dispatch_mutation(
        reserved_request=reserved,
        reservation_proof=proof,
        attempt=attempt,
    )

    assert outcome.frontier is FrontierState.UNKNOWN
    assert outcome.reap_attestation is not None
    assert harness.tracker.index("journal:GO_DURABLE") < harness.tracker.index(
        "process:kill_and_exact_reap"
    )
    assert harness.tracker.index("process:kill_and_exact_reap") < harness.tracker.index(
        "journal:settle_after_reap"
    )
    assert harness.controller.active_generation is None


def test_pre_go_failure_requires_reap_before_not_dispatched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, authorize_failure=True)
    _install_mutation_seams(harness, monkeypatch)
    reserved, proof, attempt = _create_attempt()

    outcome = harness.controller.dispatch_mutation(
        reserved_request=reserved,
        reservation_proof=proof,
        attempt=attempt,
    )

    assert "send:GO" not in harness.tracker
    assert outcome.frontier is FrontierState.NOT_DISPATCHED
    assert harness.tracker.index("process:kill_and_exact_reap") < harness.tracker.index(
        "journal:settle_after_reap"
    )


def test_result_confirm_is_authoritative_before_secondary_evidence_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    _install_mutation_seams(harness, monkeypatch)
    reserved, proof, attempt = _create_attempt()
    transport = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(("clientOrderId", attempt.client_id), ("status", "NEW")),
    )
    result = DispatchResult.build(attempt, transport_result=transport)
    harness.channel.incoming.append(_message("RESULT", result.to_payload()))

    outcome = harness.controller.dispatch_mutation(
        reserved_request=reserved,
        reservation_proof=proof,
        attempt=attempt,
    )

    assert outcome.frontier is FrontierState.CONFIRMED
    assert outcome.evidence_eligible is True
    assert harness.tracker.index("journal:record_confirmed") < harness.tracker.index(
        "evidence:RESULT"
    )
    assert "process:kill_and_exact_reap" not in harness.tracker


def test_confirm_durability_failure_after_go_reaps_to_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, confirm_failure=True)
    _install_mutation_seams(harness, monkeypatch)
    reserved, proof, attempt = _create_attempt()
    transport = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(("clientOrderId", attempt.client_id), ("status", "NEW")),
    )
    result = DispatchResult.build(attempt, transport_result=transport)
    harness.channel.incoming.append(_message("RESULT", result.to_payload()))

    outcome = harness.controller.dispatch_mutation(
        reserved_request=reserved,
        reservation_proof=proof,
        attempt=attempt,
    )

    assert outcome.frontier is FrontierState.UNKNOWN
    assert harness.tracker.index("journal:record_confirmed") < harness.tracker.index(
        "process:kill_and_exact_reap"
    )
    assert harness.tracker.index("process:kill_and_exact_reap") < harness.tracker.index(
        "journal:settle_after_reap"
    )


def test_secondary_evidence_failure_preserves_confirmed_but_blocks_final_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    _install_mutation_seams(harness, monkeypatch, evidence_result_failure=True)
    reserved, proof, attempt = _create_attempt()
    transport = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(("clientOrderId", attempt.client_id), ("status", "NEW")),
    )
    result = DispatchResult.build(attempt, transport_result=transport)
    harness.channel.incoming.append(_message("RESULT", result.to_payload()))

    outcome = harness.controller.dispatch_mutation(
        reserved_request=reserved,
        reservation_proof=proof,
        attempt=attempt,
    )

    assert outcome.frontier is FrontierState.CONFIRMED
    assert outcome.evidence_eligible is False
    assert harness.controller.final_evidence_eligible is False
    assert "journal:settle_after_reap" not in harness.tracker
    assert "process:kill_and_exact_reap" in harness.tracker


def test_recovery_cannot_launch_before_primary_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    with pytest.raises(subject.SupervisorError, match="OLD_CREDENTIAL_CHILD_NOT_REAPED"):
        harness.controller.start_recovery(
            primary_authority=harness.authority,
        )
    assert "process:launch" not in harness.tracker


def test_finish_validates_typed_preexit_before_exact_reap_and_never_emits_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    loaded_module = Path(subject.__file__).resolve()
    action = PlannedAction(
        generation=1,
        ordinal=0,
        kind=ActionKind.COMPLETE_CHILD,
        step=Step.COMPLETE_CHILD,
        method=None,
        path=None,
        parameters=(),
        retry_index=0,
        retry_of_action_sha256=None,
        reconciliation_key=None,
        freshness=Freshness.NOT_APPLICABLE,
        final_evidence_claims=(),
        requires_durable_reservation=False,
        requires_fresh_open_proof=False,
        precondition_action_sha256=None,
        local_limit_seconds=None,
        absolute_deadline_cap=Decimal(str(LIFECYCLE_DEADLINE)),
        pass_deadline=None,
        action_sha256=_sha("complete-child"),
    )
    monkeypatch.setattr(subject, "plan_next", lambda _state: action)
    monkeypatch.setattr(
        subject,
        "apply_local",
        lambda state, _action, result: replace(
            state,
            block_reason=(
                None if result.disposition is LocalDisposition.SUCCEEDED else "FINISH_FAILED"
            ),
        ),
    )
    harness.channel.incoming.append(
        _message(
            "SESSION_FINISHED",
            {
                "schema_version": "gate1b.credential-execution-session.v1",
                "status": "FINISHED",
                "generation": 1,
                "final_state": "COMPLETED",
                "final_evidence_sha256": _sha("fresh-final-evidence"),
            },
        )
    )

    def publish_preexit(value: object) -> None:
        if not isinstance(value, IPCMessage) or value.kind != "SESSION_FINISHED":
            return
        harness.tracker.append("child:typed_preexit_durable")
        path = harness.root / "child-pre-exit.json"
        path.write_text(
            json.dumps(
                {
                    "capability": "PRIMARY",
                    "generation": 1,
                    "local_exit_pending": True,
                    "loaded_project_modules": [str(loaded_module)],
                    "redaction_status": "VERIFIED",
                    "schema_version": "gate1b.credential-child-pre-exit.v1",
                    "session_finished": True,
                    "status": "CHILD_COMPLETE",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    harness.channel.before_receive = publish_preexit

    def verify_runtime(
        before: RuntimeSnapshot,
        *,
        loaded_project_module_paths: tuple[Path, ...],
    ) -> RuntimeSnapshot:
        harness.tracker.append("runtime:post_reap_verified")
        assert loaded_project_module_paths == (loaded_module,)
        return before

    monkeypatch.setattr(
        subject,
        "verify_runtime_unchanged",
        verify_runtime,
        raising=False,
    )
    result = harness.controller.finish(
        final_evidence_sha256=_sha("fresh-final-evidence"),
    )

    assert result.reap_attestation is not None
    assert harness.tracker.index("send:SESSION_FINISH") < harness.tracker.index(
        "child:typed_preexit_durable"
    )
    assert harness.tracker.index("child:typed_preexit_durable") < harness.tracker.index(
        "process:exact_reap"
    )
    assert harness.tracker.index("process:exact_reap") < harness.tracker.index(
        "runtime:post_reap_verified"
    )
    assert result.loaded_project_module_paths == (loaded_module,)
    assert type(result.post_runtime_snapshot) is RuntimeSnapshot
    assert not (harness.root / "verdict.json").exists()

    finalized = FinalizedEvidence(
        verification=cast(Any, object()),
        process_exit_path=harness.root / "process-exit.json",
        manifest_path=harness.root / "manifest.json",
        manifest_hash_path=harness.root / "manifest.json.sha256",
        verdict_path=harness.root / "verdict.json",
        verdict_hash_path=harness.root / "verdict.json.sha256",
    )

    def finalize_blocked(
        _bound: FinalEvidenceFinalizer,
        reap: ReapAttestation,
        *,
        cause: object,
    ) -> FinalizedEvidence:
        harness.tracker.append("finalizer:blocked")
        assert reap == result.reap_attestation
        assert cause is not None
        return finalized

    monkeypatch.setattr(
        harness.finalizer,
        "finalize_blocked",
        MethodType(finalize_blocked, harness.finalizer),
    )
    completion = harness.controller._finalize_completion(
        session_start=subject.SessionStart(
            generation=1,
            capability=GenerationCapability.PRIMARY,
            authority_sha256=harness.authority.authority_sha256,
            reconstruction_sha256=harness.projection.reconstruction_sha256,
        ),
        driven=subject._DrivenSessionExit(
            session_exit=result,
            fresh_final_reference=None,
        ),
        recovery_generations=(),
    )

    assert harness.tracker.index("runtime:post_reap_verified") < harness.tracker.index(
        "finalizer:blocked"
    )
    assert completion.fresh_final_reference is None
    assert completion.finalized_evidence is finalized


def test_runtime_verification_failure_is_distinct_after_exact_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch)
    loaded_module = Path(subject.__file__).resolve()
    action = PlannedAction(
        generation=1,
        ordinal=0,
        kind=ActionKind.COMPLETE_CHILD,
        step=Step.COMPLETE_CHILD,
        method=None,
        path=None,
        parameters=(),
        retry_index=0,
        retry_of_action_sha256=None,
        reconciliation_key=None,
        freshness=Freshness.NOT_APPLICABLE,
        final_evidence_claims=(),
        requires_durable_reservation=False,
        requires_fresh_open_proof=False,
        precondition_action_sha256=None,
        local_limit_seconds=None,
        absolute_deadline_cap=Decimal(str(LIFECYCLE_DEADLINE)),
        pass_deadline=None,
        action_sha256=_sha("complete-child-runtime-drift"),
    )
    monkeypatch.setattr(subject, "plan_next", lambda _state: action)
    harness.channel.incoming.append(
        _message(
            "SESSION_FINISHED",
            {
                "schema_version": "gate1b.credential-execution-session.v1",
                "status": "FINISHED",
                "generation": 1,
                "final_state": "COMPLETED",
                "final_evidence_sha256": _sha("runtime-drift-evidence"),
            },
        )
    )

    def wait_preexit(
        _controller: subject.ExecutionSupervisor,
        _active: object,
        _phase: object,
    ) -> tuple[Path, str, tuple[Path, ...]]:
        harness.tracker.append("child:typed_preexit_durable")
        return (
            harness.root / "child-pre-exit.json",
            _sha("child-pre-exit"),
            (loaded_module,),
        )

    monkeypatch.setattr(
        harness.controller,
        "_wait_for_child_preexit",
        MethodType(wait_preexit, harness.controller),
    )

    def fail_runtime(
        _before: RuntimeSnapshot,
        *,
        loaded_project_module_paths: tuple[Path, ...],
    ) -> RuntimeSnapshot:
        assert loaded_project_module_paths == (loaded_module,)
        harness.tracker.append("runtime:verification_failed")
        raise RuntimeError("injected runtime drift")

    monkeypatch.setattr(subject, "verify_runtime_unchanged", fail_runtime)

    with pytest.raises(subject.SupervisorError, match="RUNTIME_BINDING_FAILED"):
        harness.controller.finish(
            final_evidence_sha256=_sha("runtime-drift-evidence"),
        )

    assert harness.tracker.index("process:exact_reap") < harness.tracker.index(
        "runtime:verification_failed"
    )
    assert harness.controller.active_generation is None


def test_runtime_verification_failure_never_launches_recovery_or_ready_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _started_harness(tmp_path, monkeypatch, start=False)
    harness.controller._persisted_intent = cast(Any, object())
    harness.controller._last_reap_attestation = _reap(
        harness.child,
        harness.process_journal,
        clean=True,
    )
    finalized = FinalizedEvidence(
        verification=cast(Any, object()),
        process_exit_path=harness.root / "process-exit.json",
        manifest_path=harness.root / "manifest.json",
        manifest_hash_path=harness.root / "manifest.json.sha256",
        verdict_path=harness.root / "verdict.json",
        verdict_hash_path=harness.root / "verdict.json.sha256",
    )
    blocked_causes: list[BlockedFinalizationCause] = []

    monkeypatch.setattr(
        subject.ExecutionSupervisor,
        "start_primary",
        lambda _controller, *, authority: subject.SessionStart(
            generation=1,
            capability=GenerationCapability.PRIMARY,
            authority_sha256=authority.authority_sha256,
            reconstruction_sha256=_sha("primary-reconstruction"),
        ),
    )
    monkeypatch.setattr(
        subject.ExecutionSupervisor,
        "_drive_active_session",
        lambda _controller: (_ for _ in ()).throw(
            subject.SupervisorError("RUNTIME_BINDING_FAILED")
        ),
    )

    def forbidden_recovery(
        _controller: subject.ExecutionSupervisor,
        *,
        primary_authority: SessionAuthority,
    ) -> object:
        raise AssertionError(f"recovery launched for {primary_authority.client_id}")

    monkeypatch.setattr(
        subject.ExecutionSupervisor,
        "_recover_until_terminal",
        forbidden_recovery,
    )

    def forbidden_ready_finalize(
        _bound: FinalEvidenceFinalizer,
        _bundle: object,
        _reap: ReapAttestation,
    ) -> FinalizedEvidence:
        raise AssertionError("READY finalizer called after runtime drift")

    monkeypatch.setattr(
        harness.finalizer,
        "finalize",
        MethodType(forbidden_ready_finalize, harness.finalizer),
    )
    monkeypatch.setattr(
        harness.finalizer,
        "finalize_blocked",
        MethodType(
            lambda _bound, _reap, *, cause: (
                blocked_causes.append(cause),
                finalized,
            )[1],
            harness.finalizer,
        ),
    )

    with pytest.raises(subject.SupervisorError, match="RUNTIME_BINDING_FAILED"):
        harness.controller.execute_primary(authority=harness.authority)

    assert blocked_causes == [BlockedFinalizationCause.RUNTIME_BINDING_FAILED]
