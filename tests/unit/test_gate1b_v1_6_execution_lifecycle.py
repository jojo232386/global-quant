from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

import pytest

from global_quant.gate1b.execution_lifecycle import (
    FINAL_READ_STEPS,
    PRE_INTENT_READ_STEPS,
    ActionKind,
    BudgetState,
    Capability,
    FinalEvidenceClaim,
    Freshness,
    LifecycleError,
    LifecycleTiming,
    LocalDisposition,
    LocalResolution,
    MutationDisposition,
    MutationResolution,
    PassEligibility,
    PhasePermitProjection,
    PrimaryJournalProjection,
    ReadDisposition,
    ReadResolution,
    ReconciliationKeyKind,
    RecoveryJournalProjection,
    RecoveryTarget,
    Step,
    apply_local,
    expire_lifecycle,
    plan_next,
    reserve_http,
    resolve_http,
    resume_recovery,
    start_primary,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
PROBE_ID = "g1b16-0123456789-0123456789abcdef-01"
CLOSE_ID = "g1b16c-01234567-0123456789abcdef-1"


def _proof(kind: str, action_sha256: str) -> str:
    return hashlib.sha256(f"{kind}\0{action_sha256}".encode()).hexdigest()


def timing() -> LifecycleTiming:
    return LifecycleTiming(
        lifecycle_started_at=Decimal("1000"),
        lifecycle_deadline=Decimal("1180"),
    )


def primary_state():
    return start_primary(
        PrimaryJournalProjection(
            reconstruction_sha256=SHA_A,
            generation=1,
            timing=timing(),
            probe_client_id=PROBE_ID,
            close_client_id=CLOSE_ID,
        )
    )


def _permit(state, action, issued_at: Decimal | str) -> PhasePermitProjection:
    issued = Decimal(issued_at)
    absolute = min(
        state.timing.lifecycle_deadline,
        action.absolute_deadline_cap,
        issued + Decimal("5"),
    )
    return PhasePermitProjection(
        generation=state.generation,
        sequence=state.last_permit_sequence + 1,
        action_sha256=action.action_sha256,
        lifecycle_deadline=state.timing.lifecycle_deadline,
        issued_at=issued,
        absolute_deadline=absolute,
        local_limit_seconds=Decimal("5"),
    )


def _read(state, disposition: ReadDisposition, at: Decimal | str):
    action = plan_next(state)
    assert action.kind is ActionKind.READ
    state = reserve_http(state, action, _permit(state, action, at))
    return resolve_http(
        state,
        ReadResolution(
            action_sha256=action.action_sha256,
            result_proof_sha256=_proof("read", action.action_sha256),
            disposition=disposition,
            observed_at=Decimal(at),
        ),
    )


def _mutate(
    state,
    disposition: MutationDisposition,
    at: Decimal | str,
    *,
    accepted_at: Decimal | str | None = None,
):
    action = plan_next(state)
    assert action.kind in {
        ActionKind.CREATE,
        ActionKind.CANCEL,
        ActionKind.EMERGENCY_CLOSE,
    }
    state = reserve_http(state, action, _permit(state, action, at))
    return resolve_http(
        state,
        MutationResolution(
            action_sha256=action.action_sha256,
            frontier_proof_sha256=_proof("frontier", action.action_sha256),
            disposition=disposition,
            observed_at=Decimal(at),
            accepted_at=None if accepted_at is None else Decimal(accepted_at),
        ),
    )


def _local(state, disposition: LocalDisposition = LocalDisposition.SUCCEEDED):
    action = plan_next(state)
    assert action.kind in {
        ActionKind.PERSIST_INTENT,
        ActionKind.BIND_INTENT,
        ActionKind.COMPLETE_CHILD,
    }
    return apply_local(
        state,
        action,
        LocalResolution(
            action_sha256=action.action_sha256,
            evidence_sha256=_proof("local", action.action_sha256),
            disposition=disposition,
        ),
    )


def _complete_pre_intent(state, *, start_at: int = 1001):
    for offset, step in enumerate(PRE_INTENT_READ_STEPS):
        action = plan_next(state)
        assert action.step is step
        disposition = (
            ReadDisposition.ORDER_NOT_FOUND
            if step is Step.PRE_DUPLICATE_ORDER
            else ReadDisposition.VALIDATED
        )
        state = _read(state, disposition, Decimal(start_at) + offset)
    assert plan_next(state).kind is ActionKind.PERSIST_INTENT
    state = _local(state)
    assert plan_next(state).kind is ActionKind.BIND_INTENT
    return _local(state)


def _to_cancel(state):
    state = _complete_pre_intent(state)
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    return _read(state, ReadDisposition.ORDER_NEW, "1013")


def _normal_complete(state):
    state = _to_cancel(state)
    state = _mutate(state, MutationDisposition.CONFIRMED, "1014")
    for index, step in enumerate(FINAL_READ_STEPS):
        action = plan_next(state)
        assert action.step is step
        disposition = (
            ReadDisposition.ORDER_CANCELED_ZERO_FILL
            if step is Step.FINAL_ORDER
            else ReadDisposition.ACCOUNT_FLAT
            if step is Step.FINAL_ACCOUNT
            else ReadDisposition.FINAL_CLEAN
        )
        state = _read(state, disposition, Decimal("1015") + index)
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD
    return _local(state)


def test_frozen_normal_schedule_is_exactly_21_http_attempts() -> None:
    state = primary_state()
    http_steps: list[Step] = []
    final_claims: list[tuple[FinalEvidenceClaim, ...]] = []

    while plan_next(state).kind is ActionKind.READ and len(http_steps) < 11:
        action = plan_next(state)
        http_steps.append(action.step)
        disposition = (
            ReadDisposition.ORDER_NOT_FOUND
            if action.step is Step.PRE_DUPLICATE_ORDER
            else ReadDisposition.VALIDATED
        )
        state = _read(state, disposition, Decimal("1001") + len(http_steps) - 1)

    state = _local(state)
    state = _local(state)
    http_steps.append(plan_next(state).step)
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    http_steps.append(plan_next(state).step)
    state = _read(state, ReadDisposition.ORDER_NEW, "1013")
    http_steps.append(plan_next(state).step)
    state = _mutate(state, MutationDisposition.CONFIRMED, "1014")

    for index, expected in enumerate(FINAL_READ_STEPS):
        action = plan_next(state)
        assert action.step is expected
        assert action.freshness is Freshness.FINAL_FRESH
        http_steps.append(action.step)
        final_claims.append(action.final_evidence_claims)
        disposition = (
            ReadDisposition.ORDER_CANCELED_ZERO_FILL
            if expected is Step.FINAL_ORDER
            else ReadDisposition.ACCOUNT_FLAT
            if expected is Step.FINAL_ACCOUNT
            else ReadDisposition.FINAL_CLEAN
        )
        state = _read(state, disposition, Decimal("1015") + index)

    state = _local(state)
    assert state.completed
    assert state.normal_pass_candidate
    assert state.budget.total_http_attempts == 21
    assert state.budget.post_create_read_attempts == 8
    assert state.budget.mutation_requests == 2
    assert state.budget.submissions == 1
    assert tuple(http_steps[:11]) == PRE_INTENT_READ_STEPS
    assert tuple(http_steps[-7:]) == FINAL_READ_STEPS
    assert final_claims[4] == (
        FinalEvidenceClaim.ACCOUNT,
        FinalEvidenceClaim.POSITIONS,
    )


def test_terminal_read_is_the_first_of_seven_final_reads_not_an_eighth_read() -> None:
    state = _to_cancel(primary_state())
    state = _mutate(state, MutationDisposition.CONFIRMED, "1014")
    action = plan_next(state)

    assert action.step is Step.FINAL_ORDER
    assert action.final_evidence_claims == (FinalEvidenceClaim.ORDER,)
    assert len(FINAL_READ_STEPS) == 7


def test_create_is_hash_bound_to_both_intent_persistence_and_bind_barriers() -> None:
    state = primary_state()
    for offset, step in enumerate(PRE_INTENT_READ_STEPS):
        disposition = (
            ReadDisposition.ORDER_NOT_FOUND
            if step is Step.PRE_DUPLICATE_ORDER
            else ReadDisposition.VALIDATED
        )
        state = _read(state, disposition, Decimal("1001") + offset)

    persist = plan_next(state)
    persisted_proof = _proof("local", persist.action_sha256)
    state = _local(state)
    assert state.persisted_intent_proof_sha256 == persisted_proof

    bind = plan_next(state)
    binding_proof = _proof("local", bind.action_sha256)
    state = _local(state)
    create = plan_next(state)
    assert state.intent_binding_proof_sha256 == binding_proof
    assert create.precondition_action_sha256 == binding_proof


def test_final_result_cannot_reuse_a_pre_intent_action_digest() -> None:
    state = primary_state()
    pre_action = plan_next(state)
    state = _to_cancel(state)
    state = _mutate(state, MutationDisposition.CONFIRMED, "1014")
    final_action = plan_next(state)
    state = reserve_http(state, final_action, _permit(state, final_action, "1015"))

    with pytest.raises(LifecycleError, match="RESULT_ACTION_MISMATCH"):
        resolve_http(
            state,
            ReadResolution(
                action_sha256=pre_action.action_sha256,
                result_proof_sha256=_proof("read", pre_action.action_sha256),
                disposition=ReadDisposition.ORDER_CANCELED_ZERO_FILL,
                observed_at=Decimal("1015"),
            ),
        )


def test_parsed_but_nonclean_final_result_cannot_advance_final_evidence() -> None:
    state = _to_cancel(primary_state())
    state = _mutate(state, MutationDisposition.CONFIRMED, "1014")
    state = _read(state, ReadDisposition.ORDER_CANCELED_ZERO_FILL, "1015")
    assert plan_next(state).step is Step.FINAL_OPEN_ORDERS

    state = _read(state, ReadDisposition.VALIDATED, "1016")
    assert state.block_reason == "FINAL_READ_STATE_UNPROVEN"
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD


def test_permit_is_bound_to_lifecycle_and_never_exceeds_five_seconds() -> None:
    state = primary_state()
    action = plan_next(state)
    good = _permit(state, action, "1001")

    with pytest.raises(LifecycleError, match="PERMIT_LOCAL_LIMIT_EXCEEDED"):
        reserve_http(state, action, replace(good, local_limit_seconds=Decimal("5.001")))
    with pytest.raises(LifecycleError, match="PERMIT_LIFECYCLE_MISMATCH"):
        reserve_http(state, action, replace(good, lifecycle_deadline=Decimal("1181")))
    with pytest.raises(LifecycleError, match="PERMIT_ABSOLUTE_DEADLINE_EXCEEDED"):
        reserve_http(state, action, replace(good, absolute_deadline=Decimal("1006.001")))


def test_create_after_sixty_seconds_is_forbidden_but_cleanup_after_three_seconds_continues() -> (
    None
):
    state = _complete_pre_intent(primary_state())
    create = plan_next(state)
    late_create = _permit(state, create, "1060")
    with pytest.raises(LifecycleError, match="CREATE_DEADLINE_EXCEEDED"):
        reserve_http(state, create, late_create)

    state = _complete_pre_intent(primary_state())
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    state = _read(state, ReadDisposition.ORDER_NEW, "1013")
    cancel = plan_next(state)
    state = reserve_http(state, cancel, _permit(state, cancel, "1015.001"))
    assert state.pass_eligibility is PassEligibility.DISQUALIFIED
    assert state.pending is not None
    assert state.pending.action.kind is ActionKind.CANCEL


def test_lifecycle_expiry_blocks_without_minting_a_new_clock() -> None:
    state = expire_lifecycle(primary_state(), Decimal("1180"))
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD
    assert state.timing.lifecycle_deadline == Decimal("1180")


def test_one_safe_read_retry_is_exact_and_aggregate() -> None:
    state = primary_state()
    first = plan_next(state)
    state = _read(state, ReadDisposition.SAFE_FAILURE, "1001")
    retry = plan_next(state)
    assert retry.step is first.step
    assert retry.retry_index == 1
    state = _read(state, ReadDisposition.VALIDATED, "1002")

    second = plan_next(state)
    state = reserve_http(state, second, _permit(state, second, "1003"))
    state = resolve_http(
        state,
        ReadResolution(
            action_sha256=second.action_sha256,
            result_proof_sha256=_proof("read", second.action_sha256),
            disposition=ReadDisposition.SAFE_FAILURE,
            observed_at=Decimal("1003"),
        ),
    )
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD
    assert state.budget.read_retries == 1


@pytest.mark.parametrize("retry_step", [Step.PRE_SERVER_TIME, Step.PROBE_ORDER])
def test_one_proven_read_retry_remains_a_normal_candidate(retry_step: Step) -> None:
    state = primary_state()
    if retry_step is Step.PRE_SERVER_TIME:
        state = _read(state, ReadDisposition.SAFE_FAILURE, "1001")
        state = _read(state, ReadDisposition.VALIDATED, "1002")
        for offset, step in enumerate(PRE_INTENT_READ_STEPS[1:], start=2):
            disposition = (
                ReadDisposition.ORDER_NOT_FOUND
                if step is Step.PRE_DUPLICATE_ORDER
                else ReadDisposition.VALIDATED
            )
            state = _read(state, disposition, Decimal("1001") + offset)
        state = _local(state)
        state = _local(state)
        state = _mutate(
            state,
            MutationDisposition.CONFIRMED,
            "1013",
            accepted_at="1013",
        )
    else:
        state = _complete_pre_intent(state)
        state = _mutate(
            state,
            MutationDisposition.CONFIRMED,
            "1012",
            accepted_at="1012",
        )
        state = _read(state, ReadDisposition.SAFE_FAILURE, "1013")
    state = _read(state, ReadDisposition.ORDER_NEW, "1014")
    state = _mutate(state, MutationDisposition.CONFIRMED, "1015")
    for index, step in enumerate(FINAL_READ_STEPS):
        disposition = (
            ReadDisposition.ORDER_CANCELED_ZERO_FILL
            if step is Step.FINAL_ORDER
            else ReadDisposition.ACCOUNT_FLAT
            if step is Step.FINAL_ACCOUNT
            else ReadDisposition.FINAL_CLEAN
        )
        state = _read(state, disposition, Decimal("1016") + index)
    state = _local(state)

    assert state.normal_pass_candidate
    assert state.budget.total_http_attempts == 22
    assert state.budget.read_retries == 1


def test_create_unknown_reconciles_same_probe_id_and_never_reposts() -> None:
    state = _complete_pre_intent(primary_state())
    state = _mutate(state, MutationDisposition.UNKNOWN, "1012")
    reconcile = plan_next(state)

    assert reconcile.kind is ActionKind.READ
    assert reconcile.step is Step.RECONCILE_CREATE_ORDER
    assert reconcile.reconciliation_key is not None
    assert reconcile.reconciliation_key.kind is ReconciliationKeyKind.CREATE_CLIENT_ID
    assert reconcile.reconciliation_key.probe_client_id == PROBE_ID

    state = _read(state, ReadDisposition.ORDER_NEW, "1013")
    cancel = plan_next(state)
    assert cancel.kind is ActionKind.CANCEL
    assert cancel.retry_index == 0
    assert state.budget.create_requests == 1


def test_cancel_unknown_requires_fresh_open_proof_before_new_cleanup_cancel() -> None:
    state = _to_cancel(primary_state())
    state = _mutate(state, MutationDisposition.UNKNOWN, "1014")
    reconcile = plan_next(state)
    assert reconcile.step is Step.RECONCILE_CANCEL_ORDER
    assert reconcile.reconciliation_key is not None
    assert reconcile.reconciliation_key.kind is ReconciliationKeyKind.ORDER_TERMINAL_STATE

    state = _read(state, ReadDisposition.ORDER_NEW, "1015")
    second = plan_next(state)
    assert second.kind is ActionKind.CANCEL
    assert second.retry_index == 0
    assert second.requires_fresh_open_proof
    assert second.precondition_action_sha256 == _proof("read", reconcile.action_sha256)
    state = _mutate(state, MutationDisposition.CONFIRMED, "1016")
    assert state.budget.cancel_requests == 2


def test_fill_runs_five_owned_containment_reads_then_only_first_close() -> None:
    state = _complete_pre_intent(primary_state())
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    state = _read(state, ReadDisposition.ORDER_FILLED, "1013")

    expected = (
        Step.CONTAINMENT_ORDER,
        Step.CONTAINMENT_TRADES,
        Step.CONTAINMENT_ACCOUNT,
        Step.CONTAINMENT_EXCHANGE_INFO,
        Step.CONTAINMENT_MARK_PRICE,
    )
    dispositions = (
        ReadDisposition.ORDER_FILLED,
        ReadDisposition.VALIDATED,
        ReadDisposition.ACCOUNT_OWNED_RESIDUAL,
        ReadDisposition.VALIDATED,
        ReadDisposition.VALIDATED,
    )
    for offset, (step, disposition) in enumerate(zip(expected, dispositions, strict=True)):
        assert plan_next(state).step is step
        state = _read(state, disposition, Decimal("1014") + offset)

    close = plan_next(state)
    assert close.kind is ActionKind.EMERGENCY_CLOSE
    assert close.reconciliation_key is not None
    assert close.reconciliation_key.kind is ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE
    assert close.precondition_action_sha256 not in {None, state.reconstruction_sha256}
    assert len(state.containment_source_result_sha256s) == 5
    state = _mutate(state, MutationDisposition.UNKNOWN, "1019")
    assert state.budget.close_requests == 1
    assert plan_next(state).step is Step.RECONCILE_CLOSE_ORDER

    state = _read(state, ReadDisposition.CLOSE_ORDER_TERMINAL, "1020")
    close_key_steps = {Step.FINAL_ORDER, Step.FINAL_TRADES, Step.FINAL_ACCOUNT}
    for index, step in enumerate(FINAL_READ_STEPS):
        action = plan_next(state)
        assert action.step is step
        if step in close_key_steps:
            assert action.reconciliation_key is not None
            assert action.reconciliation_key.kind is ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE
        disposition = (
            ReadDisposition.ORDER_FILLED
            if step is Step.FINAL_ORDER
            else ReadDisposition.ACCOUNT_FLAT
            if step is Step.FINAL_ACCOUNT
            else ReadDisposition.FINAL_CLEAN
        )
        state = _read(state, disposition, Decimal("1021") + index)
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD


def test_close_is_never_reposted_when_final_account_still_has_residual() -> None:
    state = _complete_pre_intent(primary_state())
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    state = _read(state, ReadDisposition.ORDER_FILLED, "1013")
    for disposition in (
        ReadDisposition.ORDER_FILLED,
        ReadDisposition.VALIDATED,
        ReadDisposition.ACCOUNT_OWNED_RESIDUAL,
        ReadDisposition.VALIDATED,
        ReadDisposition.VALIDATED,
    ):
        state = _read(state, disposition, Decimal("1014") + state.containment_index)
    state = _mutate(state, MutationDisposition.CONFIRMED, "1019")
    state = _read(state, ReadDisposition.CLOSE_ORDER_TERMINAL, "1020")
    for index, step in enumerate(FINAL_READ_STEPS):
        disposition = (
            ReadDisposition.ORDER_FILLED
            if step is Step.FINAL_ORDER
            else ReadDisposition.ACCOUNT_OWNED_RESIDUAL
            if step is Step.FINAL_ACCOUNT
            else ReadDisposition.FINAL_CLEAN
        )
        state = _read(state, disposition, Decimal("1021") + index)
        if step is Step.FINAL_ACCOUNT:
            break

    assert state.budget.close_requests == 1
    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD


def test_recovery_is_typed_reconciliation_only_and_cannot_create() -> None:
    state = resume_recovery(
        RecoveryJournalProjection(
            reconstruction_sha256=SHA_A,
            source_attempt_sha256=SHA_B,
            generation=2,
            timing=timing(),
            probe_client_id=PROBE_ID,
            close_client_id=CLOSE_ID,
            budget=BudgetState(
                total_http_attempts=1,
                create_requests=1,
                mutation_requests=1,
                submissions=1,
            ),
            target=RecoveryTarget.CREATE_UNKNOWN,
        )
    )

    assert state.capability is Capability.RECOVERY_ONLY
    assert state.pass_eligibility is PassEligibility.DISQUALIFIED
    assert plan_next(state).step is Step.RECONCILE_CREATE_ORDER
    state = _read(state, ReadDisposition.ORDER_NOT_FOUND, "1001")
    assert plan_next(state).kind is ActionKind.READ
    assert plan_next(state).step is Step.FINAL_ORDER
    assert all(plan_next(state).kind is not ActionKind.CREATE for _ in range(2))


def test_recovery_owned_open_requires_journal_reconstructed_proof() -> None:
    with pytest.raises(LifecycleError, match="RECOVERY_OPEN_PROOF_REQUIRED"):
        resume_recovery(
            RecoveryJournalProjection(
                reconstruction_sha256=SHA_A,
                source_attempt_sha256=SHA_B,
                generation=2,
                timing=timing(),
                probe_client_id=PROBE_ID,
                close_client_id=CLOSE_ID,
                budget=BudgetState(
                    total_http_attempts=1,
                    create_requests=1,
                    mutation_requests=1,
                    submissions=1,
                ),
                target=RecoveryTarget.OWNED_ORDER_OPEN,
            )
        )


def test_budget_projection_and_reservation_fail_closed_at_frozen_caps() -> None:
    with pytest.raises(LifecycleError, match="BUDGET_TOTAL_HTTP_EXCEEDED"):
        BudgetState(total_http_attempts=32)
    with pytest.raises(LifecycleError, match="BUDGET_CANCEL_EXCEEDED"):
        BudgetState(cancel_requests=3, mutation_requests=3)
    with pytest.raises(LifecycleError, match="BUDGET_SUBMISSION_EXCEEDED"):
        BudgetState(submissions=3)

    hard_max = BudgetState(
        total_http_attempts=31,
        pre_intent_read_attempts=12,
        post_create_read_attempts=15,
        create_requests=1,
        cancel_requests=2,
        close_requests=1,
        mutation_requests=4,
        submissions=2,
    )
    assert hard_max.total_http_attempts == 31
    assert hard_max.mutation_requests == 4
    assert hard_max.submissions == 2


def test_mutations_never_expose_a_transport_retry_index() -> None:
    state = _complete_pre_intent(primary_state())
    create = plan_next(state)
    assert create.retry_index == 0
    state = _mutate(
        state,
        MutationDisposition.CONFIRMED,
        "1012",
        accepted_at="1012",
    )
    state = _read(state, ReadDisposition.ORDER_NEW, "1013")
    assert plan_next(state).kind is ActionKind.CANCEL
    assert plan_next(state).retry_index == 0


def test_late_read_result_cannot_become_an_ownership_proof() -> None:
    state = primary_state()
    action = plan_next(state)
    permit = _permit(state, action, "1001")
    state = reserve_http(state, action, permit)
    state = resolve_http(
        state,
        ReadResolution(
            action_sha256=action.action_sha256,
            result_proof_sha256=_proof("read", action.action_sha256),
            disposition=ReadDisposition.VALIDATED,
            observed_at=Decimal("1006.001"),
        ),
    )

    assert plan_next(state).kind is ActionKind.COMPLETE_CHILD
    assert state.block_reason == "READ_RESULT_AFTER_PHASE_DEADLINE"


def test_phase_permit_sequence_must_be_exact_not_merely_increasing() -> None:
    state = primary_state()
    action = plan_next(state)
    permit = replace(_permit(state, action, "1001"), sequence=2)
    with pytest.raises(LifecycleError, match="PERMIT_SEQUENCE_REPLAY"):
        reserve_http(state, action, permit)


def test_first_process_boundary_phase_permit_sequence_is_zero() -> None:
    state = primary_state()
    action = plan_next(state)
    permit = _permit(state, action, "1001")

    assert permit.sequence == 0
    state = reserve_http(state, action, permit)
    assert state.last_permit_sequence == 0


def test_no_forbidden_legacy_execution_imports() -> None:
    import global_quant.gate1b.execution_lifecycle as module

    source = module.__loader__.get_source(module.__name__)
    assert source is not None
    for forbidden in (
        "mutation_runner",
        "DemoLifecycleTransport",
        "threading",
        "Future",
        "asyncio",
    ):
        assert forbidden not in source
