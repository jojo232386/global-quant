"""Pure credential-free lifecycle scheduler for frozen Gate 1B v1.6.

The scheduler is an immutable reducer.  It chooses no venue, owns no credential,
performs no I/O, reads no clock, and creates no deadline.  A credential-free
supervisor supplies a journal-reconstructed projection and an already-issued
absolute phase permit.  The reducer then proves that the next durable request
reservation is the one frozen action allowed at that point.

The terminal probe-order read is the first member of the seven-read final
schedule.  This is the only interpretation which simultaneously satisfies the
frozen seven-endpoint final evidence contract and the exact normal total of 21
HTTP attempts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from global_quant.gate1b.mutation_protocol import (
    CREATE_DEADLINE_SECONDS,
    MAX_ACCEPTED_TO_CANCEL_SECONDS,
    MAX_HARD_MUTATION_REQUESTS,
    MAX_HTTP_REQUESTS,
    MAX_POST_CREATE_READ_REQUESTS,
    MAX_READ_RETRIES,
    POST_CREATE_HTTP_RESERVE,
    REQUEST_TIMEOUT_SECONDS,
    TOTAL_RUNTIME_SECONDS,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIVE = Decimal(REQUEST_TIMEOUT_SECONDS)
_THREE = Decimal(MAX_ACCEPTED_TO_CANCEL_SECONDS)
_SIXTY = Decimal(CREATE_DEADLINE_SECONDS)
_ONE_EIGHTY = Decimal(TOTAL_RUNTIME_SECONDS)


class LifecycleError(RuntimeError):
    """Fail-closed lifecycle projection or transition error."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Capability(StrEnum):
    PRIMARY = "PRIMARY"
    RECOVERY_ONLY = "RECOVERY_ONLY"


class PassEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DISQUALIFIED = "DISQUALIFIED"


class ActionKind(StrEnum):
    READ = "READ"
    PERSIST_INTENT = "PERSIST_INTENT"
    BIND_INTENT = "BIND_INTENT"
    CREATE = "CREATE"
    CANCEL = "CANCEL"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"
    COMPLETE_CHILD = "COMPLETE_CHILD"


class Freshness(StrEnum):
    PRE_INTENT = "PRE_INTENT"
    POST_MUTATION = "POST_MUTATION"
    FINAL_FRESH = "FINAL_FRESH"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FinalEvidenceClaim(StrEnum):
    ORDER = "ORDER"
    OPEN_REGULAR_ORDERS = "OPEN_REGULAR_ORDERS"
    OPEN_ALGO_ORDERS = "OPEN_ALGO_ORDERS"
    TRADES = "TRADES"
    ACCOUNT = "ACCOUNT"
    POSITIONS = "POSITIONS"
    SYMBOL_CONFIG = "SYMBOL_CONFIG"
    POSITION_MODE = "POSITION_MODE"


class Step(StrEnum):
    PRE_SERVER_TIME = "PRE_SERVER_TIME"
    PRE_POSITION_MODE = "PRE_POSITION_MODE"
    PRE_SYMBOL_CONFIG = "PRE_SYMBOL_CONFIG"
    PRE_ACCOUNT = "PRE_ACCOUNT"
    PRE_OPEN_ORDERS = "PRE_OPEN_ORDERS"
    PRE_OPEN_ALGO_ORDERS = "PRE_OPEN_ALGO_ORDERS"
    PRE_EXCHANGE_INFO = "PRE_EXCHANGE_INFO"
    PRE_DUPLICATE_ORDER = "PRE_DUPLICATE_ORDER"
    PRE_USER_TRADES = "PRE_USER_TRADES"
    PRE_BOOK_TICKER = "PRE_BOOK_TICKER"
    PRE_MARK_PRICE = "PRE_MARK_PRICE"
    PERSIST_INTENT = "PERSIST_INTENT"
    BIND_INTENT = "BIND_INTENT"
    CREATE = "CREATE"
    PROBE_ORDER = "PROBE_ORDER"
    PRIMARY_CANCEL = "PRIMARY_CANCEL"
    RECONCILE_CREATE_ORDER = "RECONCILE_CREATE_ORDER"
    RECONCILE_CANCEL_ORDER = "RECONCILE_CANCEL_ORDER"
    CONTAINMENT_ORDER = "CONTAINMENT_ORDER"
    CONTAINMENT_TRADES = "CONTAINMENT_TRADES"
    CONTAINMENT_ACCOUNT = "CONTAINMENT_ACCOUNT"
    CONTAINMENT_EXCHANGE_INFO = "CONTAINMENT_EXCHANGE_INFO"
    CONTAINMENT_MARK_PRICE = "CONTAINMENT_MARK_PRICE"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"
    RECONCILE_CLOSE_ORDER = "RECONCILE_CLOSE_ORDER"
    FINAL_ORDER = "FINAL_ORDER"
    FINAL_OPEN_ORDERS = "FINAL_OPEN_ORDERS"
    FINAL_OPEN_ALGO_ORDERS = "FINAL_OPEN_ALGO_ORDERS"
    FINAL_TRADES = "FINAL_TRADES"
    FINAL_ACCOUNT = "FINAL_ACCOUNT"
    FINAL_SYMBOL_CONFIG = "FINAL_SYMBOL_CONFIG"
    FINAL_POSITION_MODE = "FINAL_POSITION_MODE"
    COMPLETE_CHILD = "COMPLETE_CHILD"


PRE_INTENT_READ_STEPS = (
    Step.PRE_SERVER_TIME,
    Step.PRE_POSITION_MODE,
    Step.PRE_SYMBOL_CONFIG,
    Step.PRE_ACCOUNT,
    Step.PRE_OPEN_ORDERS,
    Step.PRE_OPEN_ALGO_ORDERS,
    Step.PRE_EXCHANGE_INFO,
    Step.PRE_DUPLICATE_ORDER,
    Step.PRE_USER_TRADES,
    Step.PRE_BOOK_TICKER,
    Step.PRE_MARK_PRICE,
)

FINAL_READ_STEPS = (
    Step.FINAL_ORDER,
    Step.FINAL_OPEN_ORDERS,
    Step.FINAL_OPEN_ALGO_ORDERS,
    Step.FINAL_TRADES,
    Step.FINAL_ACCOUNT,
    Step.FINAL_SYMBOL_CONFIG,
    Step.FINAL_POSITION_MODE,
)

CONTAINMENT_READ_STEPS = (
    Step.CONTAINMENT_ORDER,
    Step.CONTAINMENT_TRADES,
    Step.CONTAINMENT_ACCOUNT,
    Step.CONTAINMENT_EXCHANGE_INFO,
    Step.CONTAINMENT_MARK_PRICE,
)


class _Phase(StrEnum):
    PRE_INTENT = "PRE_INTENT"
    PERSIST_INTENT = "PERSIST_INTENT"
    BIND_INTENT = "BIND_INTENT"
    CREATE = "CREATE"
    PROBE_ORDER = "PROBE_ORDER"
    CANCEL = "CANCEL"
    RECONCILE_CREATE = "RECONCILE_CREATE"
    RECONCILE_CANCEL = "RECONCILE_CANCEL"
    CONTAINMENT = "CONTAINMENT"
    CLOSE = "CLOSE"
    RECONCILE_CLOSE = "RECONCILE_CLOSE"
    FINAL = "FINAL"
    COMPLETE_CHILD = "COMPLETE_CHILD"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


class ReconciliationKeyKind(StrEnum):
    CREATE_CLIENT_ID = "CREATE_CLIENT_ID"
    ORDER_TERMINAL_STATE = "ORDER_TERMINAL_STATE"
    CLOSE_CLIENT_ID_STATE = "CLOSE_CLIENT_ID_STATE"


@dataclass(frozen=True, slots=True)
class ReconciliationKey:
    kind: ReconciliationKeyKind
    probe_client_id: str
    close_client_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not ReconciliationKeyKind
            or not _valid_client_id(self.probe_client_id)
            or (
                self.kind is ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE
                and not _valid_client_id(self.close_client_id)
            )
            or (
                self.kind is not ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE
                and self.close_client_id is not None
            )
        ):
            raise LifecycleError("RECONCILIATION_KEY_INVALID")


@dataclass(frozen=True, slots=True)
class LifecycleTiming:
    lifecycle_started_at: Decimal
    lifecycle_deadline: Decimal

    def __post_init__(self) -> None:
        if (
            not _finite_decimal(self.lifecycle_started_at)
            or not _finite_decimal(self.lifecycle_deadline)
            or self.lifecycle_started_at < 0
            or self.lifecycle_deadline <= self.lifecycle_started_at
            or self.lifecycle_deadline - self.lifecycle_started_at > _ONE_EIGHTY
        ):
            raise LifecycleError("LIFECYCLE_TIMING_INVALID")

    @property
    def create_deadline(self) -> Decimal:
        return min(self.lifecycle_deadline, self.lifecycle_started_at + _SIXTY)


@dataclass(frozen=True, slots=True)
class BudgetState:
    total_http_attempts: int = 0
    pre_intent_read_attempts: int = 0
    post_create_read_attempts: int = 0
    read_retries: int = 0
    create_requests: int = 0
    cancel_requests: int = 0
    close_requests: int = 0
    mutation_requests: int = 0
    submissions: int = 0

    def __post_init__(self) -> None:
        values = (
            self.total_http_attempts,
            self.pre_intent_read_attempts,
            self.post_create_read_attempts,
            self.read_retries,
            self.create_requests,
            self.cancel_requests,
            self.close_requests,
            self.mutation_requests,
            self.submissions,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise LifecycleError("BUDGET_COUNTER_INVALID")
        if self.total_http_attempts > MAX_HTTP_REQUESTS:
            raise LifecycleError("BUDGET_TOTAL_HTTP_EXCEEDED")
        if self.post_create_read_attempts > MAX_POST_CREATE_READ_REQUESTS:
            raise LifecycleError("BUDGET_POST_CREATE_READ_EXCEEDED")
        if self.read_retries > MAX_READ_RETRIES:
            raise LifecycleError("BUDGET_READ_RETRY_EXCEEDED")
        if self.create_requests > 1:
            raise LifecycleError("BUDGET_CREATE_EXCEEDED")
        if self.cancel_requests > 2:
            raise LifecycleError("BUDGET_CANCEL_EXCEEDED")
        if self.close_requests > 1:
            raise LifecycleError("BUDGET_CLOSE_EXCEEDED")
        if self.mutation_requests > MAX_HARD_MUTATION_REQUESTS:
            raise LifecycleError("BUDGET_MUTATION_EXCEEDED")
        if self.submissions > 2:
            raise LifecycleError("BUDGET_SUBMISSION_EXCEEDED")
        if self.mutation_requests != (
            self.create_requests + self.cancel_requests + self.close_requests
        ):
            raise LifecycleError("BUDGET_MUTATION_COUNTER_MISMATCH")
        if self.submissions != self.create_requests + self.close_requests:
            raise LifecycleError("BUDGET_SUBMISSION_COUNTER_MISMATCH")
        if self.total_http_attempts != (
            self.pre_intent_read_attempts + self.post_create_read_attempts + self.mutation_requests
        ):
            raise LifecycleError("BUDGET_HTTP_COUNTER_MISMATCH")
        if self.read_retries > (self.pre_intent_read_attempts + self.post_create_read_attempts):
            raise LifecycleError("BUDGET_READ_RETRY_COUNTER_MISMATCH")


@dataclass(frozen=True, slots=True)
class PrimaryJournalProjection:
    reconstruction_sha256: str
    generation: int
    timing: LifecycleTiming
    probe_client_id: str
    close_client_id: str

    def __post_init__(self) -> None:
        _validate_projection_common(
            self.reconstruction_sha256,
            self.generation,
            self.timing,
            self.probe_client_id,
            self.close_client_id,
        )

    @property
    def capability(self) -> Capability:
        return Capability.PRIMARY


class RecoveryTarget(StrEnum):
    CREATE_UNKNOWN = "CREATE_UNKNOWN"
    CANCEL_UNKNOWN = "CANCEL_UNKNOWN"
    CLOSE_UNKNOWN = "CLOSE_UNKNOWN"
    OWNED_ORDER_OPEN = "OWNED_ORDER_OPEN"
    OWNED_FILL = "OWNED_FILL"
    FINAL_EVIDENCE = "FINAL_EVIDENCE"


@dataclass(frozen=True, slots=True)
class RecoveryJournalProjection:
    reconstruction_sha256: str
    source_attempt_sha256: str
    generation: int
    timing: LifecycleTiming
    probe_client_id: str
    close_client_id: str
    budget: BudgetState
    target: RecoveryTarget
    fresh_open_proof_sha256: str | None = None
    accepted_at: Decimal | None = None

    def __post_init__(self) -> None:
        _validate_projection_common(
            self.reconstruction_sha256,
            self.generation,
            self.timing,
            self.probe_client_id,
            self.close_client_id,
        )
        if (
            not _valid_sha256(self.source_attempt_sha256)
            or type(self.budget) is not BudgetState
            or type(self.target) is not RecoveryTarget
            or (
                self.fresh_open_proof_sha256 is not None
                and not _valid_sha256(self.fresh_open_proof_sha256)
            )
            or (
                self.accepted_at is not None
                and (
                    not _finite_decimal(self.accepted_at)
                    or self.accepted_at < self.timing.lifecycle_started_at
                )
            )
        ):
            raise LifecycleError("RECOVERY_PROJECTION_INVALID")
        if self.target is RecoveryTarget.OWNED_ORDER_OPEN and self.fresh_open_proof_sha256 is None:
            raise LifecycleError("RECOVERY_OPEN_PROOF_REQUIRED")
        if self.target is RecoveryTarget.CREATE_UNKNOWN and self.budget.create_requests != 1:
            raise LifecycleError("RECOVERY_CREATE_ATTEMPT_REQUIRED")
        if self.target is RecoveryTarget.CANCEL_UNKNOWN and self.budget.cancel_requests < 1:
            raise LifecycleError("RECOVERY_CANCEL_ATTEMPT_REQUIRED")
        if self.target is RecoveryTarget.CLOSE_UNKNOWN and self.budget.close_requests != 1:
            raise LifecycleError("RECOVERY_CLOSE_ATTEMPT_REQUIRED")
        if self.target in {RecoveryTarget.OWNED_ORDER_OPEN, RecoveryTarget.OWNED_FILL} and (
            self.budget.create_requests != 1
        ):
            raise LifecycleError("RECOVERY_CREATE_LINEAGE_REQUIRED")

    @property
    def capability(self) -> Capability:
        return Capability.RECOVERY_ONLY


@dataclass(frozen=True, slots=True)
class PhasePermitProjection:
    generation: int
    sequence: int
    action_sha256: str
    lifecycle_deadline: Decimal
    issued_at: Decimal
    absolute_deadline: Decimal
    local_limit_seconds: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.sequence) is not int
            or self.sequence < 0
            or not _valid_sha256(self.action_sha256)
            or not _finite_decimal(self.lifecycle_deadline)
            or not _finite_decimal(self.issued_at)
            or not _finite_decimal(self.absolute_deadline)
            or not _finite_decimal(self.local_limit_seconds)
            or self.local_limit_seconds <= 0
        ):
            raise LifecycleError("PHASE_PERMIT_INVALID")


@dataclass(frozen=True, slots=True)
class PlannedAction:
    generation: int
    ordinal: int
    kind: ActionKind
    step: Step
    method: str | None
    path: str | None
    parameters: tuple[tuple[str, str], ...]
    retry_index: int
    retry_of_action_sha256: str | None
    reconciliation_key: ReconciliationKey | None
    freshness: Freshness
    final_evidence_claims: tuple[FinalEvidenceClaim, ...]
    requires_durable_reservation: bool
    requires_fresh_open_proof: bool
    precondition_action_sha256: str | None
    local_limit_seconds: Decimal | None
    absolute_deadline_cap: Decimal
    pass_deadline: Decimal | None
    action_sha256: str


class ReadDisposition(StrEnum):
    VALIDATED = "VALIDATED"
    FINAL_CLEAN = "FINAL_CLEAN"
    SAFE_FAILURE = "SAFE_FAILURE"
    UNSAFE_FAILURE = "UNSAFE_FAILURE"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_NEW = "ORDER_NEW"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_CANCELED_ZERO_FILL = "ORDER_CANCELED_ZERO_FILL"
    ORDER_CANCELED_WITH_FILL = "ORDER_CANCELED_WITH_FILL"
    ORDER_FILLED = "ORDER_FILLED"
    CLOSE_ORDER_TERMINAL = "CLOSE_ORDER_TERMINAL"
    ACCOUNT_OWNED_RESIDUAL = "ACCOUNT_OWNED_RESIDUAL"
    ACCOUNT_FLAT = "ACCOUNT_FLAT"


class MutationDisposition(StrEnum):
    CONFIRMED = "CONFIRMED"
    NOT_DISPATCHED = "NOT_DISPATCHED"
    UNKNOWN = "UNKNOWN"


class LocalDisposition(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ReadResolution:
    action_sha256: str
    result_proof_sha256: str
    disposition: ReadDisposition
    observed_at: Decimal

    def __post_init__(self) -> None:
        if (
            not _valid_sha256(self.action_sha256)
            or not _valid_sha256(self.result_proof_sha256)
            or type(self.disposition) is not ReadDisposition
            or not _finite_decimal(self.observed_at)
        ):
            raise LifecycleError("READ_RESOLUTION_INVALID")


@dataclass(frozen=True, slots=True)
class MutationResolution:
    action_sha256: str
    frontier_proof_sha256: str
    disposition: MutationDisposition
    observed_at: Decimal
    accepted_at: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_sha256(self.action_sha256)
            or not _valid_sha256(self.frontier_proof_sha256)
            or type(self.disposition) is not MutationDisposition
            or not _finite_decimal(self.observed_at)
            or (
                self.accepted_at is not None
                and (not _finite_decimal(self.accepted_at) or self.accepted_at > self.observed_at)
            )
        ):
            raise LifecycleError("MUTATION_RESOLUTION_INVALID")


@dataclass(frozen=True, slots=True)
class LocalResolution:
    action_sha256: str
    evidence_sha256: str
    disposition: LocalDisposition

    def __post_init__(self) -> None:
        if (
            not _valid_sha256(self.action_sha256)
            or not _valid_sha256(self.evidence_sha256)
            or type(self.disposition) is not LocalDisposition
        ):
            raise LifecycleError("LOCAL_RESOLUTION_INVALID")


@dataclass(frozen=True, slots=True)
class ReservedAction:
    action: PlannedAction
    permit: PhasePermitProjection


@dataclass(frozen=True, slots=True)
class LifecycleState:
    reconstruction_sha256: str
    generation: int
    capability: Capability
    timing: LifecycleTiming
    probe_client_id: str
    close_client_id: str
    phase: _Phase
    budget: BudgetState = BudgetState()
    pre_intent_index: int = 0
    containment_index: int = 0
    final_index: int = 0
    final_evidence_steps: tuple[Step, ...] = ()
    final_result_proof_sha256s: tuple[str, ...] = ()
    containment_source_result_sha256s: tuple[str, ...] = ()
    persisted_intent_proof_sha256: str | None = None
    intent_binding_proof_sha256: str | None = None
    pending: ReservedAction | None = None
    retry_step: Step | None = None
    retry_of_action_sha256: str | None = None
    fresh_open_proof_sha256: str | None = None
    accepted_at: Decimal | None = None
    fill_seen: bool = False
    close_reconciliation_required: bool = False
    last_permit_sequence: int = -1
    pass_eligibility: PassEligibility = PassEligibility.ELIGIBLE
    disqualifications: tuple[str, ...] = ()
    block_reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.phase is _Phase.COMPLETED

    @property
    def normal_pass_candidate(self) -> bool:
        return (
            self.completed
            and self.capability is Capability.PRIMARY
            and self.pass_eligibility is PassEligibility.ELIGIBLE
            and self.block_reason is None
            and self.budget.total_http_attempts == 21 + self.budget.read_retries
            and self.budget.read_retries <= 1
            and self.budget.pre_intent_read_attempts in {11, 12}
            and self.budget.post_create_read_attempts in {8, 9}
            and (
                self.budget.pre_intent_read_attempts
                - 11
                + self.budget.post_create_read_attempts
                - 8
                == self.budget.read_retries
            )
            and self.budget.create_requests == 1
            and self.budget.cancel_requests == 1
            and self.budget.close_requests == 0
            and self.budget.mutation_requests == 2
            and self.budget.submissions == 1
            and self.final_evidence_steps == FINAL_READ_STEPS
            and len(self.final_result_proof_sha256s) == len(FINAL_READ_STEPS)
            and len(set(self.final_result_proof_sha256s)) == len(FINAL_READ_STEPS)
        )


_PRE_READ_CONTRACT = {
    Step.PRE_SERVER_TIME: ("/fapi/v1/time", ()),
    Step.PRE_POSITION_MODE: ("/fapi/v1/positionSide/dual", (("recvWindow", "5000"),)),
    Step.PRE_SYMBOL_CONFIG: (
        "/fapi/v1/symbolConfig",
        (("recvWindow", "5000"), ("symbol", "ETHUSDT")),
    ),
    Step.PRE_ACCOUNT: ("/fapi/v2/account", (("recvWindow", "5000"),)),
    Step.PRE_OPEN_ORDERS: ("/fapi/v1/openOrders", (("recvWindow", "5000"),)),
    Step.PRE_OPEN_ALGO_ORDERS: (
        "/fapi/v1/openAlgoOrders",
        (("recvWindow", "5000"),),
    ),
    Step.PRE_EXCHANGE_INFO: ("/fapi/v1/exchangeInfo", ()),
    Step.PRE_USER_TRADES: (
        "/fapi/v1/userTrades",
        (("recvWindow", "5000"), ("symbol", "ETHUSDT")),
    ),
    Step.PRE_BOOK_TICKER: ("/fapi/v1/ticker/bookTicker", (("symbol", "ETHUSDT"),)),
    Step.PRE_MARK_PRICE: ("/fapi/v1/premiumIndex", (("symbol", "ETHUSDT"),)),
}

_FINAL_CONTRACT = {
    Step.FINAL_OPEN_ORDERS: (
        "/fapi/v1/openOrders",
        (("recvWindow", "5000"),),
        (FinalEvidenceClaim.OPEN_REGULAR_ORDERS,),
    ),
    Step.FINAL_OPEN_ALGO_ORDERS: (
        "/fapi/v1/openAlgoOrders",
        (("recvWindow", "5000"),),
        (FinalEvidenceClaim.OPEN_ALGO_ORDERS,),
    ),
    Step.FINAL_TRADES: (
        "/fapi/v1/userTrades",
        (("recvWindow", "5000"), ("symbol", "ETHUSDT")),
        (FinalEvidenceClaim.TRADES,),
    ),
    Step.FINAL_ACCOUNT: (
        "/fapi/v2/account",
        (("recvWindow", "5000"),),
        (FinalEvidenceClaim.ACCOUNT, FinalEvidenceClaim.POSITIONS),
    ),
    Step.FINAL_SYMBOL_CONFIG: (
        "/fapi/v1/symbolConfig",
        (("recvWindow", "5000"), ("symbol", "ETHUSDT")),
        (FinalEvidenceClaim.SYMBOL_CONFIG,),
    ),
    Step.FINAL_POSITION_MODE: (
        "/fapi/v1/positionSide/dual",
        (("recvWindow", "5000"),),
        (FinalEvidenceClaim.POSITION_MODE,),
    ),
}


def _finite_decimal(value: object) -> bool:
    return type(value) is Decimal and value.is_finite()


def _valid_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _valid_client_id(value: object) -> bool:
    return type(value) is str and _CLIENT_ID.fullmatch(value) is not None


def _validate_projection_common(
    reconstruction_sha256: object,
    generation: object,
    timing: object,
    probe_client_id: object,
    close_client_id: object,
) -> None:
    if (
        not _valid_sha256(reconstruction_sha256)
        or type(generation) is not int
        or generation <= 0
        or type(timing) is not LifecycleTiming
        or not _valid_client_id(probe_client_id)
        or not _valid_client_id(close_client_id)
        or probe_client_id == close_client_id
    ):
        raise LifecycleError("JOURNAL_PROJECTION_INVALID")


def start_primary(projection: PrimaryJournalProjection) -> LifecycleState:
    """Create a fresh primary state only from the typed journal projection."""

    if type(projection) is not PrimaryJournalProjection:
        raise LifecycleError("PRIMARY_PROJECTION_REQUIRED")
    return LifecycleState(
        reconstruction_sha256=projection.reconstruction_sha256,
        generation=projection.generation,
        capability=projection.capability,
        timing=projection.timing,
        probe_client_id=projection.probe_client_id,
        close_client_id=projection.close_client_id,
        phase=_Phase.PRE_INTENT,
    )


def resume_recovery(projection: RecoveryJournalProjection) -> LifecycleState:
    """Restore cleanup-only state from an immutable verified replay projection."""

    if type(projection) is not RecoveryJournalProjection:
        raise LifecycleError("RECOVERY_JOURNAL_PROJECTION_REQUIRED")
    phases = {
        RecoveryTarget.CREATE_UNKNOWN: _Phase.RECONCILE_CREATE,
        RecoveryTarget.CANCEL_UNKNOWN: _Phase.RECONCILE_CANCEL,
        RecoveryTarget.CLOSE_UNKNOWN: _Phase.RECONCILE_CLOSE,
        RecoveryTarget.OWNED_ORDER_OPEN: _Phase.CANCEL,
        RecoveryTarget.OWNED_FILL: _Phase.CONTAINMENT,
        RecoveryTarget.FINAL_EVIDENCE: _Phase.FINAL,
    }
    return LifecycleState(
        reconstruction_sha256=projection.reconstruction_sha256,
        generation=projection.generation,
        capability=projection.capability,
        timing=projection.timing,
        probe_client_id=projection.probe_client_id,
        close_client_id=projection.close_client_id,
        phase=phases[projection.target],
        budget=projection.budget,
        fresh_open_proof_sha256=projection.fresh_open_proof_sha256,
        accepted_at=projection.accepted_at,
        fill_seen=projection.target is RecoveryTarget.OWNED_FILL,
        close_reconciliation_required=projection.target is RecoveryTarget.CLOSE_UNKNOWN,
        pass_eligibility=PassEligibility.DISQUALIFIED,
        disqualifications=("RECOVERY_ONLY_SESSION",),
    )


def _reconciliation_key(
    state: LifecycleState,
    kind: ReconciliationKeyKind,
) -> ReconciliationKey:
    return ReconciliationKey(
        kind=kind,
        probe_client_id=state.probe_client_id,
        close_client_id=(
            state.close_client_id if kind is ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE else None
        ),
    )


def _canonical_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _make_action(
    state: LifecycleState,
    *,
    kind: ActionKind,
    step: Step,
    method: str | None = None,
    path: str | None = None,
    parameters: tuple[tuple[str, str], ...] = (),
    reconciliation_key: ReconciliationKey | None = None,
    freshness: Freshness = Freshness.NOT_APPLICABLE,
    final_evidence_claims: tuple[FinalEvidenceClaim, ...] = (),
    requires_fresh_open_proof: bool = False,
    precondition_action_sha256: str | None = None,
) -> PlannedAction:
    http = kind in {
        ActionKind.READ,
        ActionKind.CREATE,
        ActionKind.CANCEL,
        ActionKind.EMERGENCY_CLOSE,
    }
    retry_index = 1 if kind is ActionKind.READ and state.retry_step is step else 0
    retry_of = state.retry_of_action_sha256 if retry_index == 1 else None
    cap = (
        state.timing.create_deadline
        if kind is ActionKind.CREATE
        else state.timing.lifecycle_deadline
    )
    local_limit = _FIVE if http else None
    pass_deadline = (
        state.accepted_at + _THREE
        if (
            kind is ActionKind.CANCEL
            and state.budget.cancel_requests == 0
            and state.accepted_at is not None
        )
        else None
    )
    ordinal = state.budget.total_http_attempts + 1 if http else state.budget.total_http_attempts
    payload = {
        "absolute_deadline_cap": _canonical_decimal(cap),
        "final_evidence_claims": [claim.value for claim in final_evidence_claims],
        "freshness": freshness.value,
        "generation": state.generation,
        "kind": kind.value,
        "local_limit_seconds": _canonical_decimal(local_limit),
        "method": method,
        "ordinal": ordinal,
        "parameters": list(parameters),
        "pass_deadline": _canonical_decimal(pass_deadline),
        "path": path,
        "precondition_action_sha256": precondition_action_sha256,
        "reconciliation_key": (
            None
            if reconciliation_key is None
            else {
                "close_client_id": reconciliation_key.close_client_id,
                "kind": reconciliation_key.kind.value,
                "probe_client_id": reconciliation_key.probe_client_id,
            }
        ),
        "requires_fresh_open_proof": requires_fresh_open_proof,
        "retry_index": retry_index,
        "retry_of_action_sha256": retry_of,
        "step": step.value,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return PlannedAction(
        generation=state.generation,
        ordinal=ordinal,
        kind=kind,
        step=step,
        method=method,
        path=path,
        parameters=parameters,
        retry_index=retry_index,
        retry_of_action_sha256=retry_of,
        reconciliation_key=reconciliation_key,
        freshness=freshness,
        final_evidence_claims=final_evidence_claims,
        requires_durable_reservation=http,
        requires_fresh_open_proof=requires_fresh_open_proof,
        precondition_action_sha256=precondition_action_sha256,
        local_limit_seconds=local_limit,
        absolute_deadline_cap=cap,
        pass_deadline=pass_deadline,
        action_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _order_parameters(client_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("origClientOrderId", client_id),
        ("recvWindow", "5000"),
        ("symbol", "ETHUSDT"),
    )


def _read_action(
    state: LifecycleState,
    step: Step,
    path: str,
    parameters: tuple[tuple[str, str], ...],
    *,
    freshness: Freshness,
    key: ReconciliationKey | None = None,
    claims: tuple[FinalEvidenceClaim, ...] = (),
) -> PlannedAction:
    return _make_action(
        state,
        kind=ActionKind.READ,
        step=step,
        method="GET",
        path=path,
        parameters=parameters,
        reconciliation_key=key,
        freshness=freshness,
        final_evidence_claims=claims,
    )


def plan_next(state: LifecycleState) -> PlannedAction:
    """Return the one action allowed by the immutable state projection."""

    if type(state) is not LifecycleState:
        raise LifecycleError("LIFECYCLE_STATE_REQUIRED")
    if state.pending is not None:
        raise LifecycleError("ACTION_ALREADY_RESERVED")
    if state.phase is _Phase.COMPLETED:
        raise LifecycleError("LIFECYCLE_ALREADY_COMPLETED")
    if state.phase is _Phase.PRE_INTENT:
        step = PRE_INTENT_READ_STEPS[state.pre_intent_index]
        if step is Step.PRE_DUPLICATE_ORDER:
            path, parameters = "/fapi/v1/order", _order_parameters(state.probe_client_id)
        else:
            path, parameters = _PRE_READ_CONTRACT[step]
        return _read_action(
            state,
            step,
            path,
            parameters,
            freshness=Freshness.PRE_INTENT,
        )
    if state.phase is _Phase.PERSIST_INTENT:
        return _make_action(state, kind=ActionKind.PERSIST_INTENT, step=Step.PERSIST_INTENT)
    if state.phase is _Phase.BIND_INTENT:
        return _make_action(state, kind=ActionKind.BIND_INTENT, step=Step.BIND_INTENT)
    if state.phase is _Phase.CREATE:
        if state.capability is not Capability.PRIMARY:
            raise LifecycleError("RECOVERY_CREATE_FORBIDDEN")
        if state.intent_binding_proof_sha256 is None:
            raise LifecycleError("INTENT_BINDING_PROOF_REQUIRED")
        return _make_action(
            state,
            kind=ActionKind.CREATE,
            step=Step.CREATE,
            method="POST",
            path="/fapi/v1/order",
            parameters=(("requestProfile", "PERSISTED_EXACT_PROBE"),),
            reconciliation_key=_reconciliation_key(
                state,
                ReconciliationKeyKind.CREATE_CLIENT_ID,
            ),
            precondition_action_sha256=state.intent_binding_proof_sha256,
        )
    if state.phase is _Phase.PROBE_ORDER:
        return _read_action(
            state,
            Step.PROBE_ORDER,
            "/fapi/v1/order",
            _order_parameters(state.probe_client_id),
            freshness=Freshness.POST_MUTATION,
            key=_reconciliation_key(state, ReconciliationKeyKind.CREATE_CLIENT_ID),
        )
    if state.phase is _Phase.CANCEL:
        if state.fresh_open_proof_sha256 is None:
            raise LifecycleError("FRESH_OPEN_PROOF_REQUIRED")
        return _make_action(
            state,
            kind=ActionKind.CANCEL,
            step=Step.PRIMARY_CANCEL,
            method="DELETE",
            path="/fapi/v1/order",
            parameters=_order_parameters(state.probe_client_id),
            reconciliation_key=_reconciliation_key(
                state,
                ReconciliationKeyKind.ORDER_TERMINAL_STATE,
            ),
            requires_fresh_open_proof=True,
            precondition_action_sha256=state.fresh_open_proof_sha256,
        )
    if state.phase in {_Phase.RECONCILE_CREATE, _Phase.RECONCILE_CANCEL}:
        create = state.phase is _Phase.RECONCILE_CREATE
        return _read_action(
            state,
            Step.RECONCILE_CREATE_ORDER if create else Step.RECONCILE_CANCEL_ORDER,
            "/fapi/v1/order",
            _order_parameters(state.probe_client_id),
            freshness=Freshness.POST_MUTATION,
            key=_reconciliation_key(
                state,
                ReconciliationKeyKind.CREATE_CLIENT_ID
                if create
                else ReconciliationKeyKind.ORDER_TERMINAL_STATE,
            ),
        )
    if state.phase is _Phase.CONTAINMENT:
        step = CONTAINMENT_READ_STEPS[state.containment_index]
        if step is Step.CONTAINMENT_ORDER:
            path, parameters = "/fapi/v1/order", _order_parameters(state.probe_client_id)
        elif step is Step.CONTAINMENT_TRADES:
            path, parameters = (
                "/fapi/v1/userTrades",
                (("recvWindow", "5000"), ("symbol", "ETHUSDT")),
            )
        elif step is Step.CONTAINMENT_ACCOUNT:
            path, parameters = "/fapi/v2/account", (("recvWindow", "5000"),)
        elif step is Step.CONTAINMENT_EXCHANGE_INFO:
            path, parameters = "/fapi/v1/exchangeInfo", ()
        else:
            path, parameters = "/fapi/v1/premiumIndex", (("symbol", "ETHUSDT"),)
        return _read_action(
            state,
            step,
            path,
            parameters,
            freshness=Freshness.POST_MUTATION,
            key=_reconciliation_key(state, ReconciliationKeyKind.ORDER_TERMINAL_STATE),
        )
    if state.phase is _Phase.CLOSE:
        if state.budget.close_requests != 0:
            raise LifecycleError("CLOSE_REPOST_FORBIDDEN")
        if len(state.containment_source_result_sha256s) != len(CONTAINMENT_READ_STEPS) or len(
            set(state.containment_source_result_sha256s)
        ) != len(CONTAINMENT_READ_STEPS):
            raise LifecycleError("OWNED_FILL_PROOF_INCOMPLETE")
        proof_sha256 = hashlib.sha256(
            json.dumps(
                list(state.containment_source_result_sha256s),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        return _make_action(
            state,
            kind=ActionKind.EMERGENCY_CLOSE,
            step=Step.EMERGENCY_CLOSE,
            method="POST",
            path="/fapi/v1/order",
            parameters=(("requestProfile", "PROVEN_OWNED_REDUCE_ONLY_CLOSE"),),
            reconciliation_key=_reconciliation_key(
                state,
                ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE,
            ),
            precondition_action_sha256=proof_sha256,
        )
    if state.phase is _Phase.RECONCILE_CLOSE:
        return _read_action(
            state,
            Step.RECONCILE_CLOSE_ORDER,
            "/fapi/v1/order",
            _order_parameters(state.close_client_id),
            freshness=Freshness.POST_MUTATION,
            key=_reconciliation_key(state, ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE),
        )
    if state.phase is _Phase.FINAL:
        step = FINAL_READ_STEPS[state.final_index]
        key = None
        if state.close_reconciliation_required and step in {
            Step.FINAL_ORDER,
            Step.FINAL_TRADES,
            Step.FINAL_ACCOUNT,
        }:
            key = _reconciliation_key(state, ReconciliationKeyKind.CLOSE_CLIENT_ID_STATE)
        if step is Step.FINAL_ORDER:
            path, parameters, claims = (
                "/fapi/v1/order",
                _order_parameters(state.probe_client_id),
                (FinalEvidenceClaim.ORDER,),
            )
        else:
            path, parameters, claims = _FINAL_CONTRACT[step]
        return _read_action(
            state,
            step,
            path,
            parameters,
            freshness=Freshness.FINAL_FRESH,
            key=key,
            claims=claims,
        )
    if state.phase in {_Phase.COMPLETE_CHILD, _Phase.BLOCKED}:
        return _make_action(state, kind=ActionKind.COMPLETE_CHILD, step=Step.COMPLETE_CHILD)
    raise LifecycleError("LIFECYCLE_PHASE_INVALID")


def _disqualify(state: LifecycleState, reason: str) -> LifecycleState:
    reasons = state.disqualifications
    if reason not in reasons:
        reasons = (*reasons, reason)
    return replace(
        state,
        pass_eligibility=PassEligibility.DISQUALIFIED,
        disqualifications=reasons,
    )


def _block(state: LifecycleState, reason: str) -> LifecycleState:
    state = _disqualify(state, reason)
    return replace(
        state,
        phase=_Phase.BLOCKED,
        pending=None,
        retry_step=None,
        retry_of_action_sha256=None,
        block_reason=reason,
    )


def _validate_permit(
    state: LifecycleState,
    action: PlannedAction,
    permit: PhasePermitProjection,
) -> None:
    if permit.generation != state.generation:
        raise LifecycleError("PERMIT_GENERATION_MISMATCH")
    if permit.sequence != state.last_permit_sequence + 1:
        raise LifecycleError("PERMIT_SEQUENCE_REPLAY")
    if permit.action_sha256 != action.action_sha256:
        raise LifecycleError("PERMIT_ACTION_MISMATCH")
    if permit.lifecycle_deadline != state.timing.lifecycle_deadline:
        raise LifecycleError("PERMIT_LIFECYCLE_MISMATCH")
    if permit.local_limit_seconds > _FIVE:
        raise LifecycleError("PERMIT_LOCAL_LIMIT_EXCEEDED")
    if permit.issued_at < state.timing.lifecycle_started_at:
        raise LifecycleError("PERMIT_BEFORE_LIFECYCLE")
    if permit.issued_at >= state.timing.lifecycle_deadline:
        raise LifecycleError("LIFECYCLE_DEADLINE_EXCEEDED")
    if action.kind is ActionKind.CREATE and permit.issued_at >= state.timing.create_deadline:
        raise LifecycleError("CREATE_DEADLINE_EXCEEDED")
    if (
        permit.absolute_deadline <= permit.issued_at
        or permit.absolute_deadline > permit.issued_at + permit.local_limit_seconds
        or permit.absolute_deadline > action.absolute_deadline_cap
        or permit.absolute_deadline > state.timing.lifecycle_deadline
    ):
        raise LifecycleError("PERMIT_ABSOLUTE_DEADLINE_EXCEEDED")


def _reserve_budget(state: LifecycleState, action: PlannedAction) -> BudgetState:
    budget = state.budget
    values = {
        "total_http_attempts": budget.total_http_attempts + 1,
        "pre_intent_read_attempts": budget.pre_intent_read_attempts,
        "post_create_read_attempts": budget.post_create_read_attempts,
        "read_retries": budget.read_retries,
        "create_requests": budget.create_requests,
        "cancel_requests": budget.cancel_requests,
        "close_requests": budget.close_requests,
        "mutation_requests": budget.mutation_requests,
        "submissions": budget.submissions,
    }
    if action.kind is ActionKind.READ:
        key = (
            "post_create_read_attempts"
            if budget.create_requests == 1
            else "pre_intent_read_attempts"
        )
        values[key] += 1
        if action.retry_index == 1:
            values["read_retries"] += 1
    elif action.kind is ActionKind.CREATE:
        values["create_requests"] += 1
        values["mutation_requests"] += 1
        values["submissions"] += 1
    elif action.kind is ActionKind.CANCEL:
        values["cancel_requests"] += 1
        values["mutation_requests"] += 1
    elif action.kind is ActionKind.EMERGENCY_CLOSE:
        values["close_requests"] += 1
        values["mutation_requests"] += 1
        values["submissions"] += 1
    updated = BudgetState(**values)
    if action.kind is ActionKind.CREATE and (
        MAX_HTTP_REQUESTS - updated.total_http_attempts < POST_CREATE_HTTP_RESERVE
    ):
        raise LifecycleError("POST_CREATE_RESERVE_NOT_AVAILABLE")
    return updated


def reserve_http(
    state: LifecycleState,
    action: PlannedAction,
    permit: PhasePermitProjection,
) -> LifecycleState:
    """Consume counters for the exact action before the projector performs I/O."""

    expected = plan_next(state)
    if action != expected:
        raise LifecycleError("ACTION_PLAN_MISMATCH")
    if not action.requires_durable_reservation or action.local_limit_seconds is None:
        raise LifecycleError("HTTP_ACTION_REQUIRED")
    if type(permit) is not PhasePermitProjection:
        raise LifecycleError("PHASE_PERMIT_REQUIRED")
    _validate_permit(state, action, permit)
    budget = _reserve_budget(state, action)
    updated = replace(
        state,
        budget=budget,
        pending=ReservedAction(action=action, permit=permit),
        last_permit_sequence=permit.sequence,
    )
    if (
        action.kind is ActionKind.CANCEL
        and action.pass_deadline is not None
        and permit.issued_at > action.pass_deadline
    ):
        updated = _disqualify(updated, "ACCEPTED_TO_FIRST_CANCEL_DEADLINE_MISSED")
    return updated


def _result_timing(
    state: LifecycleState,
    action_sha256: str,
    observed_at: Decimal,
) -> tuple[LifecycleState, PlannedAction, bool]:
    if state.pending is None:
        raise LifecycleError("NO_RESERVED_ACTION")
    action = state.pending.action
    if action_sha256 != action.action_sha256:
        raise LifecycleError("RESULT_ACTION_MISMATCH")
    if observed_at < state.pending.permit.issued_at:
        raise LifecycleError("RESULT_BEFORE_DISPATCH")
    updated = replace(state, pending=None)
    phase_deadline_missed = observed_at > state.pending.permit.absolute_deadline
    if phase_deadline_missed:
        updated = _disqualify(updated, "RESULT_AFTER_PHASE_DEADLINE")
    if observed_at > state.timing.lifecycle_deadline:
        updated = _disqualify(updated, "RESULT_AFTER_LIFECYCLE_DEADLINE")
    return updated, action, phase_deadline_missed


def _read_failure(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
) -> LifecycleState | None:
    if disposition is ReadDisposition.UNSAFE_FAILURE:
        return _block(state, "UNSAFE_READ_FAILURE")
    if disposition is not ReadDisposition.SAFE_FAILURE:
        return None
    if action.retry_index != 0 or state.budget.read_retries >= MAX_READ_RETRIES:
        return _block(state, "READ_RETRY_BUDGET_EXHAUSTED")
    return replace(
        state,
        retry_step=action.step,
        retry_of_action_sha256=action.action_sha256,
    )


def _advance_pre_intent(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
) -> LifecycleState:
    expected = (
        ReadDisposition.ORDER_NOT_FOUND
        if action.step is Step.PRE_DUPLICATE_ORDER
        else ReadDisposition.VALIDATED
    )
    if disposition is not expected:
        return _block(state, "PRE_INTENT_STATE_MISMATCH")
    index = state.pre_intent_index + 1
    return replace(
        state,
        phase=_Phase.PERSIST_INTENT if index == len(PRE_INTENT_READ_STEPS) else state.phase,
        pre_intent_index=index,
    )


def _with_open_proof(
    state: LifecycleState,
    action: PlannedAction,
    result_proof_sha256: str,
    *,
    fill_seen: bool,
) -> LifecycleState:
    return replace(
        state,
        phase=_Phase.CANCEL,
        fresh_open_proof_sha256=result_proof_sha256,
        fill_seen=state.fill_seen or fill_seen,
        final_index=0,
        final_evidence_steps=(),
        final_result_proof_sha256s=(),
    )


def _start_containment(state: LifecycleState) -> LifecycleState:
    if state.budget.close_requests != 0:
        return _block(state, "OWNED_RESIDUAL_AFTER_CLOSE")
    state = _disqualify(state, "OWNED_FILL_DETECTED")
    return replace(
        state,
        phase=_Phase.CONTAINMENT,
        containment_index=0,
        containment_source_result_sha256s=(),
        fill_seen=True,
        final_index=0,
        final_evidence_steps=(),
        final_result_proof_sha256s=(),
    )


def _advance_order_read(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
    result_proof_sha256: str,
) -> LifecycleState:
    if disposition is ReadDisposition.ORDER_NEW:
        return _with_open_proof(state, action, result_proof_sha256, fill_seen=False)
    if disposition is ReadDisposition.ORDER_PARTIALLY_FILLED:
        return _with_open_proof(
            _disqualify(state, "OWNED_FILL_DETECTED"),
            action,
            result_proof_sha256,
            fill_seen=True,
        )
    if disposition in {ReadDisposition.ORDER_FILLED, ReadDisposition.ORDER_CANCELED_WITH_FILL}:
        return _start_containment(state)
    if disposition in {
        ReadDisposition.ORDER_CANCELED_ZERO_FILL,
        ReadDisposition.ORDER_NOT_FOUND,
    }:
        state = _disqualify(state, "CREATE_DID_NOT_PRODUCE_NORMAL_OPEN_ORDER")
        return replace(
            state,
            phase=_Phase.FINAL,
            final_index=0,
            final_evidence_steps=(),
            final_result_proof_sha256s=(),
        )
    return _block(state, "ORDER_RECONCILIATION_STATE_INVALID")


def _advance_containment(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
    result_proof_sha256: str,
) -> LifecycleState:
    if action.step is Step.CONTAINMENT_ORDER:
        if disposition in {ReadDisposition.ORDER_NEW, ReadDisposition.ORDER_PARTIALLY_FILLED}:
            if state.budget.cancel_requests >= 2:
                return _block(state, "OPEN_REMAINDER_CANCEL_BUDGET_EXHAUSTED")
            return _with_open_proof(state, action, result_proof_sha256, fill_seen=True)
        if disposition not in {
            ReadDisposition.ORDER_FILLED,
            ReadDisposition.ORDER_CANCELED_WITH_FILL,
        }:
            return _block(state, "OWNED_FILL_ORDER_PROOF_INVALID")
    elif action.step is Step.CONTAINMENT_ACCOUNT:
        if disposition is ReadDisposition.ACCOUNT_FLAT:
            return replace(
                _disqualify(state, "OWNED_FILL_NO_RESIDUAL_POSITION"),
                phase=_Phase.FINAL,
                containment_index=0,
                final_index=0,
                final_evidence_steps=(),
                final_result_proof_sha256s=(),
            )
        if disposition is not ReadDisposition.ACCOUNT_OWNED_RESIDUAL:
            return _block(state, "OWNED_POSITION_PROOF_INVALID")
    elif disposition is not ReadDisposition.VALIDATED:
        return _block(state, "OWNED_FILL_PROOF_READ_INVALID")
    index = state.containment_index + 1
    sources = (*state.containment_source_result_sha256s, result_proof_sha256)
    return replace(
        state,
        phase=_Phase.CLOSE if index == len(CONTAINMENT_READ_STEPS) else state.phase,
        containment_index=index,
        containment_source_result_sha256s=sources,
    )


def _advance_final(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
    result_proof_sha256: str,
) -> LifecycleState:
    if action.step is Step.FINAL_ORDER:
        if disposition in {ReadDisposition.ORDER_NEW, ReadDisposition.ORDER_PARTIALLY_FILLED}:
            if state.budget.cancel_requests >= 2:
                return _block(state, "FINAL_ORDER_STILL_OPEN")
            return _with_open_proof(
                _disqualify(state, "FINAL_ORDER_STILL_OPEN"),
                action,
                result_proof_sha256,
                fill_seen=disposition is ReadDisposition.ORDER_PARTIALLY_FILLED,
            )
        if disposition in {ReadDisposition.ORDER_FILLED, ReadDisposition.ORDER_CANCELED_WITH_FILL}:
            if state.budget.close_requests == 0:
                return _start_containment(state)
        elif disposition is not ReadDisposition.ORDER_CANCELED_ZERO_FILL:
            return _block(state, "FINAL_ORDER_STATE_UNPROVEN")
    elif action.step is Step.FINAL_ACCOUNT:
        if disposition is ReadDisposition.ACCOUNT_OWNED_RESIDUAL:
            if state.budget.close_requests == 0:
                return _start_containment(state)
            return _block(state, "OWNED_RESIDUAL_AFTER_CLOSE")
        if disposition is not ReadDisposition.ACCOUNT_FLAT:
            return _block(state, "FINAL_ACCOUNT_STATE_UNPROVEN")
    elif disposition is not ReadDisposition.FINAL_CLEAN:
        return _block(state, "FINAL_READ_STATE_UNPROVEN")
    index = state.final_index + 1
    evidence = (*state.final_evidence_steps, action.step)
    proofs = (*state.final_result_proof_sha256s, result_proof_sha256)
    return replace(
        state,
        phase=_Phase.COMPLETE_CHILD if index == len(FINAL_READ_STEPS) else state.phase,
        final_index=index,
        final_evidence_steps=evidence,
        final_result_proof_sha256s=proofs,
    )


def _advance_read(
    state: LifecycleState,
    action: PlannedAction,
    disposition: ReadDisposition,
    result_proof_sha256: str,
) -> LifecycleState:
    state = replace(state, retry_step=None, retry_of_action_sha256=None)
    if action.step in PRE_INTENT_READ_STEPS:
        return _advance_pre_intent(state, action, disposition)
    if action.step is Step.PROBE_ORDER:
        return _advance_order_read(state, action, disposition, result_proof_sha256)
    if action.step is Step.RECONCILE_CREATE_ORDER:
        if disposition is ReadDisposition.ORDER_NOT_FOUND:
            return replace(
                _disqualify(state, "CREATE_UNKNOWN_CONFIRMED_NOT_FOUND"),
                phase=_Phase.FINAL,
                final_index=0,
                final_evidence_steps=(),
                final_result_proof_sha256s=(),
            )
        return _advance_order_read(state, action, disposition, result_proof_sha256)
    if action.step is Step.RECONCILE_CANCEL_ORDER:
        if disposition is ReadDisposition.ORDER_CANCELED_ZERO_FILL:
            return replace(
                state,
                phase=_Phase.FINAL,
                final_index=0,
                final_evidence_steps=(),
                final_result_proof_sha256s=(),
            )
        if disposition is ReadDisposition.ORDER_NOT_FOUND:
            return _block(state, "CANCEL_TERMINAL_STATE_MISSING")
        return _advance_order_read(state, action, disposition, result_proof_sha256)
    if action.step in CONTAINMENT_READ_STEPS:
        return _advance_containment(state, action, disposition, result_proof_sha256)
    if action.step is Step.RECONCILE_CLOSE_ORDER:
        if disposition is ReadDisposition.ORDER_NOT_FOUND:
            state = _disqualify(state, "CLOSE_UNKNOWN_NOT_FOUND")
        elif disposition is not ReadDisposition.CLOSE_ORDER_TERMINAL:
            return _block(state, "CLOSE_RECONCILIATION_UNPROVEN")
        return replace(
            state,
            phase=_Phase.FINAL,
            final_index=0,
            final_evidence_steps=(),
            final_result_proof_sha256s=(),
        )
    if action.step in FINAL_READ_STEPS:
        return _advance_final(state, action, disposition, result_proof_sha256)
    return _block(state, "UNEXPECTED_READ_STEP")


def _advance_mutation(
    state: LifecycleState,
    action: PlannedAction,
    result: MutationResolution,
) -> LifecycleState:
    state = replace(state, retry_step=None, retry_of_action_sha256=None)
    if action.kind is ActionKind.CREATE:
        if result.disposition is MutationDisposition.CONFIRMED:
            if result.accepted_at is None:
                return _block(state, "CREATE_ACCEPTED_TIME_REQUIRED")
            if result.accepted_at < state.timing.lifecycle_started_at:
                return _block(state, "CREATE_ACCEPTED_TIME_INVALID")
            return replace(state, phase=_Phase.PROBE_ORDER, accepted_at=result.accepted_at)
        if result.disposition is MutationDisposition.UNKNOWN:
            return replace(
                _disqualify(state, "CREATE_POST_DISPATCH_UNKNOWN"),
                phase=_Phase.RECONCILE_CREATE,
            )
        return replace(
            _disqualify(state, "CREATE_NOT_DISPATCHED"),
            phase=_Phase.FINAL,
            final_index=0,
            final_evidence_steps=(),
            final_result_proof_sha256s=(),
        )
    if action.kind is ActionKind.CANCEL:
        state = replace(state, fresh_open_proof_sha256=None)
        if result.disposition is MutationDisposition.CONFIRMED:
            return replace(
                state,
                phase=_Phase.FINAL,
                final_index=0,
                final_evidence_steps=(),
                final_result_proof_sha256s=(),
            )
        return replace(
            _disqualify(
                state,
                "CANCEL_POST_DISPATCH_UNKNOWN"
                if result.disposition is MutationDisposition.UNKNOWN
                else "CANCEL_NOT_DISPATCHED",
            ),
            phase=_Phase.RECONCILE_CANCEL,
            final_index=0,
            final_evidence_steps=(),
            final_result_proof_sha256s=(),
        )
    if action.kind is ActionKind.EMERGENCY_CLOSE:
        reason = (
            "EMERGENCY_CLOSE_POST_DISPATCH_UNKNOWN"
            if result.disposition is MutationDisposition.UNKNOWN
            else "EMERGENCY_CLOSE_USED"
            if result.disposition is MutationDisposition.CONFIRMED
            else "EMERGENCY_CLOSE_NOT_DISPATCHED"
        )
        return replace(
            _disqualify(state, reason),
            phase=_Phase.RECONCILE_CLOSE,
            close_reconciliation_required=True,
            final_index=0,
            final_evidence_steps=(),
            final_result_proof_sha256s=(),
        )
    return _block(state, "UNEXPECTED_MUTATION_STEP")


def resolve_http(
    state: LifecycleState,
    result: ReadResolution | MutationResolution,
) -> LifecycleState:
    """Resolve the exact reserved action without rolling any counter back."""

    if type(result) not in {ReadResolution, MutationResolution}:
        raise LifecycleError("HTTP_RESOLUTION_REQUIRED")
    pending = state.pending
    state, action, phase_deadline_missed = _result_timing(
        state,
        result.action_sha256,
        result.observed_at,
    )
    if action.kind is ActionKind.READ:
        if type(result) is not ReadResolution:
            raise LifecycleError("READ_RESOLUTION_REQUIRED")
        if phase_deadline_missed:
            return _block(state, "READ_RESULT_AFTER_PHASE_DEADLINE")
        failed = _read_failure(state, action, result.disposition)
        if failed is not None:
            return failed
        return _advance_read(
            state,
            action,
            result.disposition,
            result.result_proof_sha256,
        )
    if type(result) is not MutationResolution:
        raise LifecycleError("MUTATION_RESOLUTION_REQUIRED")
    if (
        action.kind is ActionKind.CREATE
        and result.disposition is MutationDisposition.CONFIRMED
        and result.accepted_at is not None
        and pending is not None
        and result.accepted_at < pending.permit.issued_at
    ):
        raise LifecycleError("CREATE_ACCEPTED_BEFORE_DISPATCH")
    return _advance_mutation(state, action, result)


def apply_local(
    state: LifecycleState,
    action: PlannedAction,
    result: LocalResolution,
) -> LifecycleState:
    """Apply one non-network persistence/bind/completion result."""

    expected = plan_next(state)
    if action != expected:
        raise LifecycleError("ACTION_PLAN_MISMATCH")
    if result.action_sha256 != action.action_sha256:
        raise LifecycleError("RESULT_ACTION_MISMATCH")
    if action.requires_durable_reservation:
        raise LifecycleError("LOCAL_ACTION_REQUIRED")
    if result.disposition is LocalDisposition.FAILED:
        return _block(state, f"{action.kind.value}_FAILED")
    if action.kind is ActionKind.PERSIST_INTENT:
        return replace(
            state,
            phase=_Phase.BIND_INTENT,
            persisted_intent_proof_sha256=result.evidence_sha256,
        )
    if action.kind is ActionKind.BIND_INTENT:
        if state.capability is not Capability.PRIMARY:
            return _block(state, "RECOVERY_INTENT_BIND_FORBIDDEN")
        if state.persisted_intent_proof_sha256 is None:
            return _block(state, "PERSISTED_INTENT_PROOF_REQUIRED")
        return replace(
            state,
            phase=_Phase.CREATE,
            intent_binding_proof_sha256=result.evidence_sha256,
        )
    if action.kind is ActionKind.COMPLETE_CHILD:
        return replace(state, phase=_Phase.COMPLETED)
    raise LifecycleError("UNEXPECTED_LOCAL_ACTION")


def expire_lifecycle(state: LifecycleState, observed_at: Decimal) -> LifecycleState:
    """Project caller-observed expiry; no clock or replacement deadline is created."""

    if type(state) is not LifecycleState or not _finite_decimal(observed_at):
        raise LifecycleError("LIFECYCLE_EXPIRY_PROJECTION_INVALID")
    if observed_at < state.timing.lifecycle_deadline:
        raise LifecycleError("LIFECYCLE_NOT_EXPIRED")
    if state.pending is not None:
        raise LifecycleError("PENDING_ACTION_REQUIRES_FRONTIER_RESOLUTION")
    return _block(state, "LIFECYCLE_DEADLINE_EXCEEDED")
