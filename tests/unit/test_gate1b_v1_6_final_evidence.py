from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

import global_quant.gate1b.final_evidence as final_evidence_module
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.durable_intent import persist_intent
from global_quant.gate1b.execution_journal import (
    BoundaryResult,
    DurableGenerationAdmission,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    GenerationCapability,
    JournalRecord,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    ProcessReapReceipt,
    ReadKind,
    ReadOutcome,
    ReadPurpose,
    ReadReservationProof,
    ReadResultProof,
    SessionAuthority,
)
from global_quant.gate1b.final_evidence import (
    AccountFinalEvidence,
    BlockedFinalizationCause,
    EvidenceKind,
    FinalEvidenceBundle,
    FinalEvidenceError,
    FinalEvidenceFinalizer,
    FinalOpenAlgoOrdersEvidence,
    FinalOpenOrdersEvidence,
    FinalOrderEvidence,
    FinalPositionEvidence,
    FinalPositionModeEvidence,
    FinalReadProvenance,
    FinalSymbolConfigEvidence,
    FinalTradeEvidence,
    MutationBarrier,
    OrderFinalStatus,
    PositionEntry,
    PreflightEvidenceBundle,
    PreflightKind,
    PreIntentReadProvenance,
    ReplayedPreflightEvidence,
    TradeEntry,
    final_evidence_bundle_sha256,
    load_preflight_evidence,
    persist_preflight_projection,
    project_final_evidence,
    scan_evidence_tree,
    validate_final_evidence,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    DurableIntent,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    build_client_order_id,
)
from global_quant.gate1b.process_boundary import (
    ProcessBoundaryError,
    ProcessIdentity,
    ProcessLifecycleJournal,
    ReapAttestation,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


_KINDS = (
    EvidenceKind.ORDER,
    EvidenceKind.OPEN_REGULAR_ORDERS,
    EvidenceKind.OPEN_ALGO_ORDERS,
    EvidenceKind.TRADE,
    EvidenceKind.ACCOUNT,
    EvidenceKind.SYMBOL_CONFIG,
    EvidenceKind.POSITION_MODE,
)

_AUTHORIZATION_ID = "g1b16-0123456789abcdef"
_OTHER_AUTHORIZATION_ID = "g1b16-fedcba9876543210"
_RUNTIME_COMMIT = "1" * 40
_SESSION_NONCE = "2" * 16
_CLIENT_ID = build_client_order_id(_RUNTIME_COMMIT, _SESSION_NONCE)
_DEADLINE_NS = 100_000_000_000
_ORDER_ID = _CLIENT_ID
_OTHER_ORDER_ID = "g1b16-0123456789-0123456789abcdef-01"
_PROTOCOL_COMMIT = "4" * 40
_PROTOCOL_TAG_OBJECT = "5" * 40
_PROTOCOL_SHA256 = "7" * 64


@dataclass(frozen=True)
class _Scenario:
    root: Path
    journal: ExecutionJournal
    bundle: FinalEvidenceBundle
    reap: ReapAttestation


@dataclass(frozen=True)
class _IncompleteScenario:
    root: Path
    journal: ExecutionJournal
    reap: ReapAttestation
    attempt: MutationAttempt


def test_final_artifact_allowlist_excludes_legacy_mutation_owners() -> None:
    allowlist = final_evidence_module.ARTIFACT_ALLOWLIST

    assert {"request-ledger.json", "request-ledger.json.head"} <= allowlist
    assert "execution-journal.jsonl" not in allowlist
    assert "execution-journal.jsonl.head" not in allowlist
    assert "mutation-lifecycle.json" not in allowlist


def test_public_final_projector_builds_bundle_from_exact_seven_provenances(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)

    projected = project_final_evidence(
        preflight=scenario.bundle.preflight,
        barrier=scenario.bundle.barrier,
        provenances=scenario.bundle.provenances,
    )

    assert projected == scenario.bundle


def test_public_final_bundle_digest_replays_and_rejects_tamper(tmp_path: Path) -> None:
    primary_root = tmp_path / "primary"
    foreign_root = tmp_path / "foreign"
    primary_root.mkdir()
    foreign_root.mkdir()
    scenario = _scenario(primary_root)
    digest = final_evidence_bundle_sha256(scenario.bundle, scenario.journal)

    assert len(digest) == 64
    assert digest == final_evidence_bundle_sha256(scenario.bundle, scenario.journal)

    foreign = _scenario(foreign_root)
    with pytest.raises(FinalEvidenceError, match="JOURNAL_RECORD_NOT_REPLAYED"):
        final_evidence_bundle_sha256(scenario.bundle, foreign.journal)

    object.__setattr__(scenario.bundle.order, "executed_quantity", Decimal("1"))
    with pytest.raises(FinalEvidenceError, match="FINAL_RESULT_PROJECTION_MISMATCH"):
        final_evidence_bundle_sha256(scenario.bundle, scenario.journal)


def test_typed_preflight_projector_builds_the_only_unpersisted_intent_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    identity_sha = ProcessIdentity(
        pid=43210,
        ppid=1,
        pgid=43210,
        sid=43210,
        start_token="test-start-token",
    ).sha256
    journal.admit_generation(
        DurableGenerationAdmission(generation=1, process_identity_sha256=identity_sha),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(root, journal)

    projection = final_evidence_module.project_preflight_evidence(
        authority=preflight.authority,
        authority_record=preflight.authority_record,
        provenances=preflight.provenances,
        execution_journal=journal,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    )
    exchange = preflight.by_kind(PreflightKind.EXCHANGE_INFO)

    assert projection.intent.persisted is False
    assert projection.intent.client_order_id == preflight.authority.client_id
    assert projection.intent.order_derivation == projection.order_derivation
    assert (
        projection.order_derivation.filter_snapshot_sha256
        == exchange.transport_result.result_sha256
    )
    assert projection.order_derivation.filter_contract_sha256 == projection.filters.canonical_sha256
    assert projection.order_derivation.book_age_ms == Decimal("101")
    assert projection.order_derivation.mark_age_ms == Decimal("100")
    assert projection.order_derivation.observed_elapsed_seconds == Decimal("0.011")
    assert projection.account_state.nonzero_positions == ()
    assert projection.account_state.open_regular_order_ids == ()
    assert projection.account_state.open_algo_order_ids == ()
    assert projection.symbol_state.symbol == SYMBOL
    assert projection.baseline_trades == ()
    assert projection.artifact_payload["schema_version"] == "gate1b.preflight-projection.v1"
    assert len(projection.artifact_payload["reads"]) == 11
    assert (
        projection.artifact_sha256
        == hashlib.sha256(
            json.dumps(
                projection.artifact_payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
    )


def _preflight_contract(
    kind: PreflightKind,
    authority: SessionAuthority,
    *,
    sequence: int,
) -> tuple[str, dict[str, str], ResponseKind, tuple[tuple[str, object], ...]]:
    wall = 1_786_370_000_000
    monotonic_before = 1_000_000_000 + sequence * 1_000_000
    timing = (
        ("localMonotonicAfterNs", monotonic_before + 100_000),
        ("localMonotonicBeforeNs", monotonic_before),
        ("localWallAfterMs", wall),
        ("localWallBeforeMs", wall),
    )
    recv = {"recvWindow": str(RECEIVE_WINDOW_MS)}
    return {
        PreflightKind.SERVER_TIME: (
            "/fapi/v1/time",
            {},
            ResponseKind.SERVER_TIME,
            (("serverTime", wall), *timing),
        ),
        PreflightKind.POSITION_MODE: (
            "/fapi/v1/positionSide/dual",
            recv,
            ResponseKind.POSITION_MODE,
            (("dualSidePosition", False),),
        ),
        PreflightKind.SYMBOL_CONFIG: (
            "/fapi/v1/symbolConfig",
            {"symbol": SYMBOL, **recv},
            ResponseKind.SYMBOL_CONFIG,
            (
                ("isAutoAddMargin", False),
                ("leverage", 1),
                ("marginType", "ISOLATED"),
                ("symbol", SYMBOL),
            ),
        ),
        PreflightKind.ACCOUNT: (
            "/fapi/v2/account",
            recv,
            ResponseKind.ACCOUNT,
            (
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
            ),
        ),
        PreflightKind.OPEN_REGULAR_ORDERS: (
            "/fapi/v1/openOrders",
            recv,
            ResponseKind.OPEN_ORDERS,
            (("count", 0), ("orders", [])),
        ),
        PreflightKind.OPEN_ALGO_ORDERS: (
            "/fapi/v1/openAlgoOrders",
            recv,
            ResponseKind.OPEN_ALGO_ORDERS,
            (("count", 0), ("orders", [])),
        ),
        PreflightKind.EXCHANGE_INFO: (
            "/fapi/v1/exchangeInfo",
            {},
            ResponseKind.EXCHANGE_INFO,
            (
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
                ("symbol", SYMBOL),
                ("timeInForce", ["GTC", "GTX"]),
                ("uninterpretedFilterTypes", []),
            ),
        ),
        PreflightKind.DUPLICATE_ORDER: (
            "/fapi/v1/order",
            {"origClientOrderId": authority.client_id, "symbol": SYMBOL, **recv},
            ResponseKind.ORDER_NOT_FOUND,
            (
                ("clientOrderId", authority.client_id),
                ("outcome", "CONFIRMED_NOT_FOUND"),
                ("venueCode", -2013),
            ),
        ),
        PreflightKind.TRADE: (
            "/fapi/v1/userTrades",
            {"symbol": SYMBOL, **recv},
            ResponseKind.USER_TRADES,
            (("count", 0), ("trades", [])),
        ),
        PreflightKind.BOOK_TICKER: (
            "/fapi/v1/ticker/bookTicker",
            {"symbol": SYMBOL},
            ResponseKind.BOOK_TICKER,
            (
                ("askPrice", "2000.01"),
                ("askQty", "1"),
                ("bidPrice", "2000"),
                ("bidQty", "1"),
                ("lastUpdateId", 1234),
                ("symbol", SYMBOL),
                ("time", wall - 100),
                *timing,
            ),
        ),
        PreflightKind.MARK_PRICE: (
            "/fapi/v1/premiumIndex",
            {"symbol": SYMBOL},
            ResponseKind.MARK_PRICE,
            (("markPrice", "2000"), ("symbol", SYMBOL), ("time", wall - 100), *timing),
        ),
    }[kind]


def _establish_preflight_chain(
    root: Path,
    journal: ExecutionJournal,
    *,
    field_overrides: dict[PreflightKind, dict[str, object]] | None = None,
    observed_at_overrides: dict[PreflightKind, int] | None = None,
    intent_transform: Callable[[DurableIntent], DurableIntent] | None = None,
) -> PreflightEvidenceBundle:
    authority = SessionAuthority.build(
        authorization_id=_AUTHORIZATION_ID,
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
        generation=1,
    )
    authority_record = journal.establish_session_authority(authority)
    provenances: list[PreIntentReadProvenance] = []
    for sequence, kind in enumerate(
        (
            PreflightKind.SERVER_TIME,
            PreflightKind.POSITION_MODE,
            PreflightKind.SYMBOL_CONFIG,
            PreflightKind.ACCOUNT,
            PreflightKind.OPEN_REGULAR_ORDERS,
            PreflightKind.OPEN_ALGO_ORDERS,
            PreflightKind.EXCHANGE_INFO,
            PreflightKind.DUPLICATE_ORDER,
            PreflightKind.TRADE,
            PreflightKind.BOOK_TICKER,
            PreflightKind.MARK_PRICE,
        ),
        start=1,
    ):
        path, parameters, response_kind, fields = _preflight_contract(
            kind,
            authority,
            sequence=sequence,
        )
        field_map = dict(fields)
        field_map.update((field_overrides or {}).get(kind, {}))
        prepared = journal.reserve_pre_intent_read(
            authority_sha256=authority.authority_sha256,
            path=path,
            parameters=parameters,
            elapsed_seconds=Decimal(sequence) / Decimal(1000),
            deadline_ns=_DEADLINE_NS,
            retry_index=0,
        )
        prepared_record = journal.records()[prepared.record_sequence - 1]
        transport_result = TransportResult.build(
            request_sha256=prepared.reservation.reservation_sha256,
            logical_request_sha256=prepared.reservation.logical_request_sha256,
            kind=response_kind,
            fields=tuple(field_map.items()),
        )
        result_record = journal.record_pre_intent_read_result(
            reservation_sha256=prepared.reservation.reservation_sha256,
            result_sha256=transport_result.result_sha256,
            observed_at_ns=(observed_at_overrides or {}).get(
                kind,
                1_000_000_000 + sequence * 1_000_000 + 200_000,
            ),
        )
        provenances.append(
            PreIntentReadProvenance(
                kind=kind,
                reservation=prepared.reservation,
                prepared_record=prepared_record,
                result_record=result_record,
                transport_result=transport_result,
            )
        )
    projection = final_evidence_module.project_preflight_evidence(
        authority=authority,
        authority_record=authority_record,
        provenances=tuple(provenances),
        execution_journal=journal,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    )
    persist_preflight_projection(root / "preflight.json", projection)
    persisted = persist_intent(
        root / "intent.json",
        projection.intent if intent_transform is None else intent_transform(projection.intent),
    )
    binding_record = journal.bind_persisted_intent(authority.authority_sha256, persisted)
    return PreflightEvidenceBundle(
        authority=authority,
        authority_record=authority_record,
        provenances=tuple(provenances),
        persisted_intent=persisted,
        intent_binding_record=binding_record,
    )


def test_preflight_projection_survives_new_projector_process_replay_and_tamper_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-restart-process"),
        ),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(root, journal)

    replayed = load_preflight_evidence(
        root / "preflight.json",
        execution_journal=ExecutionJournal(journal.path),
        persisted_intent=preflight.persisted_intent,
    )

    assert type(replayed) is ReplayedPreflightEvidence
    assert replayed.bundle == preflight
    assert (
        replayed.projection.intent.intent_sha256 == preflight.persisted_intent.intent.intent_sha256
    )
    assert replayed.artifact.path == root / "preflight.json"

    payload = json.loads((root / "preflight.json").read_text(encoding="ascii"))
    payload["account_state"]["available_balance"] = "999"
    (root / "preflight.json").write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    (root / "preflight.json").chmod(0o600)
    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_ARTIFACT_RECONSTRUCTION_MISMATCH"):
        load_preflight_evidence(
            root / "preflight.json",
            execution_journal=ExecutionJournal(journal.path),
            persisted_intent=preflight.persisted_intent,
        )


def test_preflight_replay_rejects_canonical_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-path-race-process"),
        ),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(root, journal)
    path = root / "preflight.json"
    replacement = root / "replacement.json"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    original_inode = path.stat().st_ino
    original_read = os.read
    swapped = False

    def replace_path_then_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped and os.fstat(descriptor).st_ino == original_inode:
            os.replace(replacement, path)
            swapped = True
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", replace_path_then_read)

    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_ARTIFACT_PATH_RACE"):
        load_preflight_evidence(
            path,
            execution_journal=ExecutionJournal(journal.path),
            persisted_intent=preflight.persisted_intent,
        )

    assert swapped is True


def test_preflight_child_monotonic_bracket_must_precede_its_durable_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-process"),
        ),
        GenerationCapability.PRIMARY,
    )

    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_TIMING_RECORD_MISMATCH"):
        _establish_preflight_chain(
            root,
            journal,
            field_overrides={
                PreflightKind.SERVER_TIME: {
                    "localMonotonicAfterNs": 2_000_000_000,
                }
            },
        )


def test_preflight_timed_endpoint_order_must_match_the_durable_schedule(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-timed-order"),
        ),
        GenerationCapability.PRIMARY,
    )

    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_TIMED_SCHEDULE_CONTRADICTION"):
        _establish_preflight_chain(
            root,
            journal,
            field_overrides={
                PreflightKind.BOOK_TICKER: {
                    "localMonotonicBeforeNs": 1_500_000_000,
                    "localMonotonicAfterNs": 1_500_100_000,
                }
            },
            observed_at_overrides={
                PreflightKind.BOOK_TICKER: 1_500_200_000,
                PreflightKind.MARK_PRICE: 1_600_200_000,
            },
        )


@pytest.mark.parametrize(
    "kind,override",
    (
        (
            PreflightKind.OPEN_REGULAR_ORDERS,
            {
                "count": 1,
                "orders": [
                    {
                        "clientOrderIdSha256": _sha("existing-client-order"),
                        "executedQty": "0",
                        "orderIdSha256": _sha("existing-order"),
                        "origQty": "0.001",
                        "positionSide": "BOTH",
                        "reduceOnly": False,
                        "side": "BUY",
                        "status": "NEW",
                        "symbol": SYMBOL,
                        "type": "LIMIT",
                    }
                ],
            },
        ),
        (
            PreflightKind.ACCOUNT,
            {
                "nonzeroPositions": [
                    {
                        "positionAmt": "0.001",
                        "positionSide": "BOTH",
                        "symbol": SYMBOL,
                    }
                ]
            },
        ),
    ),
)
def test_initial_open_orders_or_position_cannot_project_an_intent(
    tmp_path: Path,
    kind: PreflightKind,
    override: dict[str, object],
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-dirty-account"),
        ),
        GenerationCapability.PRIMARY,
    )

    with pytest.raises(
        FinalEvidenceError,
        match="PREFLIGHT_PROTOCOL_PROJECTION_FAILED:ACCOUNT_NOT_CLEAN",
    ):
        _establish_preflight_chain(
            root,
            journal,
            field_overrides={kind: override},
        )


def test_server_time_skew_is_derived_from_the_child_wall_midpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(
            generation=1,
            process_identity_sha256=_sha("preflight-server-skew"),
        ),
        GenerationCapability.PRIMARY,
    )

    with pytest.raises(
        FinalEvidenceError,
        match="PREFLIGHT_PROTOCOL_PROJECTION_FAILED:SERVER_TIME_SKEW_EXCEEDED",
    ):
        _establish_preflight_chain(
            root,
            journal,
            field_overrides={
                PreflightKind.SERVER_TIME: {
                    "serverTime": 1_786_370_005_001,
                }
            },
        )


def _mutation_frontier(
    journal: ExecutionJournal,
    preflight: PreflightEvidenceBundle,
    *,
    confirmed: bool,
) -> tuple[ReservedRequest, JournalRecord, MutationAttempt]:
    previous = journal.request_ledger_snapshot(preflight.authority.authority_sha256).last_ledger
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=12,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal(12),
        ),
        intent_sha256=preflight.persisted_intent.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(sorted(preflight.persisted_intent.intent.probe_payload.items())),
        elapsed_seconds=Decimal(12),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=preflight.authority.authority_sha256,
        generation=1,
        deadline_ns=_DEADLINE_NS,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=_DEADLINE_NS,
        client_id=_CLIENT_ID,
        authorization_id=_AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    journal.record_mutation_reservation(proof)
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=_DEADLINE_NS,
        reservation_sha256=reserved.request_sha256,
        authorization_id=_AUTHORIZATION_ID,
        intent_sha256=preflight.persisted_intent.intent.intent_sha256,
        runtime_commit=_RUNTIME_COMMIT,
        session_nonce=_SESSION_NONCE,
    )
    journal.prepare_attempt(attempt)
    frontier_record = journal.record_go(attempt.attempt_id)
    if confirmed:
        frontier_record = journal.record_confirmed(attempt.attempt_id, _sha("create-result"))
    return reserved, frontier_record, attempt


def _read_contract(
    kind: EvidenceKind,
    *,
    order_client_id: str = _CLIENT_ID,
) -> tuple[str, dict[str, str], ReadKind, ReadOutcome]:
    return {
        EvidenceKind.ORDER: (
            "/fapi/v1/order",
            {
                "origClientOrderId": order_client_id,
                "recvWindow": "5000",
                "symbol": "ETHUSDT",
            },
            ReadKind.ORDER,
            ReadOutcome.ORDER_TERMINAL,
        ),
        EvidenceKind.OPEN_REGULAR_ORDERS: (
            "/fapi/v1/openOrders",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
            ReadOutcome.NEGATIVE,
        ),
        EvidenceKind.OPEN_ALGO_ORDERS: (
            "/fapi/v1/openAlgoOrders",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
            ReadOutcome.NEGATIVE,
        ),
        EvidenceKind.TRADE: (
            "/fapi/v1/userTrades",
            {"recvWindow": "5000", "symbol": "ETHUSDT"},
            ReadKind.TRADE,
            ReadOutcome.NEGATIVE,
        ),
        EvidenceKind.ACCOUNT: (
            "/fapi/v2/account",
            {"recvWindow": "5000"},
            ReadKind.ACCOUNT,
            ReadOutcome.NEGATIVE,
        ),
        EvidenceKind.SYMBOL_CONFIG: (
            "/fapi/v1/symbolConfig",
            {"recvWindow": "5000", "symbol": "ETHUSDT"},
            ReadKind.GENERAL,
            ReadOutcome.SUCCESS,
        ),
        EvidenceKind.POSITION_MODE: (
            "/fapi/v1/positionSide/dual",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
            ReadOutcome.SUCCESS,
        ),
    }[kind]


def _record_final_read(
    journal: ExecutionJournal,
    preflight: PreflightEvidenceBundle,
    *,
    kind: EvidenceKind,
    fields: tuple[tuple[str, object], ...],
    outcome_override: ReadOutcome | None = None,
    order_client_id: str = _CLIENT_ID,
    authorization_id: str = _AUTHORIZATION_ID,
) -> FinalReadProvenance:
    path, parameters, read_kind, default_outcome = _read_contract(
        kind,
        order_client_id=order_client_id,
    )
    previous = journal.request_ledger_snapshot(preflight.authority.authority_sha256).last_ledger
    total_http_requests = previous.total_http_requests + 1
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=total_http_requests,
            post_create_read_requests=previous.post_create_read_requests + 1,
            last_elapsed_seconds=Decimal(total_http_requests),
        ),
        intent_sha256=preflight.persisted_intent.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path=path,
        purpose=RequestPurpose.READ,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=Decimal(total_http_requests),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=preflight.authority.authority_sha256,
        generation=1,
        deadline_ns=_DEADLINE_NS,
        reserved_request=reserved,
    )
    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=read_kind,
        purpose=ReadPurpose.EVIDENCE,
        generation=1,
        deadline_ns=_DEADLINE_NS,
        source_attempt_id=None,
        client_id=None,
        authorization_id=authorization_id,
    )
    prepared = journal.record_read_prepared(proof)
    response_kind = {
        EvidenceKind.ORDER: ResponseKind.ORDER_OBSERVATION,
        EvidenceKind.OPEN_REGULAR_ORDERS: ResponseKind.OPEN_ORDERS,
        EvidenceKind.OPEN_ALGO_ORDERS: ResponseKind.OPEN_ALGO_ORDERS,
        EvidenceKind.TRADE: ResponseKind.USER_TRADES,
        EvidenceKind.ACCOUNT: ResponseKind.ACCOUNT,
        EvidenceKind.SYMBOL_CONFIG: ResponseKind.SYMBOL_CONFIG,
        EvidenceKind.POSITION_MODE: ResponseKind.POSITION_MODE,
    }[kind]
    transport_result = TransportResult.build(
        request_sha256=reserved.request_sha256,
        logical_request_sha256=reserved.logical_request_sha256,
        kind=response_kind,
        fields=fields,
    )
    result_proof = ReadResultProof.build(
        request_sha256=reserved.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest=prepared.digest,
        generation=1,
        monotonic_sequence=total_http_requests,
        read_kind=read_kind,
        outcome=default_outcome if outcome_override is None else outcome_override,
        result_sha256=transport_result.result_sha256,
        observed_at_ns=total_http_requests * 1_000_000_000,
    )
    result = journal.record_read_result(result_proof)
    return FinalReadProvenance(
        kind=kind,
        reserved_request=reserved,
        prepared_record=prepared,
        result_record=result,
        transport_result=transport_result,
    )


def _final_transport_fields(
    *,
    status: OrderFinalStatus,
    executed_quantity: Decimal,
    client_order_id: str,
) -> dict[EvidenceKind, tuple[tuple[str, object], ...]]:
    venue_order_id = _sha("venue-order-id")
    return {
        EvidenceKind.ORDER: (
            ("clientOrderId", client_order_id),
            ("executedQty", format(executed_quantity, "f")),
            ("orderIdSha256", venue_order_id),
            ("origQty", "0.005"),
            ("positionSide", "BOTH"),
            ("price", "1980"),
            ("reduceOnly", False),
            ("side", "BUY"),
            ("status", status.value),
            ("symbol", SYMBOL),
            ("timeInForce", "GTX"),
            ("type", "LIMIT"),
        ),
        EvidenceKind.OPEN_REGULAR_ORDERS: (("count", 0), ("orders", [])),
        EvidenceKind.OPEN_ALGO_ORDERS: (("count", 0), ("orders", [])),
        EvidenceKind.TRADE: (("count", 0), ("trades", [])),
        EvidenceKind.ACCOUNT: (
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
        ),
        EvidenceKind.SYMBOL_CONFIG: (
            ("isAutoAddMargin", False),
            ("leverage", 1),
            ("marginType", "ISOLATED"),
            ("symbol", SYMBOL),
        ),
        EvidenceKind.POSITION_MODE: (("dualSidePosition", False),),
    }


def _exact_reap(
    root: Path,
    journal: ExecutionJournal,
    admission_record: JournalRecord,
    *,
    returncode: int = 0,
    signal: int | None = None,
) -> ReapAttestation:
    identity = ProcessIdentity(
        pid=43210,
        ppid=1,
        pgid=43210,
        sid=43210,
        start_token="test-start-token",
    )
    lifecycle_started_at = time.monotonic()
    process_journal = ProcessLifecycleJournal.start(
        root / "lifecycle.jsonl",
        lifecycle_started_at=lifecycle_started_at,
        lifecycle_deadline=lifecycle_started_at + 180.0,
        execution_journal_path=journal.path,
    )
    stage_receipt = process_journal.stage_identity(1, identity)
    process_journal.record_execution_admission(
        generation=1,
        identity=identity,
        execution_journal=journal,
        admission_record=admission_record,
    )
    receipt = ProcessReapReceipt(
        generation=1,
        process_identity_sha256=identity.sha256,
        admission_record_sequence=admission_record.sequence,
        admission_record_digest=admission_record.digest,
        returncode=returncode,
        signal=signal,
        local_process_quiesced=True,
        venue_mutation_absent_proven=False,
    )
    execution_record = journal.reap_generation(receipt)
    process_record = process_journal.record_reap(
        generation=1,
        identity=identity,
        returncode=returncode,
        signal_number=signal,
        execution_journal=journal,
        execution_reap_record=execution_record,
    )
    event = process_record.event
    controller = ReapAttestation(
        generation=1,
        stage_ordinal=stage_receipt.stage_ordinal,
        identity=identity,
        process_identity_sha256=identity.sha256,
        waited_pid=identity.pid,
        returncode=returncode,
        signal=signal,
        process_journal_path=process_journal.path,
        attested_monotonic_ns=event["attested_monotonic_ns"],
        journal_sequence=process_record.sequence,
        journal_digest=process_record.digest,
        journal_head_sequence=process_record.sequence,
        journal_head_digest=process_record.digest,
        execution_journal_sequence=event["execution_journal_sequence"],
        execution_journal_digest=event["execution_journal_digest"],
        execution_head_sequence=event["execution_head_sequence"],
        execution_head_digest=event["execution_head_digest"],
    )
    return controller


def _scenario(
    tmp_path: Path,
    *,
    status: OrderFinalStatus = OrderFinalStatus.CANCELED,
    executed_quantity: Decimal = Decimal("0"),
    unknown: bool = False,
    stale_order: bool = False,
    omitted_newer_read: bool = False,
    late_confirmation: bool = False,
    returncode: int = 0,
    signal: int | None = None,
    reported_order_id: str = _ORDER_ID,
    queried_order_id: str = _CLIENT_ID,
    wrong_open_orders_outcome: bool = False,
    wrong_symbol_authorization: bool = False,
    preflight_intent_transform: Callable[[DurableIntent], DurableIntent] | None = None,
    trade_order_id_sha256: str | None = None,
) -> _Scenario:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    identity_sha = ProcessIdentity(
        pid=43210,
        ppid=1,
        pgid=43210,
        sid=43210,
        start_token="test-start-token",
    ).sha256
    admission_record = journal.admit_generation(
        DurableGenerationAdmission(generation=1, process_identity_sha256=identity_sha),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(
        root,
        journal,
        intent_transform=preflight_intent_transform,
    )
    fields = _final_transport_fields(
        status=status,
        executed_quantity=executed_quantity,
        client_order_id=reported_order_id,
    )
    projected_trade: TradeEntry | None = None
    wallet_delta = Decimal("0")
    if trade_order_id_sha256 is not None:
        projected_trade = TradeEntry(
            trade_id_sha256=_sha("final-trade"),
            order_id_sha256=trade_order_id_sha256,
            quantity=Decimal("0.001"),
            fee=Decimal("0.0001"),
            realized_pnl=Decimal("0"),
        )
        fields[EvidenceKind.TRADE] = (
            ("count", 1),
            (
                "trades",
                [
                    {
                        "commission": "0.0001",
                        "orderIdSha256": trade_order_id_sha256,
                        "quantity": "0.001",
                        "realizedPnl": "0",
                        "tradeIdSha256": projected_trade.trade_id_sha256,
                    }
                ],
            ),
        )
        fields[EvidenceKind.ACCOUNT] = (
            (
                "balances",
                [
                    {
                        "asset": "USDT",
                        "availableBalance": "99.9999",
                        "walletBalance": "99.9999",
                    }
                ],
            ),
            ("canTrade", True),
            ("multiAssetsMargin", False),
            ("nonzeroPositions", []),
        )
        wallet_delta = Decimal("-0.0001")
    provenances: dict[EvidenceKind, FinalReadProvenance] = {}
    if unknown or stale_order or late_confirmation:
        raise AssertionError("use a directed exact-journal counterexample")
    reserved_mutation, mutation_record, _attempt = _mutation_frontier(
        journal,
        preflight,
        confirmed=True,
    )
    for kind in _KINDS:
        provenances[kind] = _record_final_read(
            journal,
            preflight,
            kind=kind,
            fields=fields[kind],
            outcome_override=(
                ReadOutcome.SUCCESS
                if wrong_open_orders_outcome and kind is EvidenceKind.OPEN_REGULAR_ORDERS
                else None
            ),
            order_client_id=queried_order_id,
            authorization_id=(
                _OTHER_AUTHORIZATION_ID
                if wrong_symbol_authorization and kind is EvidenceKind.SYMBOL_CONFIG
                else _AUTHORIZATION_ID
            ),
        )
    if omitted_newer_read:
        _record_final_read(
            journal,
            preflight,
            kind=EvidenceKind.ORDER,
            fields=fields[EvidenceKind.ORDER],
        )
    barrier = MutationBarrier(
        last_request=reserved_mutation,
        last_mutation_record=mutation_record,
    )
    bundle = FinalEvidenceBundle(
        preflight=preflight,
        barrier=barrier,
        order=FinalOrderEvidence(
            provenance=provenances[EvidenceKind.ORDER],
            client_order_id=reported_order_id,
            status=status,
            executed_quantity=executed_quantity,
        ),
        open_orders=FinalOpenOrdersEvidence(
            provenance=provenances[EvidenceKind.OPEN_REGULAR_ORDERS],
            open_order_id_sha256=(),
        ),
        open_algo_orders=FinalOpenAlgoOrdersEvidence(
            provenance=provenances[EvidenceKind.OPEN_ALGO_ORDERS],
            open_algo_order_id_sha256=(),
        ),
        position=FinalPositionEvidence(
            provenance=provenances[EvidenceKind.ACCOUNT],
            nonzero_positions=(),
        ),
        trade=FinalTradeEvidence(
            provenance=provenances[EvidenceKind.TRADE],
            relevant_trades=(
                (projected_trade,)
                if trade_order_id_sha256 == _sha("venue-order-id") and projected_trade is not None
                else ()
            ),
            fee_delta=(
                Decimal("0.0001")
                if trade_order_id_sha256 == _sha("venue-order-id")
                else Decimal("0")
            ),
        ),
        account=AccountFinalEvidence(
            provenance=provenances[EvidenceKind.ACCOUNT],
            can_trade=True,
            single_asset_mode=True,
            wallet_delta=wallet_delta,
        ),
        symbol_config=FinalSymbolConfigEvidence(
            provenance=provenances[EvidenceKind.SYMBOL_CONFIG],
            symbol_config_matches=True,
        ),
        position_mode=FinalPositionModeEvidence(
            provenance=provenances[EvidenceKind.POSITION_MODE],
            position_mode_one_way=True,
        ),
    )
    reap = _exact_reap(
        root,
        journal,
        admission_record,
        returncode=returncode,
        signal=signal,
    )
    return _Scenario(root=root, journal=journal, bundle=bundle, reap=reap)


def _incomplete_scenario(
    tmp_path: Path,
    *,
    confirmed: bool = True,
    returncode: int = -9,
    signal: int | None = 9,
) -> _IncompleteScenario:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    identity_sha = ProcessIdentity(
        pid=43210,
        ppid=1,
        pgid=43210,
        sid=43210,
        start_token="test-start-token",
    ).sha256
    admission_record = journal.admit_generation(
        DurableGenerationAdmission(generation=1, process_identity_sha256=identity_sha),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(root, journal)
    _reserved, _frontier, attempt = _mutation_frontier(
        journal,
        preflight,
        confirmed=confirmed,
    )
    reap = _exact_reap(
        root,
        journal,
        admission_record,
        returncode=returncode,
        signal=signal,
    )
    return _IncompleteScenario(root=root, journal=journal, reap=reap, attempt=attempt)


def _new_finalizer(
    scenario: _Scenario,
    *,
    canary_tokens: tuple[str, ...] = (),
) -> FinalEvidenceFinalizer:
    return FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
        canary_tokens=canary_tokens,
    )


def _write_owner_only(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _detached_digest(path: Path) -> tuple[str, str]:
    digest, filename = path.read_text(encoding="ascii").strip().split("  ", maxsplit=1)
    return digest, filename


def test_clean_typed_fresh_evidence_is_only_review_eligible(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is True
    assert verification.reason_codes == ()
    assert verification.gate_pass_declared is False


def test_final_validation_reprojects_all_preflight_results_before_pass(
    tmp_path: Path,
) -> None:
    scenario = _scenario(
        tmp_path,
        preflight_intent_transform=lambda intent: replace(
            intent,
            order_derivation=replace(
                intent.order_derivation,
                book_age_ms=Decimal("999"),
            ),
        ),
    )

    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_INTENT_PROJECTION_MISMATCH"):
        validate_final_evidence(scenario.bundle, scenario.journal)


def test_real_pre_mutation_read_cannot_be_final_evidence(tmp_path: Path) -> None:
    root = tmp_path / "pending-mutation"
    root.mkdir(mode=0o700)
    journal = ExecutionJournal(root / "request-ledger.json")
    journal.admit_generation(
        DurableGenerationAdmission(generation=1, process_identity_sha256=_sha("pending")),
        GenerationCapability.PRIMARY,
    )
    preflight = _establish_preflight_chain(root, journal)
    _mutation_frontier(journal, preflight, confirmed=False)
    fields = _final_transport_fields(
        status=OrderFinalStatus.CANCELED,
        executed_quantity=Decimal(0),
        client_order_id=_CLIENT_ID,
    )

    with pytest.raises(ExecutionJournalError, match="EXACT_REQUEST_ALREADY_PENDING"):
        _record_final_read(
            journal,
            preflight,
            kind=EvidenceKind.ORDER,
            fields=fields[EvidenceKind.ORDER],
        )


def test_omitted_newer_evidence_read_cannot_leave_stale_bundle_eligible(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path, omitted_newer_read=True)

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "FINAL_EVIDENCE_SET_NOT_LATEST" in verification.reason_codes


def test_mutation_frontier_change_after_final_reads_blocks_review(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    go_record = next(
        record
        for record in scenario.journal.records()
        if type(record.event).__name__ == "_GoDurable"
    )
    bundle = replace(
        scenario.bundle,
        barrier=replace(scenario.bundle.barrier, last_mutation_record=go_record),
    )

    verification = validate_final_evidence(bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "FINAL_READ_NOT_AFTER_MUTATION" in verification.reason_codes


def test_order_result_must_match_reserved_reconciliation_key(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, reported_order_id=_OTHER_ORDER_ID)

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "FINAL_ORDER_RECONCILIATION_KEY_MISMATCH" in verification.reason_codes


def test_final_order_key_must_match_last_mutation_attempt(tmp_path: Path) -> None:
    scenario = _scenario(
        tmp_path,
        reported_order_id=_OTHER_ORDER_ID,
        queried_order_id=_OTHER_ORDER_ID,
    )

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "FINAL_ORDER_RECONCILIATION_KEY_MISMATCH" in verification.reason_codes


def test_typed_read_outcome_must_match_sanitized_endpoint_payload(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, wrong_open_orders_outcome=True)

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "FINAL_READ_OUTCOME_MISMATCH" in verification.reason_codes


def test_final_reads_must_share_the_mutation_session_authorization(tmp_path: Path) -> None:
    with pytest.raises(ExecutionJournalError, match="EXACT_REQUEST_PROOF_MISMATCH"):
        _scenario(tmp_path, wrong_symbol_authorization=True)


def test_typed_but_unreplayed_read_record_is_rejected(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    provenance = scenario.bundle.order.provenance
    forged_result = replace(provenance.result_record, digest=_sha("off-disk-result-record"))
    forged_provenance = replace(provenance, result_record=forged_result)
    bundle = replace(
        scenario.bundle,
        order=replace(scenario.bundle.order, provenance=forged_provenance),
    )

    with pytest.raises(FinalEvidenceError, match="JOURNAL_RECORD_NOT_REPLAYED"):
        validate_final_evidence(bundle, scenario.journal)


def test_mapping_cannot_impersonate_reserved_request_or_journal_records() -> None:
    with pytest.raises(FinalEvidenceError, match="FINAL_READ_AUTHORITY_REQUIRED"):
        FinalReadProvenance(
            kind=EvidenceKind.ORDER,
            reserved_request={},  # type: ignore[arg-type]
            prepared_record={},  # type: ignore[arg-type]
            result_record={},  # type: ignore[arg-type]
            transport_result={},  # type: ignore[arg-type]
        )


def test_independent_final_result_hash_api_is_not_available() -> None:
    assert not hasattr(final_evidence_module, "sanitized_final_result_sha256")


def test_typed_evidence_is_bound_to_durable_sanitized_result(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    altered = replace(scenario.bundle.order, executed_quantity=Decimal("0.001"))

    with pytest.raises(FinalEvidenceError, match="FINAL_RESULT_PROJECTION_MISMATCH"):
        replace(scenario.bundle, order=altered)


def test_filled_order_can_never_be_review_eligible(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, status=OrderFinalStatus.FILLED)
    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert verification.review_eligible is False
    assert "PROBE_ORDER_FILLED" in verification.reason_codes
    assert verification.gate_pass_declared is False


def test_unknown_status_cannot_impersonate_a_child_transport_result(tmp_path: Path) -> None:
    with pytest.raises(FinalEvidenceError, match="FINAL_TRANSPORT_FIELDS_MISMATCH"):
        _scenario(tmp_path, status=OrderFinalStatus.UNKNOWN)


def test_go_then_reap_without_result_is_unknown_and_only_blocked(tmp_path: Path) -> None:
    scenario = _incomplete_scenario(tmp_path, confirmed=False)

    assert (
        scenario.journal.resolve_after_reap(
            scenario.attempt.attempt_id,
            BoundaryResult.EOF,
        )
        is FrontierState.UNKNOWN
    )
    assert scenario.journal.frontier(scenario.attempt.attempt_id) is FrontierState.UNKNOWN

    finalized = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    ).finalize_blocked(
        scenario.reap,
        cause=BlockedFinalizationCause.FINAL_READ_SCHEDULE_INCOMPLETE,
    )
    verdict = json.loads(finalized.verdict_path.read_text(encoding="utf-8"))
    assert verdict["status"] == "BLOCKED"
    assert verdict["gate_pass_declared"] is False


def test_position_trade_and_account_caller_forgery_fails_at_projection(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    bundle = scenario.bundle
    position = replace(
        bundle.position,
        nonzero_positions=(
            PositionEntry(
                symbol="ETHUSDT",
                position_side="BOTH",
                quantity=Decimal("0.001"),
            ),
        ),
    )
    trade = replace(
        bundle.trade,
        relevant_trades=(
            TradeEntry(
                trade_id_sha256=_sha("binance-demo-trade-id\\0"),
                order_id_sha256=_sha("venue-order-id"),
                quantity=Decimal("0.001"),
                fee=Decimal("0.0001"),
                realized_pnl=Decimal("0"),
            ),
        ),
    )
    account = replace(bundle.account, wallet_delta=Decimal("-0.0001"))

    for change in (
        {"position": position},
        {"trade": trade},
        {"account": account},
    ):
        with pytest.raises(FinalEvidenceError, match="FINAL_RESULT_PROJECTION_MISMATCH"):
            replace(bundle, **change)


@pytest.mark.parametrize(
    ("label", "trade_order_id_sha256", "expected_reason"),
    (
        ("owned", _sha("venue-order-id"), "RELEVANT_TRADES_PRESENT"),
        ("unrelated", _sha("unrelated-order"), "UNEXPECTED_TRADES_PRESENT"),
    ),
)
def test_trade_relevance_uses_exact_domain_separated_order_id_hash(
    tmp_path: Path,
    label: str,
    trade_order_id_sha256: str,
    expected_reason: str,
) -> None:
    case_root = tmp_path / label
    case_root.mkdir()
    scenario = _scenario(
        case_root,
        trade_order_id_sha256=trade_order_id_sha256,
    )

    verification = validate_final_evidence(scenario.bundle, scenario.journal)

    assert expected_reason in verification.reason_codes
    if trade_order_id_sha256 == _sha("venue-order-id"):
        assert scenario.bundle.trade.relevant_trades == (
            TradeEntry(
                trade_id_sha256=_sha("final-trade"),
                order_id_sha256=trade_order_id_sha256,
                quantity=Decimal("0.001"),
                fee=Decimal("0.0001"),
                realized_pnl=Decimal("0"),
            ),
        )
        assert scenario.bundle.trade.fee_delta == Decimal("0.0001")
        assert "FINAL_FEE_DELTA_NONZERO" in verification.reason_codes
        assert "UNEXPECTED_TRADES_PRESENT" not in verification.reason_codes
    else:
        assert scenario.bundle.trade.relevant_trades == ()
        assert scenario.bundle.trade.fee_delta == 0
        assert "RELEVANT_TRADES_PRESENT" not in verification.reason_codes


def test_non_exact_or_overclaiming_reap_is_rejected(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    valid = scenario.reap
    mismatched_identity = ProcessIdentity(
        pid=43211,
        ppid=1,
        pgid=43211,
        sid=43211,
        start_token="other-process",
    )
    bad_changes = (
        {"waited_pid": 43211},
        {"identity": mismatched_identity},
        {"local_process_quiesced": False},
        {"venue_mutation_absent_proven": True},
    )
    for changes in bad_changes:
        with pytest.raises(ProcessBoundaryError, match="PROCESS_REAP_ATTESTATION_INVALID"):
            replace(valid, **changes)

    journal_mismatch = replace(valid, returncode=1)
    with pytest.raises(FinalEvidenceError, match="PROCESS_REAP_NOT_REPLAYED"):
        _new_finalizer(scenario).finalize(scenario.bundle, journal_mismatch)
    assert not (scenario.root / "process-exit.json").exists()


def test_mapping_cannot_impersonate_a_process_controller_attestation(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    with pytest.raises(FinalEvidenceError, match="PROCESS_REAP_ATTESTATION_REQUIRED"):
        _new_finalizer(scenario).finalize(scenario.bundle, {})  # type: ignore[arg-type]


def test_process_journal_is_replayed_before_any_final_artifact(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    head_path = scenario.reap.process_journal_path.with_name("lifecycle.jsonl.head")
    head = json.loads(head_path.read_text(encoding="ascii"))
    head["digest"] = _sha("forged-process-head")
    head_path.write_text(
        json.dumps(head, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="ascii",
    )
    head_path.chmod(0o600)

    with pytest.raises(FinalEvidenceError, match="PROCESS_REAP_NOT_REPLAYED"):
        _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert not (scenario.root / "process-exit.json").exists()
    assert not (scenario.root / "manifest.json").exists()


def test_reap_attestation_must_cover_latest_inactive_generation(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    lifecycle = ProcessLifecycleJournal.restore(scenario.reap.process_journal_path)
    lifecycle.stage_identity(
        2,
        ProcessIdentity(
            pid=43211,
            ppid=1,
            pgid=43211,
            sid=43211,
            start_token="newer-generation",
        ),
    )

    with pytest.raises(FinalEvidenceError, match="PROCESS_REAP_NOT_REPLAYED"):
        _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert not (scenario.root / "process-exit.json").exists()
    assert not (scenario.root / "verdict.json").exists()


def test_process_exit_is_durable_before_manifest_and_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    original = final_evidence_module._atomic_write_owner_only

    def recording_write(path: Path, payload: bytes) -> None:
        writes.append(path.name)
        original(path, payload)

    monkeypatch.setattr(final_evidence_module, "_atomic_write_owner_only", recording_write)
    scenario = _scenario(tmp_path)
    finalized = _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert writes.index("process-exit.json") < writes.index("manifest.json")
    assert writes.index("process-exit.json") < writes.index("verdict.json")
    assert writes.index("manifest.json") < writes.index("verdict.json")
    assert finalized.verification.review_eligible is True
    verdict = json.loads(finalized.verdict_path.read_text(encoding="utf-8"))
    assert verdict["status"] == "READY_FOR_INDEPENDENT_REVIEW"
    assert verdict["gate_pass_declared"] is False
    assert "PASS_GATE" not in finalized.verdict_path.read_text(encoding="utf-8")


def test_final_artifact_publication_cannot_overwrite_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    target = root / "final-state.json"
    original_link = os.link

    def racing_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"racing-owner")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(FinalEvidenceError, match="FINALIZATION_ARTIFACT_EXISTS"):
        final_evidence_module._atomic_write_owner_only(target, b"authoritative")

    assert target.read_bytes() == b"racing-owner"


def test_final_artifact_publication_rejects_temporary_inode_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    target = root / "final-state.json"
    original_link = os.link

    def substituting_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        os.unlink(source, dir_fd=src_dir_fd)
        descriptor = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=src_dir_fd,
        )
        try:
            os.write(descriptor, b"substituted")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", substituting_link)

    with pytest.raises(FinalEvidenceError, match="FINALIZATION_TEMPORARY_INODE_CHANGED"):
        final_evidence_module._atomic_write_owner_only(target, b"authoritative")


def test_nonzero_or_signaled_child_exit_is_blocked_after_exact_reap(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, returncode=-9, signal=9)
    finalized = _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)
    verdict = json.loads(finalized.verdict_path.read_text(encoding="utf-8"))

    assert finalized.verification.review_eligible is False
    assert verdict["status"] == "BLOCKED"
    assert "CREDENTIAL_CHILD_EXIT_NONZERO" in verdict["reason_codes"]


def test_preflight_artifact_is_the_exact_durable_mechanical_projection(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)
    retained = json.loads((scenario.root / "preflight.json").read_text(encoding="ascii"))
    preflight = scenario.bundle.preflight
    projected = final_evidence_module.project_preflight_evidence(
        authority=preflight.authority,
        authority_record=preflight.authority_record,
        provenances=preflight.provenances,
        execution_journal=scenario.journal,
        protocol_commit=_PROTOCOL_COMMIT,
        protocol_tag_object=_PROTOCOL_TAG_OBJECT,
        protocol_sha256=_PROTOCOL_SHA256,
    ).artifact_payload

    assert retained["schema_version"] == "gate1b.preflight-projection.v1"
    for key in (
        "account_state",
        "authority",
        "baseline_trades",
        "filters",
        "intent_candidate",
        "order_derivation",
        "reads",
        "symbol_state",
    ):
        assert retained[key] == projected[key]
    assert "intent_binding" not in retained
    assert "raw_response" not in json.dumps(retained, sort_keys=True)
    assert "signed" not in json.dumps(retained, sort_keys=True).lower()


def test_manifest_is_allowlisted_reproducible_and_detached(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    root = scenario.root

    finalized = _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    manifest = json.loads(finalized.manifest_path.read_text(encoding="utf-8"))
    entries = manifest["artifacts"]
    paths = [entry["path"] for entry in entries]
    assert paths == sorted(paths)
    assert "intent.json" in paths
    assert "process-exit.json" in paths
    assert "preflight.json" in paths
    assert "final-state.json" in paths
    assert "manifest.json" not in paths
    assert "verdict.json" not in paths
    for entry in entries:
        artifact = root / entry["path"]
        assert entry["size"] == artifact.stat().st_size
        assert entry["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

    manifest_digest, manifest_name = _detached_digest(finalized.manifest_hash_path)
    verdict_digest, verdict_name = _detached_digest(finalized.verdict_hash_path)
    assert manifest_name == "manifest.json"
    assert verdict_name == "verdict.json"
    assert manifest_digest == hashlib.sha256(finalized.manifest_path.read_bytes()).hexdigest()
    assert verdict_digest == hashlib.sha256(finalized.verdict_path.read_bytes()).hexdigest()

    final_state = json.loads((root / "final-state.json").read_text(encoding="utf-8"))
    assert final_state["mutation_barrier"] == {
        "last_mutation_record_digest": scenario.bundle.barrier.last_mutation_record.digest,
        "last_mutation_record_sequence": scenario.bundle.barrier.last_mutation_record.sequence,
        "last_request_monotonic_sequence": 12,
        "last_request_sha256": scenario.bundle.barrier.last_request.request_sha256,
        "mutation_states": ["CONFIRMED"],
    }


def test_manifest_retains_the_requests_log_and_its_durable_head(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    _write_owner_only(scenario.root / "requests.jsonl", {"kind": "RESULT"})
    _write_owner_only(
        scenario.root / "requests.jsonl.head",
        {"digest": _sha("requests-head"), "sequence": 1},
    )

    finalized = _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)
    manifest = json.loads(finalized.manifest_path.read_text(encoding="utf-8"))

    assert {entry["path"] for entry in manifest["artifacts"]} >= {
        "requests.jsonl",
        "requests.jsonl.head",
    }


def test_finalizer_replays_the_persisted_intent_again_before_artifacts(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    retained_preflight = (scenario.root / "preflight.json").read_bytes()
    _write_owner_only(
        scenario.root / "intent.json",
        {"intent_sha256": _sha("tampered-after-bundle-construction")},
    )

    with pytest.raises(FinalEvidenceError, match="PREFLIGHT_INTENT_REPLAY_FAILED"):
        _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert not (scenario.root / "process-exit.json").exists()
    assert (scenario.root / "preflight.json").read_bytes() == retained_preflight


def test_every_emitted_artifact_and_directory_is_owner_only_and_fsynced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(final_evidence_module.os, "fsync", recording_fsync)
    scenario = _scenario(tmp_path)
    root = scenario.root
    _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    files = [candidate for candidate in root.iterdir() if candidate.is_file()]
    assert files
    assert all(stat.S_IMODE(candidate.stat().st_mode) == 0o600 for candidate in files)
    generated = [candidate for candidate in files if candidate.name.startswith("final-")]
    generated.extend(root / name for name in ("process-exit.json", "manifest.json", "verdict.json"))
    assert len(fsync_calls) >= len(generated) * 2
    assert not list(root.glob(".*.tmp"))


def test_unallowlisted_or_non_owner_artifact_prevents_manifest_and_verdict(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    root = scenario.root
    _write_owner_only(root / "surprise.txt", {"value": "not allowlisted"})

    with pytest.raises(FinalEvidenceError, match="ARTIFACT_NOT_ALLOWLISTED"):
        _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert not (root / "manifest.json").exists()
    assert not (root / "verdict.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"client_order_id": "synthetic-credential-canary"},
        {"signed_url": "https://demo.invalid/?signature=material"},
        {"headers": {"X-MBX-APIKEY": "material"}},
        {"raw_response": "must-not-be-retained"},
        {"note": "-----BEGIN PRIVATE KEY-----"},
    ],
)
def test_secret_canary_and_credential_derived_material_block_finalization(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    scenario = _scenario(tmp_path)
    root = scenario.root
    _write_owner_only(root / "child-pre-exit.json", payload)
    report = scan_evidence_tree(root, canary_tokens=("synthetic-credential-canary",))

    assert report.leak_count > 0
    assert all("synthetic-credential-canary" not in finding for finding in report.findings)
    with pytest.raises(FinalEvidenceError, match="CREDENTIAL_MATERIAL_DETECTED"):
        _new_finalizer(
            scenario,
            canary_tokens=("synthetic-credential-canary",),
        ).finalize(scenario.bundle, scenario.reap)
    assert not (root / "manifest.json").exists()
    assert not (root / "verdict.json").exists()


def test_canary_in_typed_payload_is_rejected_before_process_exit_is_written(tmp_path: Path) -> None:
    scenario = _scenario(
        tmp_path,
        reported_order_id="synthetic-credential-canary",
        queried_order_id="synthetic-credential-canary",
    )
    root = scenario.root

    with pytest.raises(FinalEvidenceError, match="CREDENTIAL_MATERIAL_DETECTED"):
        _new_finalizer(
            scenario,
            canary_tokens=("synthetic-credential-canary",),
        ).finalize(scenario.bundle, scenario.reap)

    assert not (root / "process-exit.json").exists()
    assert not (root / "manifest.json").exists()


def test_supervisor_environment_must_be_credential_free(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    with pytest.raises(FinalEvidenceError, match="SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT"):
        FinalEvidenceFinalizer(
            root=root,
            execution_journal_path=tmp_path / "missing-request-ledger.json",
            supervisor_environment={"BINANCE_DEMO_API_KEY": "do-not-read"},
        )

    assert not root.exists()


def test_supervisor_environment_canary_is_rejected_under_an_arbitrary_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    with pytest.raises(FinalEvidenceError, match="SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT"):
        FinalEvidenceFinalizer(
            root=root,
            execution_journal_path=tmp_path / "missing-request-ledger.json",
            supervisor_environment={"UNRELATED_NAME": "synthetic-credential-canary"},
            canary_tokens=("synthetic-credential-canary",),
        )

    assert not root.exists()


def test_blocked_evidence_is_preserved_without_a_pass_claim(tmp_path: Path) -> None:
    scenario = _scenario(
        tmp_path,
        status=OrderFinalStatus.FILLED,
        executed_quantity=Decimal("0.001"),
    )
    finalized = _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)
    verdict = json.loads(finalized.verdict_path.read_text(encoding="utf-8"))

    assert verdict["status"] == "BLOCKED"
    assert verdict["review_eligible"] is False
    assert verdict["gate_pass_declared"] is False
    assert "PROBE_ORDER_FILLED" in verdict["reason_codes"]
    assert "PROBE_EXECUTED_QUANTITY_NONZERO" in verdict["reason_codes"]


def test_corrupt_execution_journal_head_fails_closed_before_finalization(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    head_path = scenario.journal.anchor_path
    head = json.loads(head_path.read_text(encoding="ascii"))
    head["digest"] = _sha("wrong-execution-head")
    head_path.write_text(
        json.dumps(head, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="ascii",
    )
    head_path.chmod(0o600)

    with pytest.raises(FinalEvidenceError, match="EXECUTION_JOURNAL_REPLAY_FAILED"):
        _new_finalizer(scenario).finalize(scenario.bundle, scenario.reap)

    assert not (scenario.root / "process-exit.json").exists()
    assert not (scenario.root / "manifest.json").exists()


def test_frozen_final_schedule_has_exactly_seven_endpoint_provenances(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    assert tuple(
        (
            provenance.kind,
            provenance.reserved_request.method,
            provenance.reserved_request.path,
            dict(provenance.reserved_request.parameters),
            provenance.reservation_proof.read_kind,
        )
        for provenance in scenario.bundle.provenances
    ) == (
        (
            EvidenceKind.ORDER,
            "GET",
            "/fapi/v1/order",
            {"origClientOrderId": _CLIENT_ID, "recvWindow": "5000", "symbol": "ETHUSDT"},
            ReadKind.ORDER,
        ),
        (
            EvidenceKind.OPEN_REGULAR_ORDERS,
            "GET",
            "/fapi/v1/openOrders",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
        ),
        (
            EvidenceKind.OPEN_ALGO_ORDERS,
            "GET",
            "/fapi/v1/openAlgoOrders",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
        ),
        (
            EvidenceKind.TRADE,
            "GET",
            "/fapi/v1/userTrades",
            {"recvWindow": "5000", "symbol": "ETHUSDT"},
            ReadKind.TRADE,
        ),
        (
            EvidenceKind.ACCOUNT,
            "GET",
            "/fapi/v2/account",
            {"recvWindow": "5000"},
            ReadKind.ACCOUNT,
        ),
        (
            EvidenceKind.SYMBOL_CONFIG,
            "GET",
            "/fapi/v1/symbolConfig",
            {"recvWindow": "5000", "symbol": "ETHUSDT"},
            ReadKind.GENERAL,
        ),
        (
            EvidenceKind.POSITION_MODE,
            "GET",
            "/fapi/v1/positionSide/dual",
            {"recvWindow": "5000"},
            ReadKind.GENERAL,
        ),
    )


def test_position_and_account_are_bound_to_one_account_response(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    assert scenario.bundle.position.provenance == scenario.bundle.account.provenance
    provenance = scenario.bundle.account.provenance
    assert provenance.result_sha256 == provenance.transport_result.result_sha256
    assert dict(provenance.transport_result.fields) == dict(
        _final_transport_fields(
            status=OrderFinalStatus.CANCELED,
            executed_quantity=Decimal(0),
            client_order_id=_CLIENT_ID,
        )[EvidenceKind.ACCOUNT]
    )


def test_endpoint_kind_cannot_be_relabelled_or_reused(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    with pytest.raises(FinalEvidenceError, match="FINAL_READ_ENDPOINT_MISMATCH"):
        replace(
            scenario.bundle.open_orders.provenance,
            kind=EvidenceKind.OPEN_ALGO_ORDERS,
        )
    with pytest.raises(FinalEvidenceError, match="FINAL_EVIDENCE_KIND_MISMATCH"):
        replace(
            scenario.bundle.open_algo_orders,
            provenance=scenario.bundle.open_orders.provenance,
        )
    with pytest.raises(FinalEvidenceError, match="FINAL_READ_ENDPOINT_MISMATCH"):
        replace(
            scenario.bundle.order.provenance,
            reserved_request=replace(
                scenario.bundle.order.provenance.reserved_request,
                retry_index=1,
            ),
        )


def test_every_typed_endpoint_projection_is_digest_bound(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    bundle = scenario.bundle
    counterexample_changes = (
        {"order": replace(bundle.order, executed_quantity=Decimal("0.001"))},
        {
            "open_orders": replace(
                bundle.open_orders,
                open_order_id_sha256=(_sha("unexpected-open-order"),),
            )
        },
        {
            "open_algo_orders": replace(
                bundle.open_algo_orders,
                open_algo_order_id_sha256=(_sha("unexpected-open-algo-order"),),
            )
        },
        {"trade": replace(bundle.trade, fee_delta=Decimal("0.001"))},
        {
            "position": replace(
                bundle.position,
                nonzero_positions=(
                    PositionEntry(
                        symbol="ETHUSDT",
                        position_side="BOTH",
                        quantity=Decimal("0.001"),
                    ),
                ),
            )
        },
        {"account": replace(bundle.account, wallet_delta=Decimal("0.001"))},
        {
            "symbol_config": replace(
                bundle.symbol_config,
                symbol_config_matches=False,
            )
        },
        {
            "position_mode": replace(
                bundle.position_mode,
                position_mode_one_way=False,
            )
        },
    )

    for changes in counterexample_changes:
        with pytest.raises(FinalEvidenceError, match="FINAL_RESULT_PROJECTION_MISMATCH"):
            replace(bundle, **changes)


def test_runtime_binding_failure_has_a_distinct_blocked_finalization_cause() -> None:
    assert (
        BlockedFinalizationCause.RUNTIME_BINDING_FAILED.value
        == "FINALIZATION_BLOCKED_RUNTIME_BINDING_FAILED"
    )


def test_runtime_binding_failure_finalizes_blocked_after_exact_reap(tmp_path: Path) -> None:
    scenario = _incomplete_scenario(tmp_path, returncode=0, signal=None)
    finalized = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    ).finalize_blocked(
        scenario.reap,
        cause=BlockedFinalizationCause.RUNTIME_BINDING_FAILED,
    )

    verdict = json.loads(finalized.verdict_path.read_text(encoding="ascii"))
    assert verdict["status"] == "BLOCKED"
    assert verdict["review_eligible"] is False
    assert verdict["reason_codes"] == [
        "FINALIZATION_BLOCKED_RUNTIME_BINDING_FAILED",
        "FINAL_EVIDENCE_INCOMPLETE",
    ]
    assert not list(scenario.root.glob("final-*.json"))


def test_incomplete_crashed_child_can_emit_only_blocked_finalization(
    tmp_path: Path,
) -> None:
    scenario = _incomplete_scenario(tmp_path)
    finalizer = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    )

    finalized = finalizer.finalize_blocked(
        scenario.reap,
        cause=BlockedFinalizationCause.CREDENTIAL_CHILD_CRASH,
    )

    verdict_text = finalized.verdict_path.read_text(encoding="utf-8")
    verdict = json.loads(verdict_text)
    assert verdict["status"] == "BLOCKED"
    assert verdict["review_eligible"] is False
    assert verdict["gate_pass_declared"] is False
    assert verdict["reason_codes"] == [
        "CREDENTIAL_CHILD_EXIT_NONZERO",
        "FINALIZATION_BLOCKED_CREDENTIAL_CHILD_CRASH",
        "FINAL_EVIDENCE_INCOMPLETE",
    ]
    assert "READY_FOR_INDEPENDENT_REVIEW" not in verdict_text
    assert "PASS_GATE" not in verdict_text
    assert (scenario.root / "process-exit.json").is_file()
    assert not list(scenario.root.glob("final-*.json"))


def test_incomplete_clean_exit_cannot_be_relabelled_ready(tmp_path: Path) -> None:
    scenario = _incomplete_scenario(tmp_path, returncode=0, signal=None)
    finalizer = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    )

    finalized = finalizer.finalize_blocked(
        scenario.reap,
        cause=BlockedFinalizationCause.FINAL_READ_SCHEDULE_INCOMPLETE,
    )

    verdict = json.loads(finalized.verdict_path.read_text(encoding="utf-8"))
    assert verdict["status"] == "BLOCKED"
    assert verdict["review_eligible"] is False
    assert verdict["gate_pass_declared"] is False
    assert verdict["reason_codes"] == [
        "FINALIZATION_BLOCKED_FINAL_READ_SCHEDULE_INCOMPLETE",
        "FINAL_EVIDENCE_INCOMPLETE",
    ]


def test_blocked_finalization_requires_typed_cause_and_real_reap(tmp_path: Path) -> None:
    scenario = _incomplete_scenario(tmp_path)
    finalizer = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
    )

    with pytest.raises(FinalEvidenceError, match="BLOCKED_FINALIZATION_CAUSE_REQUIRED"):
        finalizer.finalize_blocked(scenario.reap, cause="child crashed")  # type: ignore[arg-type]
    with pytest.raises(FinalEvidenceError, match="PROCESS_REAP_ATTESTATION_REQUIRED"):
        finalizer.finalize_blocked(  # type: ignore[arg-type]
            {},
            cause=BlockedFinalizationCause.FINAL_READ_SCHEDULE_INCOMPLETE,
        )
    assert not (scenario.root / "process-exit.json").exists()


def test_blocked_finalization_retains_canary_zero_leak_rule(tmp_path: Path) -> None:
    scenario = _incomplete_scenario(tmp_path)
    _write_owner_only(
        scenario.root / "child-pre-exit.json",
        {"note": "synthetic-credential-canary"},
    )
    finalizer = FinalEvidenceFinalizer(
        root=scenario.root,
        execution_journal_path=scenario.journal.path,
        supervisor_environment={"PATH": "/usr/bin:/bin"},
        canary_tokens=("synthetic-credential-canary",),
    )

    with pytest.raises(FinalEvidenceError, match="CREDENTIAL_MATERIAL_DETECTED"):
        finalizer.finalize_blocked(
            scenario.reap,
            cause=BlockedFinalizationCause.CREDENTIAL_CHILD_CRASH,
        )

    assert not (scenario.root / "process-exit.json").exists()
    assert not (scenario.root / "verdict.json").exists()
