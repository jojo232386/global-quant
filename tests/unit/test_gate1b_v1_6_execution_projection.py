"""Counterexamples for the credential-free execution projector."""

from __future__ import annotations

import hashlib
import inspect
import os
import time
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.execution_journal import (
    DurableGenerationAdmission,
    ExecutionJournal,
    FrontierState,
    GenerationCapability,
    SessionAuthority,
)
from global_quant.gate1b.execution_lifecycle import (
    ActionKind,
    LifecycleTiming,
    MutationDisposition,
    PhasePermitProjection,
    PlannedAction,
    PrimaryJournalProjection,
    ReadDisposition,
    Step,
    plan_next,
    reserve_http,
    start_primary,
)
from global_quant.gate1b.execution_projection import (
    ExactReadCompletion,
    ExactReadProjection,
    ExecutionProjectionError,
    ExecutionProjector,
    FreshFinalReference,
    MutationProjection,
    RecoverySource,
)
from global_quant.gate1b.process_boundary import (
    ProcessIdentity,
    ProcessLifecycleJournal,
)
from global_quant.gate1b.runtime_binding import RuntimeSnapshot, SourceBinding

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _runtime_snapshot(tmp_path: Path) -> RuntimeSnapshot:
    source = SourceBinding(
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
        relative_path="src/global_quant/gate1b/execution_projection.py",
        git_blob="4" * 40,
        sha256=_sha("module"),
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
        protocol_sha256=source.sha256,
        protocol_source=source,
        required_project_modules=(module.relative_path,),
        sources=(source, module),
    )


def _journals(
    tmp_path: Path,
) -> tuple[ExecutionJournal, ProcessLifecycleJournal, float, float]:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    started = time.monotonic()
    deadline = started + 120.0
    execution = ExecutionJournal(root / "request-ledger.json")
    process = ProcessLifecycleJournal.start(
        root / "lifecycle.jsonl",
        lifecycle_started_at=started,
        lifecycle_deadline=deadline,
        execution_journal_path=execution.path,
    )
    return execution, process, started, deadline


def _admit_primary(
    execution: ExecutionJournal,
    process: ProcessLifecycleJournal,
) -> SessionAuthority:
    identity = ProcessIdentity(987_650_001, 1, 987_650_001, 987_650_001, "test:1")
    process.stage_identity(1, identity)
    admission_record = execution.admit_generation(
        DurableGenerationAdmission(1, identity.sha256),
        GenerationCapability.PRIMARY,
    )
    process.record_execution_admission(
        generation=1,
        identity=identity,
        execution_journal=execution,
        admission_record=admission_record,
    )
    authority = SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    execution.establish_session_authority(authority)
    return authority


def _projector(
    tmp_path: Path,
) -> tuple[ExecutionProjector, ExecutionJournal, ProcessLifecycleJournal, SessionAuthority]:
    execution, process, _started, _deadline = _journals(tmp_path)
    authority = _admit_primary(execution, process)
    projector = ExecutionProjector(
        runtime_snapshot=_runtime_snapshot(tmp_path),
        execution_journal=execution,
        process_journal=process,
    )
    return projector, execution, process, authority


def test_projector_factory_rejects_mapping_and_subclass(tmp_path: Path) -> None:
    execution, process, _started, _deadline = _journals(tmp_path)
    snapshot = _runtime_snapshot(tmp_path)

    with pytest.raises(ExecutionProjectionError, match="RUNTIME_SNAPSHOT_REQUIRED"):
        ExecutionProjector(  # type: ignore[arg-type]
            runtime_snapshot={"runtime_commit": RUNTIME_COMMIT},
            execution_journal=execution,
            process_journal=process,
        )

    class _Journal(ExecutionJournal):
        pass

    with pytest.raises(ExecutionProjectionError, match="EXECUTION_JOURNAL_REQUIRED"):
        ExecutionProjector(
            runtime_snapshot=snapshot,
            execution_journal=_Journal(execution.path),
            process_journal=process,
        )


def test_runtime_snapshot_property_preserves_exact_identity(tmp_path: Path) -> None:
    snapshot = _runtime_snapshot(tmp_path)
    execution, process, _started, _deadline = _journals(tmp_path)
    projector = ExecutionProjector(
        runtime_snapshot=snapshot,
        execution_journal=execution,
        process_journal=process,
    )

    assert projector.runtime_snapshot is snapshot


def test_public_projection_contract_has_no_caller_boolean_hash_or_mapping() -> None:
    forbidden = {"bool", "Mapping", "dict[str, object]", "str"}
    for name in (
        "project_primary",
        "project_recovery",
        "project_pre_intent_success",
        "project_read_failure",
        "persist_intent",
        "build_exact_read",
        "complete_exact_read",
        "build_mutation",
        "project_mutation_outcome",
        "fresh_child_final_reference",
    ):
        signature = inspect.signature(getattr(ExecutionProjector, name))
        for parameter in signature.parameters.values():
            if parameter.name == "self":
                continue
            assert str(parameter.annotation) not in forbidden


def test_primary_projection_comes_from_both_live_journals(tmp_path: Path) -> None:
    projector, execution, process, authority = _projector(tmp_path)

    projection = projector.project_primary(authority)

    assert type(projection) is PrimaryJournalProjection
    assert projection.generation == 1
    assert projection.probe_client_id == authority.client_id
    assert projection.timing == LifecycleTiming(
        lifecycle_started_at=Decimal(str(process.lifecycle_started_at)),
        lifecycle_deadline=Decimal(str(process.lifecycle_deadline)),
    )
    assert projection.reconstruction_sha256 != execution.records()[-1].digest

    other = SessionAuthority.build(
        authorization_id="g1b16-fedcba9876543210",
        runtime_commit="8" * 40,
        session_nonce="9" * 16,
        generation=1,
    )
    with pytest.raises(ExecutionProjectionError, match="PRIMARY_AUTHORITY_REPLAY_MISMATCH"):
        projector.project_primary(other)


def test_pre_intent_success_projects_the_already_reserved_action(tmp_path: Path) -> None:
    projector, execution, _process, authority = _projector(tmp_path)
    state = start_primary(projector.project_primary(authority))
    action = plan_next(state)
    issued_at = state.timing.lifecycle_started_at + Decimal("1")
    permit = PhasePermitProjection(
        generation=state.generation,
        sequence=0,
        action_sha256=action.action_sha256,
        lifecycle_deadline=state.timing.lifecycle_deadline,
        issued_at=issued_at,
        absolute_deadline=issued_at + Decimal("1"),
        local_limit_seconds=Decimal("5"),
    )
    state = reserve_http(state, action, permit)
    prepared = execution.reserve_pre_intent_read(
        authority_sha256=authority.authority_sha256,
        path=action.path or "",
        parameters=dict(action.parameters),
        elapsed_seconds=Decimal("1"),
        deadline_ns=int(permit.absolute_deadline * Decimal(1_000_000_000)),
        retry_index=action.retry_index,
    )
    result = TransportResult.build(
        request_sha256=prepared.reservation.reservation_sha256,
        logical_request_sha256=prepared.reservation.logical_request_sha256,
        kind=ResponseKind.SERVER_TIME,
        fields=(("serverTime", 1_800_000_000_000),),
    )
    execution.record_pre_intent_read_result(
        reservation_sha256=prepared.reservation.reservation_sha256,
        result_sha256=result.result_sha256,
        observed_at_ns=int((issued_at + Decimal("0.5")) * Decimal(1_000_000_000)),
    )

    resolution = projector.project_pre_intent_success(
        action=action,
        result=result,
        state=state,
    )

    assert resolution.action_sha256 == action.action_sha256
    assert resolution.disposition is ReadDisposition.VALIDATED


def test_projection_output_types_are_frozen_dataclasses() -> None:
    assert RecoverySource.__dataclass_fields__.keys() == {"source_attempt_id"}
    assert ExactReadProjection.__dataclass_fields__.keys() == {
        "reserved_request",
        "reservation_proof",
    }
    assert ExactReadCompletion.__dataclass_fields__.keys() == {
        "result_proof",
        "resolution",
        "result_record",
    }
    assert MutationProjection.__dataclass_fields__.keys() == {
        "reserved_request",
        "reservation_proof",
        "attempt",
    }
    assert FreshFinalReference.__dataclass_fields__.keys() == {
        "final_evidence_sha256",
        "preflight_bundle",
        "final_bundle",
    }


@pytest.mark.parametrize(
    ("step", "kind", "fields", "expected"),
    [
        (
            Step.PRE_DUPLICATE_ORDER,
            ResponseKind.ORDER_NOT_FOUND,
            (
                ("clientOrderId", "g1b16_probe_abc"),
                ("outcome", "CONFIRMED_NOT_FOUND"),
                ("venueCode", -2013),
            ),
            ReadDisposition.ORDER_NOT_FOUND,
        ),
        (
            Step.PROBE_ORDER,
            ResponseKind.ORDER_OBSERVATION,
            (
                ("clientOrderId", "g1b16_probe_abc"),
                ("executedQty", "0"),
                ("orderIdSha256", _sha("order")),
                ("origQty", "0.001"),
                ("positionSide", "BOTH"),
                ("price", "2000"),
                ("reduceOnly", False),
                ("side", "BUY"),
                ("status", "NEW"),
                ("symbol", "ETHUSDT"),
                ("timeInForce", "GTC"),
                ("type", "LIMIT"),
            ),
            ReadDisposition.ORDER_NEW,
        ),
        (
            Step.PRE_ACCOUNT,
            ResponseKind.ACCOUNT,
            (
                (
                    "balances",
                    [
                        {
                            "asset": "USDT",
                            "availableBalance": "1000",
                            "walletBalance": "1000",
                        }
                    ],
                ),
                ("canTrade", True),
                ("multiAssetsMargin", False),
                ("nonzeroPositions", []),
            ),
            ReadDisposition.VALIDATED,
        ),
        (
            Step.FINAL_ACCOUNT,
            ResponseKind.ACCOUNT,
            (
                (
                    "balances",
                    [
                        {
                            "asset": "USDT",
                            "availableBalance": "1000",
                            "walletBalance": "1000",
                        }
                    ],
                ),
                ("canTrade", True),
                ("multiAssetsMargin", False),
                ("nonzeroPositions", []),
            ),
            ReadDisposition.ACCOUNT_FLAT,
        ),
    ],
)
def test_typed_read_disposition_uses_exact_response_domain(
    tmp_path: Path,
    step: Step,
    kind: ResponseKind,
    fields: tuple[tuple[str, object], ...],
    expected: ReadDisposition,
) -> None:
    projector, _execution, _process, _authority = _projector(tmp_path)
    action = object.__new__(PlannedAction)
    object.__setattr__(action, "step", step)
    result = TransportResult.build(
        request_sha256=_sha("request"),
        logical_request_sha256=_sha("logical"),
        kind=kind,
        fields=fields,
    )

    assert projector._read_disposition(action, result) is expected


@pytest.mark.parametrize(
    "status",
    ["FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"],
)
def test_close_reconciliation_accepts_every_terminal_close_status(
    tmp_path: Path,
    status: str,
) -> None:
    projector, _execution, _process, _authority = _projector(tmp_path)
    action = object.__new__(PlannedAction)
    object.__setattr__(action, "step", Step.RECONCILE_CLOSE_ORDER)
    result = TransportResult.build(
        request_sha256=_sha("close-request"),
        logical_request_sha256=_sha("close-logical"),
        kind=ResponseKind.ORDER_OBSERVATION,
        fields=(
            ("clientOrderId", "g1b16_close_abc"),
            ("executedQty", "0.001"),
            ("orderIdSha256", _sha("close-order")),
            ("origQty", "0.001"),
            ("positionSide", "BOTH"),
            ("price", "0"),
            ("reduceOnly", True),
            ("side", "SELL"),
            ("status", status),
            ("symbol", "ETHUSDT"),
            ("timeInForce", "GTC"),
            ("type", "MARKET"),
        ),
    )

    assert projector._read_disposition(action, result) is ReadDisposition.CLOSE_ORDER_TERMINAL


def test_mutation_frontier_mapping_is_conservative() -> None:
    mapping = ExecutionProjector._mutation_disposition

    assert mapping(FrontierState.CONFIRMED) is MutationDisposition.CONFIRMED
    assert mapping(FrontierState.NOT_DISPATCHED) is MutationDisposition.NOT_DISPATCHED
    assert mapping(FrontierState.UNKNOWN) is MutationDisposition.UNKNOWN
    with pytest.raises(ExecutionProjectionError, match="MUTATION_FRONTIER_NOT_TERMINAL"):
        mapping(FrontierState.GO_DURABLE)


def test_mutation_action_kinds_are_only_the_frozen_three() -> None:
    assert ExecutionProjector._mutation_kind(ActionKind.CREATE).value == "CREATE"
    assert ExecutionProjector._mutation_kind(ActionKind.CANCEL).value == "CANCEL"
    assert ExecutionProjector._mutation_kind(ActionKind.EMERGENCY_CLOSE).value == "EMERGENCY_CLOSE"
    with pytest.raises(ExecutionProjectionError, match="MUTATION_ACTION_REQUIRED"):
        ExecutionProjector._mutation_kind(ActionKind.READ)
