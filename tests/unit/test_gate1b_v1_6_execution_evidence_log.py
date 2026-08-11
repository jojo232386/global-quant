from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b import execution_evidence_log
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.durable_intent import persist_intent
from global_quant.gate1b.execution_evidence_log import (
    EvidenceRecordKind,
    ExecutionEvidenceLog,
    ExecutionEvidenceLogError,
)
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    DurableGenerationAdmission,
    ExecutionJournal,
    FrontierState,
    GenerationCapability,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ProcessReapReceipt,
    SessionAuthority,
)
from global_quant.gate1b.execution_kernel import DispatchResult
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    DurableIntent,
    LimitOrderFilters,
    OrderDerivationProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
)

SanitizedMutationFailure = getattr(
    execution_evidence_log,
    "SanitizedMutationFailure",
    type(None),
)
SanitizedMutationResult = getattr(
    execution_evidence_log,
    "SanitizedMutationResult",
    type(None),
)

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
DEADLINE_NS = 9_000_000_000


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _intent(*, persisted: bool) -> DurableIntent:
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


@dataclass(frozen=True)
class _PreparedContext:
    root: Path
    journal: ExecutionJournal
    admission_sequence: int
    admission_digest: str
    reserved: ReservedRequest
    proof: MutationReservationProof
    attempt: MutationAttempt


def _prepared_context(tmp_path: Path) -> _PreparedContext:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    journal = ExecutionJournal(root / "execution-journal.jsonl")
    admission = journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("process-identity-1"),
        ),
        GenerationCapability.PRIMARY,
    )
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
            deadline_ns=DEADLINE_NS + sequence,
            retry_index=0,
        )
        journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=_sha(f"pre-intent-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    persisted = persist_intent(root / "intent.json", _intent(persisted=False))
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            create_requests=previous.create_requests + 1,
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
    journal.prepare_attempt(attempt)
    return _PreparedContext(
        root=root,
        journal=journal,
        admission_sequence=admission.sequence,
        admission_digest=admission.digest,
        reserved=reserved,
        proof=proof,
        attempt=attempt,
    )


def _result(context: _PreparedContext, *, request_sha256: str | None = None) -> DispatchResult:
    transport = TransportResult.build(
        request_sha256=request_sha256 or context.reserved.request_sha256,
        logical_request_sha256=context.reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(
            ("clientOrderId", context.attempt.client_id),
            ("status", "NEW"),
        ),
    )
    return DispatchResult.build(context.attempt, transport_result=transport)


def _reap(context: _PreparedContext) -> None:
    context.journal.reap_generation(
        ProcessReapReceipt(
            generation=1,
            process_identity_sha256=_sha("process-identity-1"),
            admission_record_sequence=context.admission_sequence,
            admission_record_digest=context.admission_digest,
            returncode=-9,
            signal=9,
            local_process_quiesced=True,
            venue_mutation_absent_proven=False,
        )
    )


def test_execution_evidence_log_module_exists() -> None:
    assert importlib.util.find_spec("global_quant.gate1b.execution_evidence_log") is not None


def test_execution_evidence_log_exports_typed_public_contract() -> None:
    assert callable(getattr(execution_evidence_log, "ExecutionEvidenceLog", None))
    assert callable(getattr(execution_evidence_log, "ExecutionEvidenceLogError", None))
    assert callable(getattr(execution_evidence_log, "EvidenceRecordKind", None))
    assert callable(getattr(execution_evidence_log, "ExecutionEvidenceRecord", None))
    assert callable(getattr(execution_evidence_log, "SanitizedMutationResult", None))
    assert callable(getattr(execution_evidence_log, "SanitizedMutationFailure", None))


def test_prepared_and_result_are_exact_journal_backed_and_owner_only(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    path = context.root / "requests.jsonl"
    log = ExecutionEvidenceLog(path, execution_journal_path=context.journal.path)

    prepared = log.append_prepared(context.attempt.attempt_id)
    assert prepared.kind is EvidenceRecordKind.PREPARED
    assert prepared.frontier is FrontierState.PREPARED
    assert prepared.attempt_id == context.attempt.attempt_id
    assert prepared.mutation_kind is MutationKind.CREATE
    assert prepared.client_id == context.attempt.client_id
    assert prepared.reconciliation_client_id == context.attempt.client_id
    assert prepared.request_sha256 == context.reserved.request_sha256
    assert prepared.logical_request_sha256 == context.reserved.logical_request_sha256
    assert prepared.request_sequence == 12
    assert prepared.deadline_ns == DEADLINE_NS
    assert prepared.generation == 1
    assert prepared.retry_index == 0
    assert prepared.reservation_sha256 == context.attempt.reservation_sha256
    assert prepared.reservation_proof_sha256 == context.proof.proof_sha256
    assert prepared.journal_go_sequence is None
    assert prepared.outcome is None

    context.journal.record_go(context.attempt.attempt_id)
    dispatch_result = _result(context)
    context.journal.record_confirmed(context.attempt.attempt_id, dispatch_result.digest)
    result = log.append_result(dispatch_result)

    assert result.kind is EvidenceRecordKind.RESULT
    assert result.frontier is FrontierState.CONFIRMED
    assert result.journal_go_sequence is not None
    assert isinstance(result.outcome, SanitizedMutationResult)
    assert result.outcome.status == "NEW"
    assert result.outcome.client_order_id == context.attempt.client_id
    assert result.outcome.dispatch_result_sha256 == dispatch_result.digest
    assert result.outcome.transport_result_sha256 == dispatch_result.evidence_sha256
    assert ExecutionEvidenceLog(
        path,
        execution_journal_path=context.journal.path,
    ).replay() == (prepared, result)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.head_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(context.root.stat().st_mode) == 0o700

    raw = path.read_text(encoding="ascii").lower()
    for forbidden in (
        "authorization_id",
        "parameters",
        "origin",
        "api_key",
        "apikey",
        "secret",
        "signature",
        "signed_url",
        "request_headers",
        "raw_body",
    ):
        assert forbidden not in raw


@pytest.mark.parametrize(
    ("send_go", "expected_frontier"),
    [(False, FrontierState.NOT_DISPATCHED), (True, FrontierState.UNKNOWN)],
)
def test_failure_is_derived_from_exact_reaped_journal_frontier(
    tmp_path,
    send_go,
    expected_frontier,
) -> None:
    context = _prepared_context(tmp_path)
    log = ExecutionEvidenceLog(
        context.root / "requests.jsonl",
        execution_journal_path=context.journal.path,
    )
    log.append_prepared(context.attempt.attempt_id)
    if send_go:
        context.journal.record_go(context.attempt.attempt_id)
    _reap(context)
    assert (
        context.journal.resolve_after_reap(
            context.attempt.attempt_id,
            BoundaryResult.TIMEOUT,
        )
        is expected_frontier
    )

    failure = log.append_failure(context.attempt.attempt_id)

    assert failure.kind is EvidenceRecordKind.FAILURE
    assert failure.frontier is expected_frontier
    assert isinstance(failure.outcome, SanitizedMutationFailure)
    assert failure.outcome.boundary_result is BoundaryResult.TIMEOUT
    assert (failure.journal_go_sequence is not None) is send_go
    assert log.replay()[-1] == failure


def test_result_rejects_self_consistent_wrong_request_even_if_journaled(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    log = ExecutionEvidenceLog(
        context.root / "requests.jsonl",
        execution_journal_path=context.journal.path,
    )
    log.append_prepared(context.attempt.attempt_id)
    context.journal.record_go(context.attempt.attempt_id)
    forged = _result(context, request_sha256=_sha("wrong-request"))
    context.journal.record_confirmed(context.attempt.attempt_id, forged.digest)

    with pytest.raises(
        ExecutionEvidenceLogError,
        match="EVIDENCE_RESULT_REQUEST_BINDING_MISMATCH",
    ):
        log.append_result(forged)


def test_result_rejects_digest_not_durably_confirmed_by_journal(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    log = ExecutionEvidenceLog(
        context.root / "requests.jsonl",
        execution_journal_path=context.journal.path,
    )
    log.append_prepared(context.attempt.attempt_id)
    context.journal.record_go(context.attempt.attempt_id)
    confirmed = _result(context)
    context.journal.record_confirmed(context.attempt.attempt_id, confirmed.digest)
    different_transport = TransportResult.build(
        request_sha256=context.reserved.request_sha256,
        logical_request_sha256=context.reserved.logical_request_sha256,
        kind=ResponseKind.MUTATION_ACK,
        fields=(
            ("clientOrderId", context.attempt.client_id),
            ("status", "FILLED"),
        ),
    )
    not_confirmed = DispatchResult.build(
        context.attempt,
        transport_result=different_transport,
    )

    with pytest.raises(
        ExecutionEvidenceLogError,
        match="EVIDENCE_RESULT_JOURNAL_MISMATCH",
    ):
        log.append_result(not_confirmed)


def test_torn_tail_tamper_and_head_mismatch_fail_closed(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    path = context.root / "requests.jsonl"
    log = ExecutionEvidenceLog(path, execution_journal_path=context.journal.path)
    log.append_prepared(context.attempt.attempt_id)
    intact_log = path.read_bytes()
    intact_head = log.head_path.read_bytes()

    path.write_bytes(intact_log + b'{"torn"')
    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_TORN_RECORD"):
        log.replay()

    path.write_bytes(intact_log.replace(b'"retry_index":0', b'"retry_index":1'))
    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_DIGEST"):
        log.replay()

    path.write_bytes(intact_log)
    head = json.loads(intact_head)
    head["record_digest"] = _sha("wrong-head")
    log.head_path.write_bytes(_canonical(head) + b"\n")
    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_HEAD_MISMATCH"):
        log.replay()


def test_coordinated_log_and_head_rollback_is_detected_against_journal(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    path = context.root / "requests.jsonl"
    log = ExecutionEvidenceLog(path, execution_journal_path=context.journal.path)
    log.append_prepared(context.attempt.attempt_id)
    prepared_log = path.read_bytes()
    prepared_head = log.head_path.read_bytes()
    context.journal.record_go(context.attempt.attempt_id)
    result = _result(context)
    context.journal.record_confirmed(context.attempt.attempt_id, result.digest)
    log.append_result(result)

    path.write_bytes(prepared_log)
    log.head_path.write_bytes(prepared_head)
    with pytest.raises(
        ExecutionEvidenceLogError,
        match="EVIDENCE_JOURNAL_PROJECTION_INCOMPLETE",
    ):
        log.replay()


@pytest.mark.parametrize("attack", ["key", "value"])
def test_signed_or_credential_material_is_rejected_even_with_valid_chain_and_head(
    tmp_path,
    attack,
) -> None:
    canary = "G1B_CREDENTIAL_CANARY_42"
    context = _prepared_context(tmp_path)
    path = context.root / "requests.jsonl"
    log = ExecutionEvidenceLog(
        path,
        execution_journal_path=context.journal.path,
        credential_canaries=(canary.encode("ascii"),),
    )
    log.append_prepared(context.attempt.attempt_id)
    wire = json.loads(path.read_text(encoding="ascii"))
    if attack == "key":
        wire["signature"] = "synthetic"
    else:
        wire["note"] = f"prefix-{canary}-suffix"
    body = {key: value for key, value in wire.items() if key != "digest"}
    wire["digest"] = hashlib.sha256(_canonical(body)).hexdigest()
    path.write_bytes(_canonical(wire) + b"\n")
    head = json.loads(log.head_path.read_text(encoding="ascii"))
    head["record_digest"] = wire["digest"]
    log.head_path.write_bytes(_canonical(head) + b"\n")

    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_SENSITIVE_MATERIAL"):
        log.replay()


def test_owner_only_modes_are_required_on_reopen(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    path = context.root / "requests.jsonl"
    log = ExecutionEvidenceLog(path, execution_journal_path=context.journal.path)
    log.append_prepared(context.attempt.attempt_id)
    os.chmod(path, 0o644)

    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_FILE_MODE"):
        ExecutionEvidenceLog(path, execution_journal_path=context.journal.path)


def test_path_is_fixed_and_colocated_with_execution_journal(tmp_path) -> None:
    context = _prepared_context(tmp_path)
    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_PATH"):
        ExecutionEvidenceLog(
            context.root / "renamed.jsonl",
            execution_journal_path=context.journal.path,
        )
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    os.chmod(other, 0o700)
    with pytest.raises(ExecutionEvidenceLogError, match="EVIDENCE_JOURNAL_COLOCATION"):
        ExecutionEvidenceLog(
            other / "requests.jsonl",
            execution_journal_path=context.journal.path,
        )
