from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest

import global_quant.gate1b.execution_journal as journal_module
from global_quant.gate1b.credential_transport import ResponseKind, TransportResult
from global_quant.gate1b.execution_journal import (
    HEAD_SCHEMA_VERSION,
    MAX_RECORD_BYTES,
    SCHEMA_VERSION,
    ZERO_DIGEST,
    BoundaryResult,
    DurableGenerationAdmission,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    GenerationCapability,
    MutationAttempt,
    MutationKind,
    MutationPurpose,
    MutationReservationProof,
    OwnedFillCloseProof,
    ProcessReapReceipt,
    ReadKind,
    ReadOutcome,
    ReadPurpose,
    ReadReservationProof,
    ReadResultProof,
    ReconciledOrderStatus,
    ReconciliationKey,
    ReconciliationKeyKind,
    ReconciliationObservation,
    RecoveryMode,
)
from global_quant.gate1b.mutation_protocol import (
    DEMO_HTTP_ORIGIN,
    RECEIVE_WINDOW_MS,
    SYMBOL,
    DurableIntent,
    LimitOrderFilters,
    MarketCloseFilters,
    MarketCloseProof,
    MutationLedger,
    MutationProtocolError,
    OrderDerivationProof,
    OwnedPositionProof,
    RequestPurpose,
    RequestStage,
    ReservedRequest,
    build_client_order_id,
    build_emergency_client_order_id,
)

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
DEADLINE_NS = 9_000_000_000


def _owned_exchange_fields(*, min_notional: str = "5") -> tuple[tuple[str, object], ...]:
    return tuple(
        sorted(
            {
                "contractType": "PERPETUAL",
                "filterTypeCounts": {
                    "LOT_SIZE": 1,
                    "MARKET_LOT_SIZE": 1,
                    "MIN_NOTIONAL": 1,
                    "PERCENT_PRICE": 1,
                    "PRICE_FILTER": 1,
                },
                "limitLotSize": {
                    "maxQuantity": "100",
                    "minQuantity": "0.001",
                    "stepSize": "0.001",
                },
                "marginAsset": "USDT",
                "marketLotSize": {
                    "maxQuantity": "1",
                    "minQuantity": "0.001",
                    "stepSize": "0.001",
                },
                "minNotional": min_notional,
                "orderTypes": ["LIMIT", "MARKET"],
                "percentPrice": {"multiplierDown": "0.85", "multiplierUp": "1.05"},
                "priceFilter": {
                    "maxPrice": "5000",
                    "minPrice": "1000",
                    "tickSize": "0.01",
                },
                "quoteAsset": "USDT",
                "status": "TRADING",
                "symbol": SYMBOL,
                "timeInForce": ["GTX"],
                "uninterpretedFilterTypes": [],
            }.items()
        )
    )


def _owned_exchange_result_sha256() -> str:
    return TransportResult.build(
        request_sha256="a" * 64,
        logical_request_sha256="b" * 64,
        kind=ResponseKind.EXCHANGE_INFO,
        fields=_owned_exchange_fields(),
    ).result_sha256


def _test_durable_intent(*, persisted: bool) -> DurableIntent:
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
        filter_snapshot_sha256=_owned_exchange_result_sha256(),
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


INTENT_SHA256 = _test_durable_intent(persisted=True).intent_sha256


def _default_create_reserved_request() -> ReservedRequest:
    intent = _test_durable_intent(persisted=True)
    ledger = MutationLedger(
        total_http_requests=12,
        create_requests=1,
        stage=RequestStage.CREATE_ATTEMPTED,
        last_elapsed_seconds=Decimal("12"),
    )
    return ReservedRequest(
        ledger=ledger,
        intent_sha256=intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=tuple(sorted(intent.probe_payload.items())),
        elapsed_seconds=Decimal("12"),
        retry_index=0,
    )


_DEFAULT_CREATE_REQUEST_SHA256 = _default_create_reserved_request().request_sha256


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _identity_sha(generation: int) -> str:
    return _sha(f"process-identity-{generation}")


def _admission(generation: int) -> DurableGenerationAdmission:
    return DurableGenerationAdmission(
        generation=generation,
        process_identity_sha256=_identity_sha(generation),
    )


def _reap_receipt(
    generation: int,
    *,
    process_identity_sha256: str | None = None,
    admission_record_sequence: int | None = None,
    admission_record_digest: str | None = None,
    local_process_quiesced: bool = True,
    venue_mutation_absent_proven: bool = False,
) -> ProcessReapReceipt:
    return ProcessReapReceipt(
        generation=generation,
        process_identity_sha256=process_identity_sha256 or _identity_sha(generation),
        admission_record_sequence=admission_record_sequence or generation + 1,
        admission_record_digest=admission_record_digest or _sha(f"admission-record-{generation}"),
        returncode=-9,
        signal=9,
        local_process_quiesced=local_process_quiesced,
        venue_mutation_absent_proven=venue_mutation_absent_proven,
    )


def _admit(
    journal: ExecutionJournal,
    generation: int,
    capability: GenerationCapability,
    *,
    bind_request_chain: bool = True,
) -> None:
    journal.admit_generation(_admission(generation), capability)
    if bind_request_chain and generation == 1 and capability is GenerationCapability.PRIMARY:
        _establish_test_request_chain(journal)


def _reap(journal: ExecutionJournal, generation: int) -> None:
    admission_record = next(
        record
        for record in journal.records()
        if type(record.event).__name__ == "_GenerationAdmitted"
        and record.event.generation == generation
    )
    journal.reap_generation(
        _reap_receipt(
            generation,
            admission_record_sequence=admission_record.sequence,
            admission_record_digest=admission_record.digest,
        )
    )


def _open_observation(
    journal: ExecutionJournal,
    source: MutationAttempt,
    *,
    generation: int,
    status: ReconciledOrderStatus = ReconciledOrderStatus.NEW,
    reservation: str = "fresh-open-read",
) -> ReconciliationObservation:
    outcome = {
        ReconciledOrderStatus.NEW: ReadOutcome.ORDER_NEW,
        ReconciledOrderStatus.PARTIALLY_FILLED: ReadOutcome.ORDER_PARTIALLY_FILLED,
    }[status]
    sequence = (
        sum(
            type(record.event).__name__ in {"_MutationReserved", "_ReadPrepared"}
            for record in journal.records()
        )
        + 1
    )
    result = _record_read(
        journal,
        label=f"{reservation}-g{generation}-s{sequence}",
        generation=generation,
        sequence=sequence,
        kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        outcome=outcome,
        source=source,
        path="/fapi/v1/order",
    )
    observation = journal.new_reconciliation_observation(
        source_attempt_id=source.attempt_id,
        read_result_proof_sha256=result.result_proof_sha256,
    )
    journal.record_reconciliation_observation(observation)
    return observation


def _attempt(
    kind: MutationKind = MutationKind.CREATE,
    *,
    generation: int = 1,
    reservation: str = "reservation-1",
    recovery_of_attempt_id: str | None = None,
) -> MutationAttempt:
    reservation_sha256 = (
        _DEFAULT_CREATE_REQUEST_SHA256
        if kind is MutationKind.CREATE and generation == 1
        else _sha(reservation)
    )
    return MutationAttempt.build(
        kind=kind,
        generation=generation,
        retry_index=0,
        deadline_ns=DEADLINE_NS + generation,
        reservation_sha256=reservation_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=_sha(f"open-{reservation}")
        if kind is MutationKind.CANCEL
        else None,
        recovery_of_attempt_id=recovery_of_attempt_id,
    )


def _reserved_create(
    journal: ExecutionJournal,
    *,
    generation: int = 1,
    label: str,
) -> MutationAttempt:
    del label
    authority, intent = _ensure_test_request_chain(journal)
    reserved = _default_create_reserved_request()
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=generation,
        deadline_ns=DEADLINE_NS + generation,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=generation,
        deadline_ns=DEADLINE_NS + generation,
        client_id=authority.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    journal.record_mutation_reservation(proof)
    return MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=generation,
        retry_index=0,
        deadline_ns=DEADLINE_NS + generation,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )


def _reserved_noncreate(
    journal: ExecutionJournal,
    *,
    label: str,
    kind: MutationKind,
    purpose: MutationPurpose,
    generation: int,
    source: MutationAttempt,
    precondition_sha256: str,
    recovery: bool = False,
    parameters_sha256: str | None = None,
    parameters_override: dict[str, str] | None = None,
) -> MutationAttempt:
    del label
    authority, intent = _ensure_test_request_chain(journal)
    client_id = (
        build_emergency_client_order_id(source.runtime_commit, source.session_nonce)
        if kind is MutationKind.EMERGENCY_CLOSE
        else source.client_id
    )
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    elapsed = previous.last_elapsed_seconds + Decimal("1")
    if kind is MutationKind.CANCEL:
        request_purpose = RequestPurpose.CANCEL
        method = "DELETE"
        parameters = parameters_override or intent.cancel_parameters
        ledger = replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            cancel_requests=previous.cancel_requests + 1,
            stage=RequestStage.CANCEL_ATTEMPTED,
            last_elapsed_seconds=elapsed,
        )
    else:
        request_purpose = RequestPurpose.EMERGENCY_CLOSE
        method = "POST"
        parameters = parameters_override or intent.emergency_close_payload(Decimal("0.001"))
        ledger = replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            emergency_close_requests=previous.emergency_close_requests + 1,
            stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
            last_elapsed_seconds=elapsed,
        )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=source.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method=method,
        path="/fapi/v1/order",
        purpose=request_purpose,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=elapsed,
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=_request_authority_sha256(
            journal,
            generation=generation,
            source=source,
        ),
        generation=generation,
        deadline_ns=DEADLINE_NS + generation + 100,
        reserved_request=reserved,
    )
    correct = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=purpose,
        generation=generation,
        deadline_ns=DEADLINE_NS + generation + 100,
        client_id=client_id,
        authorization_id=source.authorization_id,
        source_attempt_id=source.attempt_id,
        precondition_sha256=precondition_sha256,
    )
    proof = correct
    if parameters_sha256 is not None:
        proof = MutationReservationProof.build(
            request_sha256=correct.request_sha256,
            logical_request_sha256=correct.logical_request_sha256,
            kind=correct.kind,
            purpose=correct.purpose,
            method=correct.method,
            path=correct.path,
            retry_index=correct.retry_index,
            client_id=correct.client_id,
            authorization_id=correct.authorization_id,
            intent_sha256=correct.intent_sha256,
            generation=correct.generation,
            deadline_ns=correct.deadline_ns,
            monotonic_sequence=correct.monotonic_sequence,
            parameters_sha256=parameters_sha256,
            ledger_sha256=correct.ledger_sha256,
            source_attempt_id=correct.source_attempt_id,
            precondition_sha256=correct.precondition_sha256,
        )
    journal.record_mutation_reservation(proof)
    return MutationAttempt.build(
        kind=kind,
        generation=generation,
        retry_index=0,
        deadline_ns=DEADLINE_NS + generation,
        reservation_sha256=reserved.request_sha256,
        authorization_id=source.authorization_id,
        intent_sha256=source.intent_sha256,
        runtime_commit=source.runtime_commit,
        session_nonce=source.session_nonce,
        fresh_open_proof_sha256=(precondition_sha256 if kind is MutationKind.CANCEL else None),
        recovery_of_attempt_id=source.attempt_id if recovery else None,
    )


def _prepare(journal: ExecutionJournal, attempt: MutationAttempt):
    """Test adapter that records the exact CREATE request before frontier behavior."""

    records = journal.records()
    admitted = {
        record.event.generation: record.event.capability
        for record in records
        if type(record.event).__name__ == "_GenerationAdmitted"
    }
    reaped = {
        record.event.receipt.generation
        for record in records
        if type(record.event).__name__ == "_GenerationReaped"
    }
    capability = admitted.get(attempt.generation)
    if capability is None or attempt.generation in reaped:
        return journal.prepare_attempt(attempt)
    if capability is GenerationCapability.RECOVERY and attempt.kind is MutationKind.CREATE:
        return journal.prepare_attempt(attempt)
    already_proven = any(
        type(record.event).__name__ == "_MutationReserved"
        and record.event.proof.request_sha256 == attempt.reservation_sha256
        for record in records
    )
    if not already_proven:
        if attempt.kind is not MutationKind.CREATE:
            return journal.prepare_attempt(attempt)
        authority, _intent = _ensure_test_request_chain(journal)
        reserved = _default_create_reserved_request()
        if attempt.reservation_sha256 != reserved.request_sha256:
            return journal.prepare_attempt(attempt)
        already_exact = any(
            type(record.event).__name__ == "_ExactRequestReserved"
            and record.event.reserved_request.request_sha256 == attempt.reservation_sha256
            for record in records
        )
        if not already_exact:
            journal.record_exact_request_reservation(
                authority_sha256=authority.authority_sha256,
                generation=attempt.generation,
                deadline_ns=attempt.deadline_ns,
                reserved_request=reserved,
            )
        proof = MutationReservationProof.from_reserved_request(
            reserved,
            purpose=MutationPurpose.PRIMARY_CREATE,
            generation=attempt.generation,
            deadline_ns=attempt.deadline_ns,
            client_id=attempt.client_id,
            authorization_id=attempt.authorization_id,
            source_attempt_id=None,
            precondition_sha256=None,
        )
        journal.record_mutation_reservation(proof)
    return journal.prepare_attempt(attempt)


def _record_read(
    journal: ExecutionJournal,
    *,
    label: str,
    generation: int,
    sequence: int,
    kind: ReadKind,
    purpose: ReadPurpose,
    outcome: ReadOutcome,
    source: MutationAttempt | None,
    path: str | None = None,
) -> ReadResultProof:
    del sequence
    authority, intent = _ensure_test_request_chain(journal)
    selected_path = (
        path
        or {
            ReadKind.ORDER: "/fapi/v1/order",
            ReadKind.TRADE: "/fapi/v1/userTrades",
            ReadKind.ACCOUNT: "/fapi/v2/account",
            ReadKind.SYMBOL_FILTER: "/fapi/v1/exchangeInfo",
            ReadKind.MARK_PRICE: "/fapi/v1/premiumIndex",
            ReadKind.GENERAL: "/fapi/v1/time",
        }[kind]
    )
    client_id = source.client_id if source is not None else authority.client_id
    parameters = {
        "/fapi/v1/order": {
            "symbol": SYMBOL,
            "origClientOrderId": client_id,
            "recvWindow": str(RECEIVE_WINDOW_MS),
        },
        "/fapi/v1/userTrades": {
            "symbol": SYMBOL,
            "recvWindow": str(RECEIVE_WINDOW_MS),
        },
        "/fapi/v2/account": {"recvWindow": str(RECEIVE_WINDOW_MS)},
        "/fapi/v1/exchangeInfo": {},
        "/fapi/v1/premiumIndex": {"symbol": SYMBOL},
        "/fapi/v1/time": {},
    }[selected_path]
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    elapsed = previous.last_elapsed_seconds + Decimal("1")
    ledger = replace(
        previous,
        total_http_requests=previous.total_http_requests + 1,
        post_create_read_requests=previous.post_create_read_requests
        + (1 if previous.create_requests == 1 else 0),
        last_elapsed_seconds=elapsed,
    )
    reserved = ReservedRequest(
        ledger=ledger,
        intent_sha256=intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path=selected_path,
        purpose=RequestPurpose.READ,
        parameters=tuple(sorted(parameters.items())),
        elapsed_seconds=elapsed,
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=_request_authority_sha256(
            journal,
            generation=generation,
            source=source,
        ),
        generation=generation,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )
    reservation = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=kind,
        purpose=purpose,
        generation=generation,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=source.attempt_id if source is not None else None,
        client_id=source.client_id if source is not None else None,
        authorization_id=source.authorization_id if source is not None else AUTHORIZATION_ID,
    )
    prepared = journal.record_read_prepared(reservation)
    result = ReadResultProof.build(
        request_sha256=reservation.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest=prepared.digest,
        generation=generation,
        monotonic_sequence=reserved.ledger.total_http_requests,
        read_kind=kind,
        outcome=outcome,
        result_sha256=_sha(f"{label}-result"),
        observed_at_ns=DEADLINE_NS,
    )
    journal.record_read_result(result)
    return result


def _owned_fill_proof(
    journal: ExecutionJournal,
    source: MutationAttempt,
    *,
    generation: int,
    label: str,
    read_purpose: ReadPurpose = ReadPurpose.OWNED_FILL_CLOSE,
    record_proof: bool = True,
    recorded_result_digest_overrides: dict[ReadKind, str] | None = None,
    filter_snapshot_sha256: str | None = None,
    residual_quantity: Decimal = Decimal("0.001"),
) -> OwnedFillCloseProof:
    authority, intent = _ensure_test_request_chain(journal)
    specifications = (
        (
            "order",
            ReadKind.ORDER,
            ReadOutcome.OWNED_ORDER_FILL_CONFIRMED,
            "/fapi/v1/order",
        ),
        (
            "trade",
            ReadKind.TRADE,
            ReadOutcome.OWNED_TRADE_FILL_CONFIRMED,
            "/fapi/v1/userTrades",
        ),
        (
            "account",
            ReadKind.ACCOUNT,
            ReadOutcome.OWNED_ACCOUNT_POSITION_CONFIRMED,
            "/fapi/v2/account",
        ),
        (
            "symbol_filter",
            ReadKind.SYMBOL_FILTER,
            ReadOutcome.FILTER_SNAPSHOT_CONFIRMED,
            "/fapi/v1/exchangeInfo",
        ),
        (
            "mark_price",
            ReadKind.MARK_PRICE,
            ReadOutcome.MARK_PRICE_CONFIRMED,
            "/fapi/v1/premiumIndex",
        ),
    )
    request_sha_by_path: dict[str, str] = {}
    transports: dict[str, TransportResult] = {}
    reservations: dict[str, ReadReservationProof] = {}
    reservation_elapsed: dict[str, Decimal] = {}
    owned_order_id_sha256 = hashlib.sha256(f"owned-fill-order\0{label}".encode("ascii")).hexdigest()
    filter_min_notional = "6" if filter_snapshot_sha256 is not None else "5"
    for name, kind, outcome, path in specifications:
        parameters = {
            "/fapi/v1/order": {
                "symbol": SYMBOL,
                "origClientOrderId": source.client_id,
                "recvWindow": str(RECEIVE_WINDOW_MS),
            },
            "/fapi/v1/userTrades": {
                "symbol": SYMBOL,
                "recvWindow": str(RECEIVE_WINDOW_MS),
            },
            "/fapi/v2/account": {"recvWindow": str(RECEIVE_WINDOW_MS)},
            "/fapi/v1/exchangeInfo": {},
            "/fapi/v1/premiumIndex": {"symbol": SYMBOL},
        }[path]
        previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
        elapsed = previous.last_elapsed_seconds + Decimal("1")
        ledger = replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            post_create_read_requests=previous.post_create_read_requests + 1,
            last_elapsed_seconds=elapsed,
        )
        reserved = ReservedRequest(
            ledger=ledger,
            intent_sha256=intent.intent_sha256,
            origin=DEMO_HTTP_ORIGIN,
            method="GET",
            path=path,
            purpose=RequestPurpose.READ,
            parameters=tuple(sorted(parameters.items())),
            elapsed_seconds=elapsed,
            retry_index=0,
        )
        journal.record_exact_request_reservation(
            authority_sha256=_request_authority_sha256(
                journal,
                generation=generation,
                source=source,
            ),
            generation=generation,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=reserved,
        )
        reservation = ReadReservationProof.from_reserved_request(
            reserved,
            read_kind=kind,
            purpose=read_purpose,
            generation=generation,
            deadline_ns=DEADLINE_NS + 100,
            source_attempt_id=source.attempt_id,
            client_id=source.client_id,
            authorization_id=source.authorization_id,
        )
        prepared = journal.record_read_prepared(reservation)
        request_sha_by_path[path] = reservation.request_sha256
        fields: dict[str, tuple[tuple[str, object], ...]] = {
            "order": tuple(
                sorted(
                    {
                        "clientOrderId": source.client_id,
                        "executedQty": format(residual_quantity, "f"),
                        "orderIdSha256": owned_order_id_sha256,
                        "origQty": format(residual_quantity, "f"),
                        "positionSide": "BOTH",
                        "price": "1980",
                        "reduceOnly": False,
                        "side": "BUY",
                        "status": "FILLED",
                        "symbol": SYMBOL,
                        "timeInForce": "GTX",
                        "type": "LIMIT",
                    }.items()
                )
            ),
            "trade": (
                ("count", 1),
                (
                    "trades",
                    [
                        {
                            "commission": "0.00001",
                            "orderIdSha256": owned_order_id_sha256,
                            "quantity": format(residual_quantity, "f"),
                            "realizedPnl": "0",
                            "tradeIdSha256": _sha(f"owned-fill-trade-{label}"),
                        }
                    ],
                ),
            ),
            "account": tuple(
                sorted(
                    {
                        "balances": [
                            {
                                "asset": "USDT",
                                "availableBalance": "100",
                                "walletBalance": "100",
                            }
                        ],
                        "canTrade": True,
                        "multiAssetsMargin": False,
                        "nonzeroPositions": [
                            {
                                "positionAmt": format(residual_quantity, "f"),
                                "positionSide": "BOTH",
                                "symbol": SYMBOL,
                            }
                        ],
                    }.items()
                )
            ),
            "symbol_filter": _owned_exchange_fields(min_notional=filter_min_notional),
            "mark_price": tuple(
                sorted(
                    {
                        "localMonotonicAfterNs": DEADLINE_NS - 1,
                        "localMonotonicBeforeNs": DEADLINE_NS - 2,
                        "localWallAfterMs": 1_800_000_000_020,
                        "localWallBeforeMs": 1_800_000_000_000,
                        "markPrice": "100000",
                        "symbol": SYMBOL,
                        "time": 1_800_000_000_000,
                    }.items()
                )
            ),
        }
        response_kind = {
            "order": ResponseKind.ORDER_OBSERVATION,
            "trade": ResponseKind.USER_TRADES,
            "account": ResponseKind.ACCOUNT,
            "symbol_filter": ResponseKind.EXCHANGE_INFO,
            "mark_price": ResponseKind.MARK_PRICE,
        }[name]
        transport = TransportResult.build(
            request_sha256=reservation.request_sha256,
            logical_request_sha256=reserved.logical_request_sha256,
            kind=response_kind,
            fields=fields[name],
        )
        result = ReadResultProof.build(
            request_sha256=reservation.request_sha256,
            prepared_record_sequence=prepared.sequence,
            prepared_record_digest=prepared.digest,
            generation=generation,
            monotonic_sequence=reservation.monotonic_sequence,
            read_kind=kind,
            outcome=outcome,
            result_sha256=(recorded_result_digest_overrides or {}).get(
                kind,
                transport.result_sha256,
            ),
            observed_at_ns=DEADLINE_NS,
        )
        journal.record_read_result(result)
        reservations[name] = reservation
        reservation_elapsed[name] = reserved.elapsed_seconds
        transports[name] = transport
    filters = MarketCloseFilters(
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("1"),
        step_size=Decimal("0.001"),
        min_notional=Decimal(filter_min_notional),
        market_lot_size_filter_count=1,
        min_notional_filter_count=1,
        uninterpreted_applicable_filter_types=(),
    )
    market_proof = MarketCloseProof(
        filter_snapshot_sha256=transports["symbol_filter"].result_sha256,
        filter_contract_sha256=filters.canonical_sha256,
        filters=filters,
        quantity=residual_quantity,
        mark_price=Decimal("100000"),
        mark_price_age_ms=Decimal("10"),
        observed_elapsed_seconds=reservation_elapsed["mark_price"],
    )
    position_proof = OwnedPositionProof(
        intent_sha256=source.intent_sha256,
        symbol=SYMBOL,
        residual_quantity=residual_quantity,
        owned_executed_quantity=residual_quantity,
        position_direction="LONG",
        probe_terminal_status="FILLED",
        open_remainder_quantity=Decimal("0"),
        other_activity_absent=True,
        market_close_proof=market_proof,
        observed_after_http_attempt=max(
            reservation.monotonic_sequence for reservation in reservations.values()
        ),
        source_request_sha256s=tuple(sorted(request_sha_by_path.items())),
        observed_elapsed_seconds=max(reservation_elapsed.values()),
    )
    proof = journal.new_owned_fill_close_proof(
        source_attempt_id=source.attempt_id,
        owned_position_proof=position_proof,
        order_transport_result=transports["order"],
        trade_transport_result=transports["trade"],
        account_transport_result=transports["account"],
        symbol_filter_transport_result=transports["symbol_filter"],
        mark_price_transport_result=transports["mark_price"],
    )
    if record_proof:
        journal.record_owned_fill_close_proof(proof)
    return proof


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _raw_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def _set_digest(record: dict[str, object]) -> None:
    body = {key: value for key, value in record.items() if key != "digest"}
    record["digest"] = hashlib.sha256(_canonical(body)).hexdigest()


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(_canonical(record) + b"\n" for record in records))


def _write_anchor(path: Path, *, sequence: int, digest: str) -> None:
    path.write_bytes(
        _canonical(
            {
                "schema_version": HEAD_SCHEMA_VERSION,
                "sequence": sequence,
                "digest": digest,
            }
        )
        + b"\n"
    )
    path.chmod(0o600)


def _append_forged_event(path: Path, event: dict[str, object]) -> None:
    records = _raw_records(path)
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "previous_digest": records[-1]["digest"],
        "event": event,
    }
    _set_digest(record)
    records.append(record)
    _write_records(path, records)


def _unknown_attempt(
    tmp_path: Path,
    kind: MutationKind,
) -> tuple[ExecutionJournal, MutationAttempt]:
    journal = ExecutionJournal(tmp_path / f"{kind.value.lower()}.jsonl")
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    if kind is MutationKind.CREATE:
        attempt = _reserved_create(journal, label="unknown-create")
    else:
        source = _reserved_create(journal, label=f"{kind.value.lower()}-source")
        _prepare(journal, source)
        journal.record_go(source.attempt_id)
        journal.record_confirmed(source.attempt_id, _sha(f"{kind.value}-source-result"))
        if kind is MutationKind.CANCEL:
            observation = _open_observation(journal, source, generation=1)
            attempt = _reserved_noncreate(
                journal,
                label="unknown-cancel",
                kind=kind,
                purpose=MutationPurpose.PRIMARY_CANCEL,
                generation=1,
                source=source,
                precondition_sha256=observation.observation_sha256,
            )
        else:
            owned_proof = _owned_fill_proof(
                journal,
                source,
                generation=1,
                label="unknown-close",
            )
            attempt = _reserved_noncreate(
                journal,
                label="unknown-close",
                kind=kind,
                purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
                generation=1,
                source=source,
                precondition_sha256=owned_proof.proof_sha256,
            )
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)
    _reap(journal, 1)
    assert (
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.ABSENT)
        is FrontierState.UNKNOWN
    )
    return journal, attempt


def test_new_journal_is_owner_only_canonical_and_hash_chained(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"

    journal = ExecutionJournal(path)
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw_lines = path.read_bytes().splitlines(keepends=True)
    assert all(
        line.endswith(b"\n") and line == _canonical(json.loads(line)) + b"\n" for line in raw_lines
    )
    records = _raw_records(path)
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["previous_digest"] == ZERO_DIGEST
    assert records[1]["previous_digest"] == records[0]["digest"]
    for record in records:
        body = {key: value for key, value in record.items() if key != "digest"}
        assert record["digest"] == hashlib.sha256(_canonical(body)).hexdigest()
    anchor = json.loads(journal.anchor_path.read_text(encoding="ascii"))
    assert stat.S_IMODE(journal.anchor_path.stat().st_mode) == 0o600
    assert anchor == {
        "schema_version": HEAD_SCHEMA_VERSION,
        "sequence": records[-1]["sequence"],
        "digest": records[-1]["digest"],
    }


def test_creation_fsyncs_file_then_parent_directory(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)

    ExecutionJournal(tmp_path / "execution.jsonl")

    assert calls == ["file", "file", "directory"]


def test_every_event_append_is_fsynced(tmp_path, monkeypatch) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    calls: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)

    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )

    assert calls == ["file", "file", "directory"]


def test_complete_tail_rollback_is_detected_by_durable_anchor(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    attempt = _attempt()
    _prepare(journal, attempt)
    records = _raw_records(path)

    _write_records(path, records[:-1])

    with pytest.raises(ExecutionJournalError, match="JOURNAL_ANCHOR_AHEAD"):
        ExecutionJournal(path)


def test_anchor_digest_mismatch_at_same_sequence_fails_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    records = _raw_records(path)
    _write_anchor(
        journal.anchor_path,
        sequence=len(records),
        digest="f" * 64,
    )

    with pytest.raises(ExecutionJournalError, match="JOURNAL_ANCHOR_DIGEST_MISMATCH"):
        ExecutionJournal(path)


def test_journal_ahead_of_anchor_is_conservatively_accepted_and_repaired(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    records = _raw_records(path)
    lagging = records[-2]
    _write_anchor(
        journal.anchor_path,
        sequence=lagging["sequence"],
        digest=lagging["digest"],
    )

    reopened = ExecutionJournal(path)

    repaired = json.loads(reopened.anchor_path.read_text(encoding="ascii"))
    assert repaired["sequence"] == records[-1]["sequence"]
    assert repaired["digest"] == records[-1]["digest"]
    assert reopened.frontier(attempt.attempt_id) is FrontierState.PREPARED


def test_go_survives_anchor_fsync_failure_and_reopen_repairs_head(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    real_fsync = os.fsync
    regular_fsyncs = 0

    def fail_anchor_fsync(fd: int) -> None:
        nonlocal regular_fsyncs
        if stat.S_ISREG(os.fstat(fd).st_mode):
            regular_fsyncs += 1
            if regular_fsyncs == 2:
                raise OSError("injected anchor fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_anchor_fsync)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_ANCHOR_FSYNC_FAILED"):
        journal.record_go(attempt.attempt_id)
    monkeypatch.undo()

    reopened = ExecutionJournal(path)
    assert reopened.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    records = _raw_records(path)
    repaired = json.loads(reopened.anchor_path.read_text(encoding="ascii"))
    assert repaired["sequence"] == records[-1]["sequence"]
    assert repaired["digest"] == records[-1]["digest"]


@pytest.mark.parametrize("special", ["symlink", "directory", "fifo"])
def test_symlink_and_non_regular_paths_fail_closed(tmp_path, special) -> None:
    path = tmp_path / "execution.jsonl"
    if special == "symlink":
        target = tmp_path / "target.jsonl"
        ExecutionJournal(target)
        path.symlink_to(target)
    elif special == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_NOT_SAFE_REGULAR_FILE"):
        ExecutionJournal(path)


def test_insecure_mode_fails_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    path.chmod(0o640)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_INSECURE_MODE"):
        ExecutionJournal(path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda _path: b"not-json\n", "JOURNAL_MALFORMED"),
        (lambda path: path.read_bytes().rstrip(b"\n"), "JOURNAL_TRUNCATED"),
        (lambda _path: b"{" + b"x" * MAX_RECORD_BYTES + b"\n", "JOURNAL_RECORD_OVERSIZED"),
    ],
)
def test_malformed_truncated_and_oversized_records_fail_closed(
    tmp_path,
    mutation,
    error,
) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    path.write_bytes(mutation(path))

    with pytest.raises(ExecutionJournalError, match=error):
        ExecutionJournal(path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", "gate1b.execution-journal.v999", "JOURNAL_SCHEMA_VERSION"),
        ("sequence", 2, "JOURNAL_SEQUENCE"),
        ("previous_digest", "f" * 64, "JOURNAL_PREVIOUS_DIGEST"),
    ],
)
def test_wrong_version_sequence_and_previous_digest_fail_closed(
    tmp_path,
    field,
    value,
    error,
) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    records[0][field] = value
    _set_digest(records[0])
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match=error):
        ExecutionJournal(path)


def test_wrong_digest_fails_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    records[0]["digest"] = "f" * 64
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_DIGEST"):
        ExecutionJournal(path)


def test_noncanonical_json_fails_closed_even_with_valid_digest(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    record = _raw_records(path)[0]
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")

    with pytest.raises(ExecutionJournalError, match="JOURNAL_NONCANONICAL"):
        ExecutionJournal(path)


def test_arbitrary_payload_and_credential_field_fail_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    event = records[0]["event"]
    assert isinstance(event, dict)
    event["payload"] = {"api_key": "must-not-be-journaled"}
    _set_digest(records[0])
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_EVENT_FIELDS"):
        ExecutionJournal(path)


def test_attempt_is_immutable_and_identity_is_deterministic() -> None:
    first = _attempt()
    second = _attempt()

    assert first == second
    assert first.attempt_id == second.attempt_id
    with pytest.raises(FrozenInstanceError):
        first.retry_index = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kind", "expected_client_id", "expected_key_kind"),
    [
        (
            MutationKind.CREATE,
            build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.PROBE_CLIENT_ID,
        ),
        (
            MutationKind.CANCEL,
            build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.PROBE_TERMINAL_STATE,
        ),
        (
            MutationKind.EMERGENCY_CLOSE,
            build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID,
        ),
    ],
)
def test_attempt_has_kind_correct_deterministic_reconciliation_key(
    kind,
    expected_client_id,
    expected_key_kind,
) -> None:
    attempt = _attempt(kind)

    assert attempt.client_id == expected_client_id
    assert attempt.reconciliation_key == ReconciliationKey(
        kind=expected_key_kind,
        client_id=expected_client_id,
    )


def test_mutation_transport_retry_is_always_zero() -> None:
    with pytest.raises(ExecutionJournalError, match="MUTATION_RETRY_FORBIDDEN"):
        MutationAttempt.build(
            kind=MutationKind.CREATE,
            generation=1,
            retry_index=1,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("reservation"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
        )


def test_forged_attempt_identity_or_reconciliation_key_is_rejected() -> None:
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_IDENTITY_MISMATCH"):
        replace(attempt, attempt_id="f" * 64)
    with pytest.raises(ExecutionJournalError, match="RECONCILIATION_KEY_MISMATCH"):
        replace(
            attempt,
            reconciliation_key=ReconciliationKey(
                ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID,
                attempt.client_id,
            ),
        )


def test_cancel_requires_fresh_open_proof_and_non_cancel_rejects_it() -> None:
    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_PROOF_REQUIRED"):
        MutationAttempt.build(
            kind=MutationKind.CANCEL,
            generation=1,
            retry_index=0,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("cancel"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
        )
    with pytest.raises(ExecutionJournalError, match="UNEXPECTED_OPEN_PROOF"):
        MutationAttempt.build(
            kind=MutationKind.CREATE,
            generation=1,
            retry_index=0,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("create"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
            fresh_open_proof_sha256=_sha("proof"),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 0},
        {"deadline_ns": 0},
        {"reservation_sha256": "bad"},
        {"authorization_id": "bad"},
        {"intent_sha256": "bad"},
    ],
)
def test_attempt_rejects_invalid_sanitized_fields(changes) -> None:
    arguments = {
        "kind": MutationKind.CREATE,
        "generation": 1,
        "retry_index": 0,
        "deadline_ns": DEADLINE_NS,
        "reservation_sha256": _sha("reservation"),
        "authorization_id": AUTHORIZATION_ID,
        "intent_sha256": INTENT_SHA256,
        "runtime_commit": RUNTIME_COMMIT,
        "session_nonce": SESSION_NONCE,
    }
    arguments.update(changes)

    with pytest.raises(ExecutionJournalError, match="INVALID_ATTEMPT"):
        MutationAttempt.build(**arguments)


def test_generation_admission_is_exact_monotonic_and_requires_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")

    with pytest.raises(ExecutionJournalError, match="GENERATION_SEQUENCE"):
        _admit(journal, 2, GenerationCapability.PRIMARY)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    with pytest.raises(ExecutionJournalError, match="GENERATION_ACTIVE"):
        _admit(journal, 2, GenerationCapability.RECOVERY)
    with pytest.raises(ExecutionJournalError, match="REAP_GENERATION_MISMATCH"):
        journal.reap_generation(_reap_receipt(2))
    _reap(journal, 1)
    with pytest.raises(ExecutionJournalError, match="PRIMARY_ONLY_FIRST_GENERATION"):
        _admit(journal, 2, GenerationCapability.PRIMARY)
    _admit(journal, 2, GenerationCapability.RECOVERY)


def test_generation_admission_durably_binds_process_identity(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    admission = _admission(1)

    journal.admit_generation(admission, GenerationCapability.PRIMARY)

    event = _raw_records(path)[-1]["event"]
    assert event == {
        "type": "GENERATION_ADMITTED",
        "generation": 1,
        "capability": GenerationCapability.PRIMARY.value,
        "process_identity_sha256": admission.process_identity_sha256,
    }


def test_reap_requires_typed_matching_local_quiescence_receipt(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)

    with pytest.raises(ExecutionJournalError, match="INVALID_REAP_RECEIPT"):
        journal.reap_generation(1)  # type: ignore[arg-type]
    with pytest.raises(ExecutionJournalError, match="REAP_IDENTITY_MISMATCH"):
        journal.reap_generation(_reap_receipt(1, process_identity_sha256=_identity_sha(99)))
    with pytest.raises(ExecutionJournalError, match="LOCAL_QUIESCENCE_REQUIRED"):
        journal.reap_generation(_reap_receipt(1, local_process_quiesced=False))
    with pytest.raises(ExecutionJournalError, match="VENUE_ABSENCE_MUST_REMAIN_UNPROVEN"):
        journal.reap_generation(_reap_receipt(1, venue_mutation_absent_proven=True))

    admission_record = journal.records()[1]
    receipt = _reap_receipt(
        1,
        admission_record_sequence=admission_record.sequence,
        admission_record_digest=admission_record.digest,
    )
    journal.reap_generation(receipt)
    assert _raw_records(journal.path)[-1]["event"] == {
        "type": "GENERATION_REAPED",
        "generation": 1,
        "process_identity_sha256": receipt.process_identity_sha256,
        "admission_record_sequence": admission_record.sequence,
        "admission_record_digest": admission_record.digest,
        "returncode": -9,
        "signal": 9,
        "local_process_quiesced": True,
        "venue_mutation_absent_proven": False,
    }


@pytest.mark.parametrize("frontier_after_go", [False, True])
def test_attempt_cannot_resolve_without_matching_reap_receipt(tmp_path, frontier_after_go) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    if frontier_after_go:
        journal.record_go(attempt.attempt_id)

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.TIMEOUT)


def test_primary_capability_cannot_be_reintroduced_after_first_generation(tmp_path) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    assert journal.frontier(original.attempt_id) is FrontierState.UNKNOWN

    with pytest.raises(ExecutionJournalError, match="PRIMARY_ONLY_FIRST_GENERATION"):
        _admit(journal, 2, GenerationCapability.PRIMARY)

    _admit(journal, 2, GenerationCapability.RECOVERY)


def test_recovery_generation_cannot_be_first_or_precede_prior_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")

    with pytest.raises(ExecutionJournalError, match="RECOVERY_REQUIRES_REAP"):
        _admit(journal, 1, GenerationCapability.RECOVERY)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    with pytest.raises(ExecutionJournalError, match="GENERATION_ACTIVE"):
        _admit(journal, 2, GenerationCapability.RECOVERY)


def test_prepare_requires_exact_active_generation_and_unique_attempt(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_GENERATION_NOT_ACTIVE"):
        _prepare(journal, attempt)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    _prepare(journal, attempt)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_ALREADY_EXISTS"):
        _prepare(journal, attempt)
    _reap(journal, 1)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_ALREADY_EXISTS"):
        _prepare(journal, _attempt(reservation="another"))


def test_go_requires_durable_prepared_and_cannot_repeat_or_follow_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_NOT_FOUND"):
        journal.record_go(attempt.attempt_id)
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    with pytest.raises(ExecutionJournalError, match="GO_REQUIRES_PREPARED"):
        journal.record_go(attempt.attempt_id)
    _reap(journal, 1)
    with pytest.raises(ExecutionJournalError, match="GO_REQUIRES_PREPARED"):
        journal.record_go(attempt.attempt_id)


def test_confirmation_requires_go_and_is_terminal(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)

    with pytest.raises(ExecutionJournalError, match="CONFIRMATION_REQUIRES_GO"):
        journal.record_confirmed(attempt.attempt_id, _sha("result"))
    journal.record_go(attempt.attempt_id)
    journal.record_confirmed(attempt.attempt_id, _sha("result"))
    assert journal.frontier(attempt.attempt_id) is FrontierState.CONFIRMED
    with pytest.raises(ExecutionJournalError, match="CONFIRMATION_REQUIRES_GO"):
        journal.record_confirmed(attempt.attempt_id, _sha("result-2"))


def test_prepared_without_go_becomes_not_dispatched_only_after_exact_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.EOF)
    assert journal.frontier(attempt.attempt_id) is FrontierState.PREPARED
    _reap(journal, 1)
    assert (
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.EOF)
        is FrontierState.NOT_DISPATCHED
    )


@pytest.mark.parametrize(
    "boundary_result",
    [
        BoundaryResult.ABSENT,
        BoundaryResult.CORRUPT,
        BoundaryResult.EOF,
        BoundaryResult.TIMEOUT,
        BoundaryResult.KILLED,
        BoundaryResult.PARTIAL_WRITE,
        BoundaryResult.RESPONSE_LOSS,
        BoundaryResult.DECODE_FAILURE,
        BoundaryResult.RESULT_DURABILITY_FAILURE,
    ],
)
def test_go_without_result_becomes_unknown_only_after_reap(tmp_path, boundary_result) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        journal.resolve_after_reap(attempt.attempt_id, boundary_result)
    _reap(journal, 1)
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    assert journal.resolve_after_reap(attempt.attempt_id, boundary_result) is FrontierState.UNKNOWN


@pytest.mark.parametrize(
    "boundary_result",
    [
        BoundaryResult.TIMEOUT,
        BoundaryResult.KILLED,
        BoundaryResult.PARTIAL_WRITE,
        BoundaryResult.RESPONSE_LOSS,
        BoundaryResult.DECODE_FAILURE,
        BoundaryResult.RESULT_DURABILITY_FAILURE,
    ],
)
def test_every_explicit_post_go_boundary_failure_is_unknown(tmp_path, boundary_result) -> None:
    journal = ExecutionJournal(tmp_path / f"{boundary_result.value}.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt(reservation=boundary_result.value)
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)
    _reap(journal, 1)

    assert journal.resolve_after_reap(attempt.attempt_id, boundary_result) is FrontierState.UNKNOWN


def test_reap_never_itself_implies_venue_non_dispatch(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)

    _reap(journal, 1)

    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


def test_forged_not_dispatched_after_go_fails_closed_on_replay(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)
    _reap(journal, 1)
    _append_forged_event(
        path,
        {
            "type": "ATTEMPT_RESOLVED",
            "attempt_id": attempt.attempt_id,
            "generation": 1,
            "state": FrontierState.NOT_DISPATCHED.value,
            "boundary_result": BoundaryResult.ABSENT.value,
        },
    )

    with pytest.raises(ExecutionJournalError, match="NOT_DISPATCHED_REQUIRES_NO_GO"):
        ExecutionJournal(path)


def test_forged_unknown_before_reap_fails_closed_on_replay(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    journal.record_go(attempt.attempt_id)
    _append_forged_event(
        path,
        {
            "type": "ATTEMPT_RESOLVED",
            "attempt_id": attempt.attempt_id,
            "generation": 1,
            "state": FrontierState.UNKNOWN.value,
            "boundary_result": BoundaryResult.CORRUPT.value,
        },
    )

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        ExecutionJournal(path)


def test_failed_go_write_leaves_only_durable_prepared(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)

    def failed_write(_fd: int, _data: bytes) -> int:
        raise OSError("injected write failure")

    monkeypatch.setattr(journal_module.os, "write", failed_write)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_APPEND_FAILED"):
        journal.record_go(attempt.attempt_id)
    monkeypatch.undo()

    reopened = ExecutionJournal(path)
    assert reopened.frontier(attempt.attempt_id) is FrontierState.PREPARED
    _reap(reopened, 1)
    assert (
        reopened.resolve_after_reap(attempt.attempt_id, BoundaryResult.ABSENT)
        is FrontierState.NOT_DISPATCHED
    )


def test_partial_append_then_write_failure_is_detected_as_truncation(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd: int, data: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, data[: len(data) // 2])
        raise OSError("injected partial write failure")

    monkeypatch.setattr(journal_module.os, "write", partial_then_fail)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_APPEND_FAILED"):
        _prepare(journal, _attempt())
    monkeypatch.undo()

    with pytest.raises(ExecutionJournalError, match="JOURNAL_TRUNCATED"):
        ExecutionJournal(path)


def test_fsync_failure_after_go_is_conservatively_recovered_as_go(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)
    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISREG(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_once)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_FSYNC_FAILED"):
        journal.record_go(attempt.attempt_id)
    monkeypatch.undo()

    reopened = ExecutionJournal(path)
    assert reopened.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        (MutationKind.CREATE, RecoveryMode.QUERY_PROBE_CLIENT_ID),
        (MutationKind.CANCEL, RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL),
        (
            MutationKind.EMERGENCY_CLOSE,
            RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
        ),
    ],
)
def test_unknown_has_kind_specific_recovery_directive(tmp_path, kind, mode) -> None:
    journal, attempt = _unknown_attempt(tmp_path, kind)

    directive = journal.recovery_directive(attempt.attempt_id)

    assert directive.mode is mode
    assert directive.query_client_id == attempt.client_id
    assert directive.allows_post_create is False
    assert directive.allows_blind_retry is False
    assert directive.queries_terminal_state is (kind is MutationKind.CANCEL)
    cleanup_cancel_allowed = kind in {MutationKind.CREATE, MutationKind.CANCEL}
    assert directive.requires_fresh_open_proof is cleanup_cancel_allowed
    assert directive.allows_conditional_cleanup_cancel is cleanup_cancel_allowed
    assert directive.requires_fresh_position_state is (kind is MutationKind.EMERGENCY_CLOSE)
    assert directive.requires_fresh_order_state is (kind is MutationKind.EMERGENCY_CLOSE)
    assert directive.requires_fresh_trade_state is (kind is MutationKind.EMERGENCY_CLOSE)
    assert directive.allows_first_owned_fill_cleanup_close is cleanup_cancel_allowed
    assert directive.requires_owned_fill_proof_for_close is cleanup_cancel_allowed


def test_recovery_directive_requires_unknown(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    _prepare(journal, attempt)

    with pytest.raises(ExecutionJournalError, match="RECOVERY_REQUIRES_UNKNOWN"):
        journal.recovery_directive(attempt.attempt_id)


def test_recovery_capability_rejects_create_and_blind_close(tmp_path) -> None:
    journal, _attempt_one = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)

    with pytest.raises(ExecutionJournalError, match="RECOVERY_MUTATION_FORBIDDEN"):
        _prepare(journal, _attempt(MutationKind.CREATE, generation=2, reservation="create-2"))
    with pytest.raises(ExecutionJournalError, match="MUTATION_RESERVATION_REQUIRED"):
        _prepare(
            journal, _attempt(MutationKind.EMERGENCY_CLOSE, generation=2, reservation="close-2")
        )


def test_unknown_cancel_allows_only_new_conditional_attempt_after_fresh_open_proof(
    tmp_path,
) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CANCEL)
    _admit(journal, 2, GenerationCapability.RECOVERY)

    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_OBSERVATION_REQUIRED"):
        journal.new_conditional_cleanup_cancel(
            source_attempt_id=original.attempt_id,
            observation_sha256=_sha("arbitrary-not-recorded"),
            deadline_ns=DEADLINE_NS + 2,
            reservation_sha256=_sha("conditional-cancel"),
        )
    observation = _open_observation(
        journal,
        original,
        generation=2,
        status=ReconciledOrderStatus.PARTIALLY_FILLED,
    )
    exact_second = _reserved_noncreate(
        journal,
        label="conditional-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    second = journal.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=exact_second.reservation_sha256,
    )
    assert second == exact_second
    assert second.attempt_id != original.attempt_id
    assert second.retry_index == 0
    assert second.recovery_of_attempt_id == original.attempt_id
    assert second.authorization_id == original.authorization_id
    assert second.intent_sha256 == original.intent_sha256
    assert second.runtime_commit == original.runtime_commit
    assert second.session_nonce == original.session_nonce
    assert second.client_id == original.client_id
    assert second.fresh_open_proof_sha256 == observation.observation_sha256
    _prepare(journal, second)
    assert journal.frontier(second.attempt_id) is FrontierState.PREPARED


def test_one_open_observation_cannot_authorize_multiple_cleanup_attempts(tmp_path) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(journal, original, generation=2)
    exact_first = _reserved_noncreate(
        journal,
        label="first-cleanup",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    first = journal.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=exact_first.reservation_sha256,
    )
    assert first == exact_first
    _prepare(journal, first)
    journal.record_go(first.attempt_id)
    journal.record_confirmed(first.attempt_id, _sha("first-cleanup-result"))
    exact_second = _reserved_noncreate(
        journal,
        label="second-cleanup",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    second = journal.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 3,
        reservation_sha256=exact_second.reservation_sha256,
    )

    with pytest.raises(
        ExecutionJournalError,
        match="FRESH_OPEN_OBSERVATION_ALREADY_CONSUMED",
    ):
        _prepare(journal, second)


def test_unknown_create_allows_new_cleanup_cancel_after_fresh_open_proof(tmp_path) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(journal, original, generation=2)

    exact_cleanup = _reserved_noncreate(
        journal,
        label="create-unknown-cleanup",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    cleanup = journal.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=exact_cleanup.reservation_sha256,
    )
    assert cleanup == exact_cleanup

    assert cleanup.kind is MutationKind.CANCEL
    assert cleanup.recovery_of_attempt_id == original.attempt_id
    assert cleanup.retry_index == 0
    _prepare(journal, cleanup)


def test_confirmed_create_allows_cleanup_cancel_after_reap_and_fresh_open_proof(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    original = _attempt(MutationKind.CREATE)
    _prepare(journal, original)
    journal.record_go(original.attempt_id)
    journal.record_confirmed(original.attempt_id, _sha("create-confirmed"))
    _reap(journal, 1)
    journal.recovery_directive(original.attempt_id)
    with pytest.raises(ExecutionJournalError, match="PRIMARY_ONLY_FIRST_GENERATION"):
        _admit(journal, 2, GenerationCapability.PRIMARY)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(journal, original, generation=2)

    exact_cleanup = _reserved_noncreate(
        journal,
        label="confirmed-create-cleanup",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    cleanup = journal.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=exact_cleanup.reservation_sha256,
    )
    assert cleanup == exact_cleanup

    _prepare(journal, cleanup)
    assert journal.frontier(cleanup.attempt_id) is FrontierState.PREPARED


def test_fresh_open_observation_is_typed_durable_and_source_bound(tmp_path) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(
        journal,
        original,
        generation=2,
        reservation="source-read-reservation",
    )

    event = _raw_records(journal.path)[-1]["event"]
    assert event == {
        "type": "RECONCILIATION_OBSERVED",
        "source_attempt_id": original.attempt_id,
        "source_authorization_id": original.authorization_id,
        "source_client_id": original.client_id,
        "generation": 2,
        "order_status": ReconciledOrderStatus.NEW.value,
        "read_reservation_sha256": observation.read_reservation_sha256,
        "read_result_proof_sha256": observation.read_result_proof_sha256,
        "read_result_record_sequence": observation.read_result_record_sequence,
        "read_result_record_digest": observation.read_result_record_digest,
        "observation_sha256": observation.observation_sha256,
    }

    reopened = ExecutionJournal(journal.path)
    exact_cleanup = _reserved_noncreate(
        reopened,
        label="cleanup-after-reopen",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    cleanup = reopened.new_conditional_cleanup_cancel(
        source_attempt_id=original.attempt_id,
        observation_sha256=observation.observation_sha256,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=exact_cleanup.reservation_sha256,
    )
    assert cleanup == exact_cleanup
    _prepare(reopened, cleanup)


@pytest.mark.parametrize(
    "observation_changes",
    [
        {"source_authorization_id": "g1b16-fedcba9876543210"},
        {"source_client_id": "different-client-id"},
        {"generation": 3},
    ],
)
def test_reconciliation_observation_rejects_wrong_source_or_generation(
    tmp_path, observation_changes
) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    valid = _open_observation(journal, original, generation=2)
    arguments = {
        "source_attempt_id": valid.source_attempt_id,
        "source_authorization_id": valid.source_authorization_id,
        "source_client_id": valid.source_client_id,
        "generation": valid.generation,
        "order_status": valid.order_status,
        "read_reservation_sha256": valid.read_reservation_sha256,
        "read_result_proof_sha256": valid.read_result_proof_sha256,
        "read_result_record_sequence": valid.read_result_record_sequence,
        "read_result_record_digest": valid.read_result_record_digest,
    }
    arguments.update(observation_changes)
    observation = ReconciliationObservation.build(**arguments)

    with pytest.raises(
        ExecutionJournalError,
        match=r"OBSERVATION_(SOURCE_LINEAGE_MISMATCH|GENERATION_NOT_ACTIVE)",
    ):
        journal.record_reconciliation_observation(observation)


def test_reconciliation_observation_rejects_non_open_order_status() -> None:
    with pytest.raises(ExecutionJournalError, match="INVALID_RECONCILIATION_OBSERVATION"):
        ReconciliationObservation.build(
            source_attempt_id="1" * 64,
            source_authorization_id=AUTHORIZATION_ID,
            source_client_id=build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            generation=2,
            order_status="FILLED",  # type: ignore[arg-type]
            read_reservation_sha256=_sha("fresh-read"),
            read_result_proof_sha256=_sha("fresh-result"),
            read_result_record_sequence=1,
            read_result_record_digest=_sha("fresh-result-record"),
        )


@pytest.mark.parametrize(
    "lineage_changes",
    [
        {"authorization_id": "g1b16-fedcba9876543210"},
        {"intent_sha256": "a" * 64},
        {"runtime_commit": "b" * 40},
        {"session_nonce": "c" * 16},
    ],
)
def test_recovery_cancel_cannot_change_source_lineage(tmp_path, lineage_changes) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(journal, original, generation=2)
    valid = _reserved_noncreate(
        journal,
        label="lineage-cleanup",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=original,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    arguments = {
        "kind": valid.kind,
        "generation": valid.generation,
        "retry_index": valid.retry_index,
        "deadline_ns": valid.deadline_ns,
        "reservation_sha256": valid.reservation_sha256,
        "authorization_id": valid.authorization_id,
        "intent_sha256": valid.intent_sha256,
        "runtime_commit": valid.runtime_commit,
        "session_nonce": valid.session_nonce,
        "fresh_open_proof_sha256": valid.fresh_open_proof_sha256,
        "recovery_of_attempt_id": valid.recovery_of_attempt_id,
    }
    arguments.update(lineage_changes)
    forged = MutationAttempt.build(**arguments)

    with pytest.raises(
        ExecutionJournalError,
        match=r"(MUTATION_RESERVATION_MISMATCH|RECOVERY_MUTATION_LINEAGE_MISMATCH)",
    ):
        _prepare(journal, forged)


def test_recovery_generation_rejects_cancel_not_derived_from_unknown(tmp_path) -> None:
    journal, source = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    observation = _open_observation(journal, source, generation=2)
    valid = _reserved_noncreate(
        journal,
        label="cancel-not-derived",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.RECOVERY_CONDITIONAL_CANCEL,
        generation=2,
        source=source,
        precondition_sha256=observation.observation_sha256,
        recovery=True,
    )
    forged = MutationAttempt.build(
        kind=valid.kind,
        generation=valid.generation,
        retry_index=valid.retry_index,
        deadline_ns=valid.deadline_ns,
        reservation_sha256=valid.reservation_sha256,
        authorization_id=valid.authorization_id,
        intent_sha256=valid.intent_sha256,
        runtime_commit=valid.runtime_commit,
        session_nonce=valid.session_nonce,
        fresh_open_proof_sha256=valid.fresh_open_proof_sha256,
        recovery_of_attempt_id=_sha("not-the-observed-source"),
    )

    with pytest.raises(ExecutionJournalError, match="RECOVERY_MUTATION_NOT_AUTHORIZED"):
        _prepare(journal, forged)


def test_prepare_rejects_orphan_mutation_reservation_digest(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    attempt = _attempt(reservation="unrecorded-request")

    with pytest.raises(ExecutionJournalError, match="MUTATION_RESERVATION_REQUIRED"):
        journal.prepare_attempt(attempt)


def test_durable_exact_mutation_reservation_is_consumed_by_prepare(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    authority, _intent = _ensure_test_request_chain(journal)
    reserved = _default_create_reserved_request()
    exact_record = journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 1,
        reserved_request=reserved,
    )
    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=DEADLINE_NS + 1,
        client_id=authority.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )
    record = journal.record_mutation_reservation(proof)
    attempt = _attempt()

    _prepare(journal, attempt)

    assert proof.request_sha256 == reserved.request_sha256
    assert proof.proof_sha256 != reserved.request_sha256
    assert exact_record.event.reserved_request == reserved
    assert record.digest in {item.digest for item in journal.records()}
    second = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS + 1,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_ALREADY_EXISTS"):
        _prepare(journal, second)


def test_generation_rejects_second_inflight_mutation(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    first = _reserved_create(journal, label="first-inflight")
    _prepare(journal, first)

    with pytest.raises(ExecutionJournalError, match="EXACT_REQUEST_ALREADY_EXISTS"):
        _reserved_create(journal, label="second-inflight")


def test_create_budget_is_one_even_after_first_is_confirmed(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    first = _reserved_create(journal, label="first-create")
    _prepare(journal, first)
    journal.record_go(first.attempt_id)
    journal.record_confirmed(first.attempt_id, _sha("first-create-result"))

    with pytest.raises(ExecutionJournalError, match="EXACT_REQUEST_ALREADY_EXISTS"):
        _reserved_create(journal, label="second-create")


def test_read_result_requires_exact_durable_read_prepared_record(tmp_path) -> None:
    reservation_type = getattr(journal_module, "ReadReservationProof", None)
    result_type = getattr(journal_module, "ReadResultProof", None)
    kind_type = getattr(journal_module, "ReadKind", None)
    purpose_type = getattr(journal_module, "ReadPurpose", None)
    outcome_type = getattr(journal_module, "ReadOutcome", None)
    assert reservation_type is not None
    assert result_type is not None
    assert kind_type is not None
    assert purpose_type is not None
    assert outcome_type is not None
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="read-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("read-source-result"))
    authority, intent = _ensure_test_request_chain(journal)
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    exact = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            post_create_read_requests=previous.post_create_read_requests + 1,
            last_elapsed_seconds=Decimal("13"),
        ),
        intent_sha256=intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=tuple(sorted(intent.query_parameters.items())),
        elapsed_seconds=Decimal("13"),
        retry_index=0,
    )
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=exact,
    )
    reservation = reservation_type.from_reserved_request(
        exact,
        read_kind=kind_type.ORDER,
        purpose=purpose_type.EVIDENCE,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=source.attempt_id,
        client_id=source.client_id,
        authorization_id=AUTHORIZATION_ID,
    )
    prepared = journal.record_read_prepared(reservation)
    result = result_type.build(
        request_sha256=reservation.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest=prepared.digest,
        generation=1,
        monotonic_sequence=exact.ledger.total_http_requests,
        read_kind=kind_type.ORDER,
        outcome=outcome_type.ORDER_NEW,
        result_sha256=_sha("sanitized-order-result"),
        observed_at_ns=DEADLINE_NS - 1,
    )

    forged = result_type.build(
        request_sha256=reservation.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest="f" * 64,
        generation=1,
        monotonic_sequence=exact.ledger.total_http_requests,
        read_kind=kind_type.ORDER,
        outcome=outcome_type.ORDER_NEW,
        result_sha256=_sha("forged-order-result"),
        observed_at_ns=DEADLINE_NS - 1,
    )
    with pytest.raises(ExecutionJournalError, match="READ_PREPARED_RECORD_MISMATCH"):
        journal.record_read_result(forged)
    journal.record_read_result(result)


def test_fresh_open_observation_is_derived_from_durable_read_result(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="source-create")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("source-create-result"))
    open_result = _record_read(
        journal,
        label="fresh-open",
        generation=1,
        sequence=2,
        kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        outcome=ReadOutcome.ORDER_NEW,
        source=source,
    )

    observation = journal.new_reconciliation_observation(
        source_attempt_id=source.attempt_id,
        read_result_proof_sha256=open_result.result_proof_sha256,
    )
    journal.record_reconciliation_observation(observation)

    assert observation.order_status is ReconciledOrderStatus.NEW
    terminal_result = _record_read(
        journal,
        label="terminal",
        generation=1,
        sequence=3,
        kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        outcome=ReadOutcome.ORDER_TERMINAL,
        source=source,
    )
    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_RESULT_REQUIRED"):
        journal.new_reconciliation_observation(
            source_attempt_id=source.attempt_id,
            read_result_proof_sha256=terminal_result.result_proof_sha256,
        )


def test_read_reservation_is_derived_from_exact_protocol_reserved_request() -> None:
    reserved = ReservedRequest(
        ledger=MutationLedger(
            total_http_requests=1,
            last_elapsed_seconds=Decimal("1"),
        ),
        intent_sha256=INTENT_SHA256,
        origin="https://demo-fapi.binance.com",
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=(("origClientOrderId", "probe-id"), ("symbol", "BTCUSDT")),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )

    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        generation=1,
        deadline_ns=DEADLINE_NS,
        source_attempt_id="a" * 64,
        client_id="probe-id",
        authorization_id=AUTHORIZATION_ID,
    )

    assert proof.request_sha256 == reserved.request_sha256
    assert proof.logical_request_sha256 == reserved.logical_request_sha256
    assert proof.monotonic_sequence == reserved.ledger.total_http_requests
    assert proof.parameters_sha256 == journal_module.reserved_request_parameters_sha256(reserved)
    assert proof.ledger_sha256 == journal_module.reserved_request_ledger_sha256(reserved)
    proof.validate_reserved_request(reserved)
    with pytest.raises(ExecutionJournalError, match="READ_RESERVED_REQUEST_MISMATCH"):
        proof.validate_reserved_request(
            replace(
                reserved,
                parameters=(("origClientOrderId", "other-id"), ("symbol", "BTCUSDT")),
            )
        )
    with pytest.raises(ExecutionJournalError, match="READ_RESERVED_CLIENT_ID_MISMATCH"):
        ReadReservationProof.from_reserved_request(
            replace(
                reserved,
                parameters=(
                    ("origClientOrderId", "other-id"),
                    ("symbol", "BTCUSDT"),
                ),
            ),
            read_kind=ReadKind.ORDER,
            purpose=ReadPurpose.ORDER_RECONCILIATION,
            generation=1,
            deadline_ns=DEADLINE_NS,
            source_attempt_id="a" * 64,
            client_id="probe-id",
            authorization_id=AUTHORIZATION_ID,
        )


def test_mutation_reservation_is_derived_from_exact_protocol_reserved_request() -> None:
    reserved = ReservedRequest(
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("1"),
        ),
        intent_sha256=INTENT_SHA256,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=(("newClientOrderId", "probe-id"), ("symbol", "BTCUSDT")),
        elapsed_seconds=Decimal("1"),
        retry_index=0,
    )

    proof = MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=DEADLINE_NS,
        client_id="probe-id",
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )

    assert proof.request_sha256 == reserved.request_sha256
    assert proof.logical_request_sha256 == reserved.logical_request_sha256
    assert proof.retry_index == 0
    assert proof.monotonic_sequence == reserved.ledger.total_http_requests
    assert proof.parameters_sha256 == journal_module.reserved_request_parameters_sha256(reserved)
    assert proof.ledger_sha256 == journal_module.reserved_request_ledger_sha256(reserved)


def test_owned_fill_close_proof_requires_five_exact_durable_read_results(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="owned-fill-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("owned-fill-create-result"))
    proof = _owned_fill_proof(journal, source, generation=1, label="owned-fill")

    assert proof.source_runtime_commit == source.runtime_commit
    assert proof.source_session_nonce == source.session_nonce
    assert proof.residual_quantity == "0.001"
    assert proof.owned_executed_quantity == "0.001"
    assert proof.open_remainder_quantity == "0"
    assert proof.other_activity_absent is True
    assert proof.market_close_proof_sha256
    assert proof.owned_position_proof_sha256
    references = (
        proof.order_result,
        proof.trade_result,
        proof.account_result,
        proof.symbol_filter_result,
        proof.mark_price_result,
    )
    records = journal.records()
    owned_events = [
        type(record.event).__name__
        for record in records
        if type(record.event).__name__ in {"_ReadPrepared", "_ReadResultRecorded"}
    ]
    assert owned_events == ["_ReadPrepared", "_ReadResultRecorded"] * 5
    durable_results = {
        record.event.proof.result_proof_sha256: record.event.proof
        for record in records
        if type(record.event).__name__ == "_ReadResultRecorded"
    }
    for reference in references:
        result = durable_results[reference.result_proof_sha256]
        assert reference.request_sha256 == result.request_sha256
        assert reference.transport_result_sha256 == result.result_sha256


def test_owned_fill_factory_rejects_unrelated_evidence_read_reservations(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="unrelated-evidence-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("unrelated-evidence-result"))

    with pytest.raises(
        ExecutionJournalError,
        match="OWNED_FILL_CLOSE_READ_BINDING_MISMATCH",
    ):
        _owned_fill_proof(
            journal,
            source,
            generation=1,
            label="unrelated-evidence",
            read_purpose=ReadPurpose.EVIDENCE,
            record_proof=False,
        )


def test_owned_fill_factory_rejects_transport_digest_not_durable_for_exact_read(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="forged-result-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("forged-result-create"))

    with pytest.raises(
        ExecutionJournalError,
        match="OWNED_FILL_TRANSPORT_RESULT_MISMATCH",
    ):
        _owned_fill_proof(
            journal,
            source,
            generation=1,
            label="forged-result",
            record_proof=False,
            recorded_result_digest_overrides={ReadKind.ORDER: _sha("arbitrary-confirmed")},
        )


def test_primary_cancel_requires_durable_fresh_open_result_and_consumes_it_once(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="cancel-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("cancel-source-result"))

    open_result = _record_read(
        journal,
        label="primary-cancel-open",
        generation=1,
        sequence=2,
        kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        outcome=ReadOutcome.ORDER_NEW,
        source=source,
    )
    observation = journal.new_reconciliation_observation(
        source_attempt_id=source.attempt_id,
        read_result_proof_sha256=open_result.result_proof_sha256,
    )
    journal.record_reconciliation_observation(observation)
    first = _reserved_noncreate(
        journal,
        label="first-primary-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=source,
        precondition_sha256=observation.observation_sha256,
    )
    _prepare(journal, first)
    journal.record_go(first.attempt_id)
    journal.record_confirmed(first.attempt_id, _sha("first-primary-cancel-result"))
    second = _reserved_noncreate(
        journal,
        label="reused-primary-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=source,
        precondition_sha256=observation.observation_sha256,
    )

    with pytest.raises(
        ExecutionJournalError,
        match="FRESH_OPEN_OBSERVATION_ALREADY_CONSUMED",
    ):
        _prepare(journal, second)


@pytest.mark.parametrize(
    "still_open_status",
    [ReconciledOrderStatus.NEW, ReconciledOrderStatus.PARTIALLY_FILLED],
)
def test_confirmed_primary_cancel_allows_second_cancel_only_after_fresh_still_open_proof(
    tmp_path,
    still_open_status: ReconciledOrderStatus,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    create = _reserved_create(journal, label="second-cancel-create")
    _prepare(journal, create)
    journal.record_go(create.attempt_id)
    journal.record_confirmed(create.attempt_id, _sha("second-cancel-create-result"))

    first_open = _open_observation(
        journal,
        create,
        generation=1,
        reservation="first-cancel-open",
    )
    first_cancel = _reserved_noncreate(
        journal,
        label="first-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=create,
        precondition_sha256=first_open.observation_sha256,
    )
    _prepare(journal, first_cancel)
    journal.record_go(first_cancel.attempt_id)
    journal.record_confirmed(first_cancel.attempt_id, _sha("first-cancel-result"))

    second_open = _open_observation(
        journal,
        first_cancel,
        generation=1,
        status=still_open_status,
        reservation="second-cancel-open",
    )
    second_cancel = _reserved_noncreate(
        journal,
        label="second-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=first_cancel,
        precondition_sha256=second_open.observation_sha256,
    )
    _prepare(journal, second_cancel)

    assert second_cancel.retry_index == 0
    assert second_cancel.recovery_of_attempt_id is None
    assert second_cancel.client_id == create.client_id
    assert second_cancel.fresh_open_proof_sha256 == second_open.observation_sha256
    assert journal.frontier(second_cancel.attempt_id) is FrontierState.PREPARED


def test_confirmed_primary_cancel_terminal_result_cannot_authorize_second_cancel(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    create = _reserved_create(journal, label="terminal-second-cancel-create")
    _prepare(journal, create)
    journal.record_go(create.attempt_id)
    journal.record_confirmed(create.attempt_id, _sha("terminal-second-cancel-create-result"))

    first_open = _open_observation(
        journal,
        create,
        generation=1,
        reservation="terminal-first-cancel-open",
    )
    first_cancel = _reserved_noncreate(
        journal,
        label="terminal-first-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=create,
        precondition_sha256=first_open.observation_sha256,
    )
    _prepare(journal, first_cancel)
    journal.record_go(first_cancel.attempt_id)
    journal.record_confirmed(first_cancel.attempt_id, _sha("terminal-first-cancel-result"))

    terminal_result = _record_read(
        journal,
        label="terminal-after-confirmed-cancel",
        generation=1,
        sequence=1,
        kind=ReadKind.ORDER,
        purpose=ReadPurpose.ORDER_RECONCILIATION,
        outcome=ReadOutcome.ORDER_TERMINAL,
        source=first_cancel,
    )
    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_RESULT_REQUIRED"):
        journal.new_reconciliation_observation(
            source_attempt_id=first_cancel.attempt_id,
            read_result_proof_sha256=terminal_result.result_proof_sha256,
        )


def test_second_cancel_fresh_open_proof_requires_exact_confirmed_cancel_lineage(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    create = _reserved_create(journal, label="lineage-second-cancel-create")
    _prepare(journal, create)
    journal.record_go(create.attempt_id)
    journal.record_confirmed(create.attempt_id, _sha("lineage-second-cancel-create-result"))

    first_open = _open_observation(
        journal,
        create,
        generation=1,
        reservation="lineage-first-cancel-open",
    )
    first_cancel = _reserved_noncreate(
        journal,
        label="lineage-first-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=create,
        precondition_sha256=first_open.observation_sha256,
    )
    _prepare(journal, first_cancel)
    journal.record_go(first_cancel.attempt_id)
    journal.record_confirmed(first_cancel.attempt_id, _sha("lineage-first-cancel-result"))

    second_open = _open_observation(
        journal,
        first_cancel,
        generation=1,
        reservation="lineage-second-cancel-open",
    )
    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_RESERVATION_OBSERVATION_MISMATCH",
    ):
        _reserved_noncreate(
            journal,
            label="lineage-second-cancel-wrong-source",
            kind=MutationKind.CANCEL,
            purpose=MutationPurpose.PRIMARY_CANCEL,
            generation=1,
            source=create,
            precondition_sha256=second_open.observation_sha256,
        )


def test_cancel_budget_allows_two_sequential_fresh_attempts_only(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    create = _reserved_create(journal, label="cancel-budget-source")
    _prepare(journal, create)
    journal.record_go(create.attempt_id)
    journal.record_confirmed(create.attempt_id, _sha("cancel-budget-source-result"))

    source = create
    for index in (1, 2):
        observation = _open_observation(
            journal,
            source,
            generation=1,
            reservation=f"cancel-budget-open-{index}",
        )
        cancel = _reserved_noncreate(
            journal,
            label=f"cancel-budget-{index}",
            kind=MutationKind.CANCEL,
            purpose=MutationPurpose.PRIMARY_CANCEL,
            generation=1,
            source=source,
            precondition_sha256=observation.observation_sha256,
        )
        _prepare(journal, cancel)
        journal.record_go(cancel.attempt_id)
        journal.record_confirmed(cancel.attempt_id, _sha(f"cancel-budget-result-{index}"))
        source = cancel

    third_observation = _open_observation(
        journal,
        source,
        generation=1,
        reservation="cancel-budget-open-3",
    )
    with pytest.raises(MutationProtocolError, match="INVALID_MUTATION_LEDGER"):
        _reserved_noncreate(
            journal,
            label="cancel-budget-3",
            kind=MutationKind.CANCEL,
            purpose=MutationPurpose.PRIMARY_CANCEL,
            generation=1,
            source=source,
            precondition_sha256=third_observation.observation_sha256,
        )


def test_primary_first_close_requires_owned_fill_proof_and_never_reposts(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="close-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("close-source-result"))
    proof = _owned_fill_proof(journal, source, generation=1, label="primary-close")
    first = _reserved_noncreate(
        journal,
        label="first-primary-close",
        kind=MutationKind.EMERGENCY_CLOSE,
        purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
        generation=1,
        source=source,
        precondition_sha256=proof.proof_sha256,
    )
    _prepare(journal, first)
    journal.record_go(first.attempt_id)
    journal.record_confirmed(first.attempt_id, _sha("first-primary-close-result"))

    second_proof = _owned_fill_proof(
        journal,
        source,
        generation=1,
        label="second-primary-close",
    )
    with pytest.raises(MutationProtocolError, match="INVALID_MUTATION_LEDGER"):
        _reserved_noncreate(
            journal,
            label="second-primary-close",
            kind=MutationKind.EMERGENCY_CLOSE,
            purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
            generation=1,
            source=source,
            precondition_sha256=second_proof.proof_sha256,
        )


def test_close_reservation_parameters_must_match_exact_owned_quantity(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="quantity-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("quantity-source-result"))
    proof = _owned_fill_proof(journal, source, generation=1, label="quantity-proof")

    with pytest.raises(
        ExecutionJournalError,
        match="EXACT_REQUEST_PROOF_MISMATCH",
    ):
        _reserved_noncreate(
            journal,
            label="wrong-close-quantity",
            kind=MutationKind.EMERGENCY_CLOSE,
            purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
            generation=1,
            source=source,
            precondition_sha256=proof.proof_sha256,
            parameters_sha256=journal_module.owned_close_parameters_sha256(
                quantity="0.002",
                client_id=build_emergency_client_order_id(
                    source.runtime_commit,
                    source.session_nonce,
                ),
            ),
        )


def test_recovery_allows_only_first_owned_fill_close_and_never_reposts_unknown_close(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="recovery-close-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    _reap(journal, 1)
    assert (
        journal.resolve_after_reap(source.attempt_id, BoundaryResult.EOF) is FrontierState.UNKNOWN
    )
    _admit(journal, 2, GenerationCapability.RECOVERY)
    proof = _owned_fill_proof(journal, source, generation=2, label="recovery-close")
    close = _reserved_noncreate(
        journal,
        label="first-recovery-close",
        kind=MutationKind.EMERGENCY_CLOSE,
        purpose=MutationPurpose.RECOVERY_OWNED_FILL_CLOSE,
        generation=2,
        source=source,
        precondition_sha256=proof.proof_sha256,
        recovery=True,
    )
    _prepare(journal, close)
    journal.record_go(close.attempt_id)
    _reap(journal, 2)
    assert (
        journal.resolve_after_reap(close.attempt_id, BoundaryResult.TIMEOUT)
        is FrontierState.UNKNOWN
    )
    _admit(journal, 3, GenerationCapability.RECOVERY)
    second_proof = _owned_fill_proof(
        journal,
        source,
        generation=3,
        label="recovery-close-repost",
    )
    with pytest.raises(MutationProtocolError, match="INVALID_MUTATION_LEDGER"):
        _reserved_noncreate(
            journal,
            label="second-recovery-close",
            kind=MutationKind.EMERGENCY_CLOSE,
            purpose=MutationPurpose.RECOVERY_OWNED_FILL_CLOSE,
            generation=3,
            source=source,
            precondition_sha256=second_proof.proof_sha256,
            recovery=True,
        )


def test_reap_receipt_is_bound_to_exact_journal_admission_record(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    admission_record = journal.admit_generation(
        _admission(1),
        GenerationCapability.PRIMARY,
    )
    forged = ProcessReapReceipt(
        generation=1,
        process_identity_sha256=_identity_sha(1),
        admission_record_sequence=admission_record.sequence,
        admission_record_digest="f" * 64,
        returncode=-9,
        signal=9,
        local_process_quiesced=True,
        venue_mutation_absent_proven=False,
    )

    with pytest.raises(ExecutionJournalError, match="REAP_ADMISSION_RECORD_MISMATCH"):
        journal.reap_generation(forged)

    journal.reap_generation(
        replace(
            forged,
            admission_record_digest=admission_record.digest,
        )
    )


def test_exact_staged_then_reaped_process_generation_can_fill_execution_wal_gap(
    tmp_path,
) -> None:
    proof_type = getattr(journal_module, "StagedGenerationRecoveryProof", None)
    assert proof_type is not None
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    proof = proof_type.build(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        process_identity_sha256=_identity_sha(1),
        process_staged_record_sequence=2,
        process_staged_record_digest=_sha("process-staged-1"),
        process_reaped_record_sequence=3,
        process_reaped_record_digest=_sha("process-reaped-1"),
        reap_attestation_sha256=_sha("process-reap-attestation-1"),
        returncode=-9,
        signal=9,
        local_process_quiesced=True,
        venue_mutation_absent_proven=False,
    )

    record = journal.reconcile_staged_generation(proof)

    assert record.event.proof == proof
    _admit(journal, 2, GenerationCapability.RECOVERY)


def test_mutation_proof_validates_exact_reserved_request_and_attempt_dispatch_binding() -> None:
    client_id = build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE)
    reserved = ReservedRequest(
        ledger=MutationLedger(
            total_http_requests=1,
            create_requests=1,
            stage=RequestStage.CREATE_ATTEMPTED,
            last_elapsed_seconds=Decimal("1"),
        ),
        intent_sha256=INTENT_SHA256,
        origin="https://demo-fapi.binance.com",
        method="POST",
        path="/fapi/v1/order",
        purpose=RequestPurpose.CREATE,
        parameters=(("newClientOrderId", client_id), ("symbol", "BTCUSDT")),
        elapsed_seconds=Decimal("1"),
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
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )

    proof.validate_dispatch_binding(reserved, attempt)
    forged_reserved = replace(
        reserved,
        parameters=(("newClientOrderId", client_id), ("symbol", "ETHUSDT")),
    )
    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_RESERVED_REQUEST_MISMATCH",
    ):
        proof.validate_dispatch_binding(forged_reserved, attempt)
    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_DISPATCH_BINDING_MISMATCH",
    ):
        proof.validate_dispatch_binding(
            reserved,
            MutationAttempt.build(
                kind=MutationKind.CREATE,
                generation=1,
                retry_index=0,
                deadline_ns=DEADLINE_NS + 1,
                reservation_sha256=reserved.request_sha256,
                authorization_id=AUTHORIZATION_ID,
                intent_sha256=INTENT_SHA256,
                runtime_commit=RUNTIME_COMMIT,
                session_nonce=SESSION_NONCE,
            ),
        )
    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_RESERVED_CLIENT_ID_MISMATCH",
    ):
        MutationReservationProof.from_reserved_request(
            replace(
                reserved,
                parameters=(
                    ("newClientOrderId", "different-id"),
                    ("symbol", "BTCUSDT"),
                ),
            ),
            purpose=MutationPurpose.PRIMARY_CREATE,
            generation=1,
            deadline_ns=DEADLINE_NS,
            client_id=client_id,
            authorization_id=AUTHORIZATION_ID,
            source_attempt_id=None,
            precondition_sha256=None,
        )


def _persist_request_ledger_intent(tmp_path: Path):
    from global_quant.gate1b.durable_intent import persist_intent

    root = tmp_path / "request-ledger-intent"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return persist_intent(root / "intent.json", _test_durable_intent(persisted=False))


def _establish_test_request_chain(journal: ExecutionJournal):
    from global_quant.gate1b.durable_intent import persist_intent

    authority = journal_module.SessionAuthority.build(
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
            result_sha256=_sha(f"{journal.path.name}-pre-intent-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    root = journal.path.with_name(f".{journal.path.name}.intent")
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    persisted = persist_intent(
        root / "intent.json",
        _test_durable_intent(persisted=False),
    )
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    return authority, persisted


def _ensure_test_request_chain(journal: ExecutionJournal):
    authority = journal_module.SessionAuthority.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    if not any(
        type(record.event).__name__ == "_SessionAuthorityEstablished"
        for record in journal.records()
    ):
        established, persisted = _establish_test_request_chain(journal)
        return established, persisted.intent
    return authority, _test_durable_intent(persisted=True)


def _request_authority_sha256(
    journal: ExecutionJournal,
    *,
    generation: int,
    source: MutationAttempt | None,
) -> str:
    primary, _intent = _ensure_test_request_chain(journal)
    if generation == primary.generation:
        return primary.authority_sha256
    if source is None:
        raise AssertionError("recovery exact requests require a source attempt")
    for record in journal.records():
        if type(record.event).__name__ != "_RecoverySessionAuthorityIssued":
            continue
        authority = record.event.authority
        if authority.generation == generation and authority.source_attempt_id == source.attempt_id:
            return authority.authority_sha256
    issued = journal.issue_recovery_session_authority(
        primary_authority_sha256=primary.authority_sha256,
        source_attempt_id=source.attempt_id,
    )
    return issued.event.authority.authority_sha256


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


def _bound_request_ledger(
    tmp_path: Path,
    *,
    name: str,
) -> tuple[ExecutionJournal, object, object]:
    authority_type = journal_module.SessionAuthority
    journal = ExecutionJournal(tmp_path / f"{name}.jsonl")
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    authority = authority_type.build(
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
            result_sha256=_sha(f"{name}-result-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    persisted = _persist_request_ledger_intent(tmp_path)
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    return journal, authority, persisted


def _exact_create_request(
    journal: ExecutionJournal,
    authority: object,
    persisted: object,
) -> ReservedRequest:
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    return ReservedRequest(
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


def _exact_create_proof(
    reserved: ReservedRequest,
    authority: object,
) -> MutationReservationProof:
    return MutationReservationProof.from_reserved_request(
        reserved,
        purpose=MutationPurpose.PRIMARY_CREATE,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        client_id=authority.client_id,
        authorization_id=AUTHORIZATION_ID,
        source_attempt_id=None,
        precondition_sha256=None,
    )


def test_pre_intent_reads_use_nonsecret_session_authority_not_fake_intent(tmp_path) -> None:
    authority_type = getattr(journal_module, "SessionAuthority", None)
    assert authority_type is not None, "SessionAuthority must exist"
    journal = ExecutionJournal(tmp_path / "pre-intent.jsonl")
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    authority = authority_type.build(
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
        reservation = prepared.reservation
        assert not hasattr(reservation, "intent_sha256")
        assert reservation.session_authority_sha256 == authority.authority_sha256
        assert reservation.ledger.total_http_requests == sequence
        journal.record_pre_intent_read_result(
            reservation_sha256=reservation.reservation_sha256,
            result_sha256=_sha(f"pre-intent-result-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )

    snapshot = ExecutionJournal(journal.path).request_ledger_snapshot(authority.authority_sha256)
    assert snapshot.bound_intent_sha256 is None
    assert snapshot.last_ledger.total_http_requests == 11
    assert snapshot.last_ledger.stage is RequestStage.CREATE_READY
    assert snapshot.pending_requests == ()
    assert snapshot.completed_pre_intent_paths == tuple(
        path for path, _parameters in _pre_intent_reads(authority.client_id)
    )


def test_persisted_intent_one_way_binding_continues_exact_reserved_request_ledger(
    tmp_path,
) -> None:
    authority_type = getattr(journal_module, "SessionAuthority", None)
    assert authority_type is not None, "SessionAuthority must exist"
    journal = ExecutionJournal(tmp_path / "bound-ledger.jsonl")
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    authority = authority_type.build(
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
            result_sha256=_sha(f"bound-result-{sequence}"),
            observed_at_ns=DEADLINE_NS,
        )
    persisted = _persist_request_ledger_intent(tmp_path)
    journal.bind_persisted_intent(authority.authority_sha256, persisted)
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    reserved = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=12,
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
    record = journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )

    reopened = ExecutionJournal(journal.path)
    snapshot = reopened.request_ledger_snapshot(authority.authority_sha256)
    assert snapshot.bound_intent_sha256 == persisted.intent.intent_sha256
    assert snapshot.last_ledger == reserved.ledger
    assert snapshot.last_ledger.total_http_requests == 12
    assert snapshot.last_ledger.create_requests == 1
    assert snapshot.pending_requests == (reserved,)
    assert record.event.reserved_request == reserved
    raw_event = json.loads(journal.path.read_text(encoding="ascii").splitlines()[-1])["event"]
    assert raw_event["reserved_request"]["parameters"] == dict(reserved.parameters)
    assert raw_event["reserved_request"]["ledger"]["total_http_requests"] == 12

    with pytest.raises(ExecutionJournalError, match="INTENT_CHAIN_ALREADY_BOUND"):
        reopened.bind_persisted_intent(authority.authority_sha256, persisted)


@pytest.mark.parametrize(
    "failure_name",
    [
        "IO_AMBIGUOUS",
        "RESPONSE_INVALID",
        "NETWORK_GUARD",
        "TRANSPORT_REJECTED",
        "EXECUTOR_FAILURE",
        "RESULT_INVALID",
    ],
)
def test_pre_intent_read_failure_is_durable_and_retry_budget_is_global(
    tmp_path,
    failure_name: str,
) -> None:
    authority_type = getattr(journal_module, "SessionAuthority", None)
    failure_type = getattr(journal_module, "ReadFailureKind", None)
    assert authority_type is not None and failure_type is not None
    journal = ExecutionJournal(tmp_path / "read-failure.jsonl")
    _admit(
        journal,
        1,
        GenerationCapability.PRIMARY,
        bind_request_chain=False,
    )
    authority = authority_type.build(
        authorization_id=AUTHORIZATION_ID,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        generation=1,
    )
    journal.establish_session_authority(authority)
    first = journal.reserve_pre_intent_read(
        authority_sha256=authority.authority_sha256,
        path="/fapi/v1/time",
        parameters={},
        elapsed_seconds=Decimal("1"),
        deadline_ns=DEADLINE_NS + 1,
        retry_index=0,
    )
    journal.record_pre_intent_read_failure(
        reservation_sha256=first.reservation.reservation_sha256,
        failure=failure_type[failure_name],
        io_may_have_occurred=True,
        observed_at_ns=DEADLINE_NS,
    )
    reopened = ExecutionJournal(journal.path)
    failed = reopened.request_ledger_snapshot(authority.authority_sha256)
    persisted_failure = reopened.records()[-1].event.failure
    assert persisted_failure.failure is failure_type[failure_name]
    assert persisted_failure.io_may_have_occurred is True
    assert failed.retryable_logical_request_sha256 == (first.reservation.logical_request_sha256)
    assert failed.last_ledger.retryable_read_sha256 == (first.reservation.logical_request_sha256)

    retry = journal.reserve_pre_intent_read(
        authority_sha256=authority.authority_sha256,
        path="/fapi/v1/time",
        parameters={},
        elapsed_seconds=Decimal("2"),
        deadline_ns=DEADLINE_NS + 2,
        retry_index=1,
    )
    assert retry.reservation.ledger.total_http_requests == 2
    assert retry.reservation.ledger.read_retry_requests == 1
    journal.record_pre_intent_read_result(
        reservation_sha256=retry.reservation.reservation_sha256,
        result_sha256=_sha("retry-result"),
        observed_at_ns=DEADLINE_NS,
    )
    second = journal.reserve_pre_intent_read(
        authority_sha256=authority.authority_sha256,
        path="/fapi/v1/positionSide/dual",
        parameters={"recvWindow": str(RECEIVE_WINDOW_MS)},
        elapsed_seconds=Decimal("3"),
        deadline_ns=DEADLINE_NS + 3,
        retry_index=0,
    )
    journal.record_pre_intent_read_failure(
        reservation_sha256=second.reservation.reservation_sha256,
        failure=failure_type.EOF,
        io_may_have_occurred=False,
        observed_at_ns=DEADLINE_NS,
    )
    with pytest.raises(ExecutionJournalError, match="READ_RETRY_BUDGET_EXHAUSTED"):
        journal.reserve_pre_intent_read(
            authority_sha256=authority.authority_sha256,
            path="/fapi/v1/positionSide/dual",
            parameters={"recvWindow": str(RECEIVE_WINDOW_MS)},
            elapsed_seconds=Decimal("4"),
            deadline_ns=DEADLINE_NS + 4,
            retry_index=1,
        )


def test_bound_request_proof_requires_prior_exact_reserved_request(tmp_path) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="proof-requires-exact",
    )
    reserved = _exact_create_request(journal, authority, persisted)
    proof = _exact_create_proof(reserved, authority)

    with pytest.raises(
        ExecutionJournalError,
        match="EXACT_REQUEST_RESERVATION_REQUIRED",
    ):
        journal.record_mutation_reservation(proof)


def test_bound_request_proof_checks_every_reserved_request_digest(tmp_path) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="proof-exact-binding",
    )
    reserved = _exact_create_request(journal, authority, persisted)
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )
    correct = _exact_create_proof(reserved, authority)
    forged = MutationReservationProof.build(
        request_sha256=correct.request_sha256,
        logical_request_sha256=correct.logical_request_sha256,
        kind=correct.kind,
        purpose=correct.purpose,
        method=correct.method,
        path=correct.path,
        retry_index=correct.retry_index,
        client_id=correct.client_id,
        authorization_id=correct.authorization_id,
        intent_sha256=correct.intent_sha256,
        generation=correct.generation,
        deadline_ns=correct.deadline_ns,
        monotonic_sequence=correct.monotonic_sequence,
        parameters_sha256="f" * 64,
        ledger_sha256=correct.ledger_sha256,
        source_attempt_id=None,
        precondition_sha256=None,
    )

    with pytest.raises(ExecutionJournalError, match="EXACT_REQUEST_PROOF_MISMATCH"):
        journal.record_mutation_reservation(forged)
    journal.record_mutation_reservation(correct)


def test_exact_create_pending_replays_and_clears_only_after_typed_confirmation(
    tmp_path,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="pending-create-restart",
    )
    reserved = _exact_create_request(journal, authority, persisted)
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )
    reopened = ExecutionJournal(journal.path)
    before = reopened.request_ledger_snapshot(authority.authority_sha256)
    assert before.pending_requests == (reserved,)
    assert before.last_ledger.stage is RequestStage.CREATE_ATTEMPTED
    assert before.last_ledger.total_http_requests == 12
    proof = _exact_create_proof(reserved, authority)
    reopened.record_mutation_reservation(proof)
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS + 100,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    reopened.prepare_attempt(attempt)
    reopened.record_go(attempt.attempt_id)
    reopened.record_confirmed(attempt.attempt_id, _sha("typed-create-result"))

    after = ExecutionJournal(journal.path).request_ledger_snapshot(authority.authority_sha256)
    assert after.pending_requests == ()
    assert after.last_ledger == reserved.ledger


def test_exact_post_create_read_requires_anchor_and_result_clears_pending(
    tmp_path,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="post-create-read",
    )
    create = _exact_create_request(journal, authority, persisted)
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=create,
    )
    create_proof = _exact_create_proof(create, authority)
    journal.record_mutation_reservation(create_proof)
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS + 100,
        reservation_sha256=create.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)
    journal.record_confirmed(attempt.attempt_id, _sha("create-result-before-read"))
    query = ReservedRequest(
        ledger=replace(
            create.ledger,
            total_http_requests=13,
            post_create_read_requests=1,
            last_elapsed_seconds=Decimal("13"),
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=tuple(sorted(persisted.intent.query_parameters.items())),
        elapsed_seconds=Decimal("13"),
        retry_index=0,
    )
    read_proof = ReadReservationProof.from_reserved_request(
        query,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.EVIDENCE,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=attempt.attempt_id,
        client_id=attempt.client_id,
        authorization_id=AUTHORIZATION_ID,
    )
    with pytest.raises(
        ExecutionJournalError,
        match="EXACT_REQUEST_RESERVATION_REQUIRED",
    ):
        journal.record_read_prepared(read_proof)
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=query,
    )
    prepared = journal.record_read_prepared(read_proof)
    result = ReadResultProof.build(
        request_sha256=query.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest=prepared.digest,
        generation=1,
        monotonic_sequence=13,
        read_kind=ReadKind.ORDER,
        outcome=ReadOutcome.ORDER_NEW,
        result_sha256=_sha("post-create-order-result"),
        observed_at_ns=DEADLINE_NS,
    )
    journal.record_read_result(result)
    snapshot = ExecutionJournal(journal.path).request_ledger_snapshot(authority.authority_sha256)
    assert snapshot.pending_requests == ()
    assert snapshot.last_ledger == query.ledger


def test_bound_chain_allows_exactly_eleven_reads_then_requires_create(tmp_path) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="exactly-eleven",
    )
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    extra_read = ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=12,
            last_elapsed_seconds=Decimal("12"),
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/time",
        purpose=RequestPurpose.READ,
        parameters=(),
        elapsed_seconds=Decimal("12"),
        retry_index=0,
    )

    with pytest.raises(ExecutionJournalError, match="POST_BIND_CREATE_REQUIRED"):
        journal.record_exact_request_reservation(
            authority_sha256=authority.authority_sha256,
            generation=1,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=extra_read,
        )


def _recovery_order_read_request(
    journal: ExecutionJournal,
    authority: object,
    source: MutationAttempt,
) -> ReservedRequest:
    previous = journal.request_ledger_snapshot(authority.authority_sha256).last_ledger
    elapsed = previous.last_elapsed_seconds + Decimal("1")
    return ReservedRequest(
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


def test_recovery_generation_requires_journal_issued_typed_session_authority(
    tmp_path,
) -> None:
    recovery_type = getattr(journal_module, "RecoverySessionAuthority", None)
    assert recovery_type is not None, "RecoverySessionAuthority must exist"
    journal, source = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    primary, _intent = _ensure_test_request_chain(journal)
    read = _recovery_order_read_request(journal, primary, source)

    with pytest.raises(
        ExecutionJournalError,
        match="RECOVERY_SESSION_AUTHORITY_REQUIRED",
    ):
        journal.record_exact_request_reservation(
            authority_sha256=primary.authority_sha256,
            generation=2,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=read,
        )

    issued = journal.issue_recovery_session_authority(
        primary_authority_sha256=primary.authority_sha256,
        source_attempt_id=source.attempt_id,
    )
    recovery = issued.event.authority
    assert type(recovery) is recovery_type
    assert recovery.primary_authority_sha256 == primary.authority_sha256
    assert recovery.source_attempt_id == source.attempt_id
    assert recovery.source_authorization_id == source.authorization_id
    assert recovery.source_intent_sha256 == source.intent_sha256
    assert recovery.source_client_id == source.client_id
    assert recovery.generation == 2
    journal.record_exact_request_reservation(
        authority_sha256=recovery.authority_sha256,
        generation=2,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=read,
    )


def test_recovery_authority_reissues_for_gen3_with_same_lineage_and_no_create(
    tmp_path,
) -> None:
    journal, source = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    primary, _intent = _ensure_test_request_chain(journal)
    issued2 = journal.issue_recovery_session_authority(
        primary_authority_sha256=primary.authority_sha256,
        source_attempt_id=source.attempt_id,
    )
    recovery2 = issued2.event.authority

    with pytest.raises(ExecutionJournalError, match="RECOVERY_CREATE_FORBIDDEN"):
        journal.record_exact_request_reservation(
            authority_sha256=recovery2.authority_sha256,
            generation=2,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=_default_create_reserved_request(),
        )
    _reap(journal, 2)
    _admit(journal, 3, GenerationCapability.RECOVERY)
    issued3 = journal.issue_recovery_session_authority(
        primary_authority_sha256=primary.authority_sha256,
        source_attempt_id=source.attempt_id,
    )
    recovery3 = issued3.event.authority

    assert recovery3.authority_sha256 != recovery2.authority_sha256
    assert recovery3.generation == 3
    assert recovery3.primary_authority_sha256 == recovery2.primary_authority_sha256
    assert recovery3.source_attempt_id == recovery2.source_attempt_id
    assert recovery3.source_authorization_id == recovery2.source_authorization_id
    assert recovery3.source_intent_sha256 == recovery2.source_intent_sha256
    assert recovery3.source_runtime_commit == recovery2.source_runtime_commit
    assert recovery3.source_session_nonce == recovery2.source_session_nonce
    assert recovery3.source_client_id == recovery2.source_client_id


@pytest.mark.parametrize(
    "name",
    [
        "IO_AMBIGUOUS",
        "RESPONSE_INVALID",
        "NETWORK_GUARD",
        "TRANSPORT_REJECTED",
        "EXECUTOR_FAILURE",
        "RESULT_INVALID",
    ],
)
def test_session_read_failure_projection_is_exact_and_stable(name) -> None:
    failure_type = journal_module.ReadFailureKind
    assert failure_type[name].value == name
    assert journal_module.project_read_failure_kind(name) is failure_type[name]
    with pytest.raises(ExecutionJournalError, match="READ_FAILURE_KIND_NOT_ALLOWED"):
        journal_module.project_read_failure_kind(f"{name}_DETAIL")


@pytest.mark.parametrize(
    "failure_name",
    [
        "IO_AMBIGUOUS",
        "RESPONSE_INVALID",
        "NETWORK_GUARD",
        "TRANSPORT_REJECTED",
        "EXECUTOR_FAILURE",
        "RESULT_INVALID",
    ],
)
def test_exact_read_failure_is_durable_and_retry_budget_survives_restart(
    tmp_path,
    failure_name: str,
) -> None:
    journal = ExecutionJournal(tmp_path / "exact-read-failure.jsonl")
    _admit(journal, 1, GenerationCapability.PRIMARY)
    source = _reserved_create(journal, label="failure-source")
    _prepare(journal, source)
    journal.record_go(source.attempt_id)
    journal.record_confirmed(source.attempt_id, _sha("failure-source-result"))
    authority, intent = _ensure_test_request_chain(journal)
    first = _recovery_order_read_request(journal, authority, source)
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=first,
    )
    first_proof = ReadReservationProof.from_reserved_request(
        first,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.EVIDENCE,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=source.attempt_id,
        client_id=source.client_id,
        authorization_id=AUTHORIZATION_ID,
    )
    journal.record_read_prepared(first_proof)
    failure_record = journal.record_exact_read_failure(
        request_sha256=first.request_sha256,
        failure=journal_module.ReadFailureKind[failure_name],
        io_may_have_occurred=True,
        observed_at_ns=DEADLINE_NS,
    )
    assert failure_record.event.failure.failure is (journal_module.ReadFailureKind[failure_name])
    reopened = ExecutionJournal(journal.path)
    failed = reopened.request_ledger_snapshot(authority.authority_sha256)
    assert failed.pending_requests == ()
    assert failed.retryable_logical_request_sha256 == first.logical_request_sha256
    assert failed.last_ledger.retryable_read_sha256 == first.logical_request_sha256
    retry_elapsed = failed.last_ledger.last_elapsed_seconds + Decimal("1")
    retry = ReservedRequest(
        ledger=replace(
            failed.last_ledger,
            total_http_requests=failed.last_ledger.total_http_requests + 1,
            read_retry_requests=1,
            post_create_read_requests=(failed.last_ledger.post_create_read_requests + 1),
            last_elapsed_seconds=retry_elapsed,
            retryable_read_sha256=None,
        ),
        intent_sha256=intent.intent_sha256,
        origin=first.origin,
        method=first.method,
        path=first.path,
        purpose=first.purpose,
        parameters=first.parameters,
        elapsed_seconds=retry_elapsed,
        retry_index=1,
    )
    reopened.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=retry,
    )
    retry_proof = ReadReservationProof.from_reserved_request(
        retry,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.EVIDENCE,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=source.attempt_id,
        client_id=source.client_id,
        authorization_id=AUTHORIZATION_ID,
    )
    prepared = reopened.record_read_prepared(retry_proof)
    retry_result = ReadResultProof.build(
        request_sha256=retry.request_sha256,
        prepared_record_sequence=prepared.sequence,
        prepared_record_digest=prepared.digest,
        generation=1,
        monotonic_sequence=retry.ledger.total_http_requests,
        read_kind=ReadKind.ORDER,
        outcome=ReadOutcome.ORDER_NEW,
        result_sha256=_sha("retry-read-success"),
        observed_at_ns=DEADLINE_NS,
    )
    reopened.record_read_result(retry_result)
    final = ExecutionJournal(journal.path).request_ledger_snapshot(authority.authority_sha256)
    assert final.last_ledger.read_retry_requests == 1
    assert final.last_ledger.retryable_read_sha256 is None
    assert final.retryable_logical_request_sha256 is None


def _record_bound_create_go(
    journal: ExecutionJournal,
    authority: object,
    persisted: object,
    *,
    parameters_override: dict[str, str] | None = None,
    record_go: bool = True,
) -> tuple[ReservedRequest, MutationReservationProof, MutationAttempt]:
    reserved = _exact_create_request(journal, authority, persisted)
    if parameters_override is not None:
        reserved = replace(
            reserved,
            parameters=tuple(sorted(parameters_override.items())),
        )
    journal.record_exact_request_reservation(
        authority_sha256=authority.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )
    proof = _exact_create_proof(reserved, authority)
    journal.record_mutation_reservation(proof)
    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=1,
        retry_index=0,
        deadline_ns=DEADLINE_NS + 100,
        reservation_sha256=reserved.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    journal.prepare_attempt(attempt)
    if record_go:
        journal.record_go(attempt.attempt_id)
    return reserved, proof, attempt


def _recorded_dispatch_parts(
    journal: ExecutionJournal,
    attempt: MutationAttempt,
) -> tuple[ReservedRequest, MutationReservationProof]:
    reserved = next(
        record.event.reserved_request
        for record in journal.records()
        if type(record.event).__name__ == "_ExactRequestReserved"
        and record.event.reserved_request.request_sha256 == attempt.reservation_sha256
    )
    proof = next(
        record.event.proof
        for record in journal.records()
        if type(record.event).__name__ == "_MutationReserved"
        and record.event.proof.request_sha256 == attempt.reservation_sha256
    )
    return reserved, proof


@pytest.mark.parametrize("tamper_price", [False, True])
def test_child_economic_verifier_binds_create_to_replayed_intent(
    tmp_path,
    tamper_price: bool,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="dispatch-create-economic",
    )
    parameters = persisted.intent.probe_payload
    if tamper_price:
        parameters = {**parameters, "price": "9999.99"}
    reserved, proof, attempt = _record_bound_create_go(
        journal,
        authority,
        persisted,
        parameters_override=parameters,
    )

    if tamper_price:
        with pytest.raises(
            ExecutionJournalError,
            match="MUTATION_DISPATCH_ECONOMIC_MISMATCH",
        ):
            journal.verify_child_economic_binding(
                attempt=attempt,
                reservation_proof=proof,
                reserved_request=reserved,
                persisted_intent_path=persisted.path,
            )
        return

    receipt = ExecutionJournal(journal.path).verify_child_economic_binding(
        attempt=attempt,
        reservation_proof=proof,
        reserved_request=reserved,
        persisted_intent_path=persisted.path,
    )
    assert receipt.attempt == attempt
    assert receipt.reservation_proof == proof
    assert receipt.reserved_request == reserved
    assert receipt.intent_binding.intent_sha256 == persisted.intent.intent_sha256
    assert receipt.journal_head_digest == journal.records()[-1].digest


def test_child_economic_verifier_rejects_prepared_without_durable_go(tmp_path) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="dispatch-create-before-go",
    )
    reserved, proof, attempt = _record_bound_create_go(
        journal,
        authority,
        persisted,
        record_go=False,
    )

    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_DISPATCH_JOURNAL_MISMATCH",
    ):
        journal.verify_child_economic_binding(
            attempt=attempt,
            reservation_proof=proof,
            reserved_request=reserved,
            persisted_intent_path=persisted.path,
        )


@pytest.mark.parametrize("tamper_recv_window", [False, True])
def test_child_economic_verifier_requires_consumed_fresh_open_cancel(
    tmp_path,
    tamper_recv_window: bool,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="dispatch-cancel-economic",
    )
    _reserved, _proof, source = _record_bound_create_go(
        journal,
        authority,
        persisted,
    )
    journal.record_confirmed(source.attempt_id, _sha("dispatch-cancel-create"))
    observation = _open_observation(
        journal,
        source,
        generation=1,
        reservation="dispatch-cancel-open",
    )
    parameters = persisted.intent.cancel_parameters
    if tamper_recv_window:
        parameters = {**parameters, "recvWindow": "1"}
    cancel = _reserved_noncreate(
        journal,
        label="dispatch-cancel",
        kind=MutationKind.CANCEL,
        purpose=MutationPurpose.PRIMARY_CANCEL,
        generation=1,
        source=source,
        precondition_sha256=observation.observation_sha256,
        parameters_override=parameters,
    )
    journal.prepare_attempt(cancel)
    journal.record_go(cancel.attempt_id)
    reserved, proof = _recorded_dispatch_parts(journal, cancel)

    if tamper_recv_window:
        with pytest.raises(
            ExecutionJournalError,
            match="MUTATION_DISPATCH_ECONOMIC_MISMATCH",
        ):
            journal.verify_child_economic_binding(
                attempt=cancel,
                reservation_proof=proof,
                reserved_request=reserved,
                persisted_intent_path=persisted.path,
            )
        return

    receipt = journal.verify_child_economic_binding(
        attempt=cancel,
        reservation_proof=proof,
        reserved_request=reserved,
        persisted_intent_path=persisted.path,
    )
    assert receipt.precondition_sha256 == observation.observation_sha256


@pytest.mark.parametrize("foreign_filter", [False, True])
def test_child_economic_verifier_binds_close_proof_to_intent_and_residual(
    tmp_path,
    foreign_filter: bool,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="dispatch-close-economic",
    )
    _reserved, _proof, source = _record_bound_create_go(
        journal,
        authority,
        persisted,
    )
    journal.record_confirmed(source.attempt_id, _sha("dispatch-close-create"))
    owned = _owned_fill_proof(
        journal,
        source,
        generation=1,
        label="dispatch-close-proof",
        filter_snapshot_sha256=(_sha("foreign-filter") if foreign_filter else None),
    )
    close = _reserved_noncreate(
        journal,
        label="dispatch-close",
        kind=MutationKind.EMERGENCY_CLOSE,
        purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
        generation=1,
        source=source,
        precondition_sha256=owned.proof_sha256,
    )
    journal.prepare_attempt(close)
    journal.record_go(close.attempt_id)
    reserved, proof = _recorded_dispatch_parts(journal, close)

    if foreign_filter:
        with pytest.raises(
            ExecutionJournalError,
            match="MUTATION_DISPATCH_ECONOMIC_MISMATCH",
        ):
            journal.verify_child_economic_binding(
                attempt=close,
                reservation_proof=proof,
                reserved_request=reserved,
                persisted_intent_path=persisted.path,
            )
        return

    receipt = journal.verify_child_economic_binding(
        attempt=close,
        reservation_proof=proof,
        reserved_request=reserved,
        persisted_intent_path=persisted.path,
    )
    assert receipt.precondition_sha256 == owned.proof_sha256
    assert receipt.reserved_request.parameters == tuple(
        sorted(persisted.intent.emergency_close_payload(Decimal(owned.residual_quantity)).items())
    )


def test_child_economic_verifier_rejects_close_above_probe_quantity(
    tmp_path,
) -> None:
    journal, authority, persisted = _bound_request_ledger(
        tmp_path,
        name="dispatch-close-excess-residual",
    )
    _reserved, _proof, source = _record_bound_create_go(
        journal,
        authority,
        persisted,
    )
    journal.record_confirmed(source.attempt_id, _sha("dispatch-close-excess-create"))
    owned = _owned_fill_proof(
        journal,
        source,
        generation=1,
        label="dispatch-close-excess-proof",
        residual_quantity=Decimal("0.004"),
    )
    parameters = persisted.intent.emergency_close_payload(Decimal("0.003"))
    parameters = {**parameters, "quantity": "0.004"}
    close = _reserved_noncreate(
        journal,
        label="dispatch-close-excess",
        kind=MutationKind.EMERGENCY_CLOSE,
        purpose=MutationPurpose.PRIMARY_EMERGENCY_CLOSE,
        generation=1,
        source=source,
        precondition_sha256=owned.proof_sha256,
        parameters_override=parameters,
    )
    journal.prepare_attempt(close)
    journal.record_go(close.attempt_id)
    reserved, proof = _recorded_dispatch_parts(journal, close)

    with pytest.raises(
        ExecutionJournalError,
        match="MUTATION_DISPATCH_ECONOMIC_MISMATCH",
    ):
        journal.verify_child_economic_binding(
            attempt=close,
            reservation_proof=proof,
            reserved_request=reserved,
            persisted_intent_path=persisted.path,
        )


def _issue_intent_bound_recovery(
    journal: ExecutionJournal,
    primary_authority_sha256: str,
):
    issuer = getattr(journal, "issue_intent_bound_recovery_authority", None)
    assert callable(issuer), "intent-bound recovery issuer must exist"
    return issuer(primary_authority_sha256=primary_authority_sha256)


def _intent_bound_probe_query(
    journal: ExecutionJournal,
    primary: object,
    persisted: object,
) -> ReservedRequest:
    previous = journal.request_ledger_snapshot(primary.authority_sha256).last_ledger
    elapsed = previous.last_elapsed_seconds + Decimal("1")
    return ReservedRequest(
        ledger=replace(
            previous,
            total_http_requests=previous.total_http_requests + 1,
            post_create_read_requests=(
                previous.post_create_read_requests + (1 if previous.create_requests == 1 else 0)
            ),
            last_elapsed_seconds=elapsed,
        ),
        intent_sha256=persisted.intent.intent_sha256,
        origin=DEMO_HTTP_ORIGIN,
        method="GET",
        path="/fapi/v1/order",
        purpose=RequestPurpose.READ,
        parameters=tuple(sorted(persisted.intent.query_parameters.items())),
        elapsed_seconds=elapsed,
        retry_index=0,
    )


def _record_intent_bound_probe_result(
    journal: ExecutionJournal,
    *,
    recovery_authority_sha256: str,
    primary: object,
    persisted: object,
    generation: int,
) -> ReservedRequest:
    reserved = _intent_bound_probe_query(journal, primary, persisted)
    journal.record_exact_request_reservation(
        authority_sha256=recovery_authority_sha256,
        generation=generation,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=reserved,
    )
    proof = ReadReservationProof.from_reserved_request(
        reserved,
        read_kind=ReadKind.ORDER,
        purpose=ReadPurpose.EVIDENCE,
        generation=generation,
        deadline_ns=DEADLINE_NS + 100,
        source_attempt_id=None,
        client_id=None,
        authorization_id=AUTHORIZATION_ID,
    )
    prepared = journal.record_read_prepared(proof)
    journal.record_read_result(
        ReadResultProof.build(
            request_sha256=reserved.request_sha256,
            prepared_record_sequence=prepared.sequence,
            prepared_record_digest=prepared.digest,
            generation=generation,
            monotonic_sequence=reserved.ledger.total_http_requests,
            read_kind=ReadKind.ORDER,
            outcome=ReadOutcome.ORDER_TERMINAL,
            result_sha256=_sha(f"intent-bound-probe-g{generation}"),
            observed_at_ns=DEADLINE_NS,
        )
    )
    return reserved


def test_intent_bound_without_attempt_issues_typed_read_only_recovery(
    tmp_path,
) -> None:
    target_type = getattr(journal_module, "RecoveryAuthorityTarget", None)
    authority_type = getattr(journal_module, "IntentBoundRecoveryAuthority", None)
    assert target_type is not None, "RecoveryAuthorityTarget must exist"
    assert authority_type is not None, "IntentBoundRecoveryAuthority must exist"
    journal, primary, persisted = _bound_request_ledger(
        tmp_path,
        name="intent-bound-no-attempt",
    )
    _reap(journal, 1)
    _admit(journal, 2, GenerationCapability.RECOVERY)

    issued = _issue_intent_bound_recovery(
        journal,
        primary.authority_sha256,
    )
    recovery = issued.event.authority
    assert type(recovery) is authority_type
    assert recovery.target is target_type.INTENT_BOUND_NO_ATTEMPT
    assert recovery.primary_authority_sha256 == primary.authority_sha256
    assert recovery.source_generation == 1
    assert recovery.source_authorization_id == AUTHORIZATION_ID
    assert recovery.source_intent_sha256 == persisted.intent.intent_sha256
    assert recovery.query_client_id == persisted.intent.client_order_id
    assert recovery.generation == 2
    assert recovery.allows_create is False
    assert recovery.allows_mutation is False
    assert not any(
        type(record.event).__name__ == "_AttemptPrepared" for record in journal.records()
    )

    wrong_client = replace(
        _intent_bound_probe_query(journal, primary, persisted),
        parameters=tuple(
            sorted(
                {
                    **persisted.intent.query_parameters,
                    "origClientOrderId": "foreign-client-id",
                }.items()
            )
        ),
    )
    with pytest.raises(
        ExecutionJournalError,
        match="INTENT_BOUND_RECOVERY_READ_BINDING_MISMATCH",
    ):
        journal.record_exact_request_reservation(
            authority_sha256=recovery.authority_sha256,
            generation=2,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=wrong_client,
        )

    unrelated_read = replace(
        _intent_bound_probe_query(journal, primary, persisted),
        path="/fapi/v1/time",
        parameters=(),
    )
    with pytest.raises(
        ExecutionJournalError,
        match="INTENT_BOUND_RECOVERY_READ_BINDING_MISMATCH",
    ):
        journal.record_exact_request_reservation(
            authority_sha256=recovery.authority_sha256,
            generation=2,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=unrelated_read,
        )

    reserved = _record_intent_bound_probe_result(
        journal,
        recovery_authority_sha256=recovery.authority_sha256,
        primary=primary,
        persisted=persisted,
        generation=2,
    )
    snapshot = ExecutionJournal(journal.path).request_ledger_snapshot(primary.authority_sha256)
    assert snapshot.pending_requests == ()
    assert snapshot.last_ledger == reserved.ledger
    assert snapshot.last_ledger.create_requests == 0


def test_intent_bound_recovery_rejects_create_reserve_attempt_and_go(
    tmp_path,
) -> None:
    journal, primary, persisted = _bound_request_ledger(
        tmp_path,
        name="intent-bound-create-forbidden",
    )
    _reap(journal, 1)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    recovery = _issue_intent_bound_recovery(
        journal,
        primary.authority_sha256,
    ).event.authority
    create = _exact_create_request(journal, primary, persisted)

    with pytest.raises(
        ExecutionJournalError,
        match="INTENT_BOUND_RECOVERY_MUTATION_FORBIDDEN",
    ):
        journal.record_exact_request_reservation(
            authority_sha256=recovery.authority_sha256,
            generation=2,
            deadline_ns=DEADLINE_NS + 100,
            reserved_request=create,
        )

    attempt = MutationAttempt.build(
        kind=MutationKind.CREATE,
        generation=2,
        retry_index=0,
        deadline_ns=DEADLINE_NS + 100,
        reservation_sha256=create.request_sha256,
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=persisted.intent.intent_sha256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
    )
    with pytest.raises(ExecutionJournalError, match="RECOVERY_MUTATION_FORBIDDEN"):
        journal.prepare_attempt(attempt)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_NOT_FOUND"):
        journal.record_go(attempt.attempt_id)


def test_intent_bound_recovery_abandons_durable_create_without_prepared_or_go(
    tmp_path,
) -> None:
    journal, primary, persisted = _bound_request_ledger(
        tmp_path,
        name="intent-bound-abandoned-create",
    )
    create = _exact_create_request(journal, primary, persisted)
    journal.record_exact_request_reservation(
        authority_sha256=primary.authority_sha256,
        generation=1,
        deadline_ns=DEADLINE_NS + 100,
        reserved_request=create,
    )
    assert not any(
        type(record.event).__name__ == "_AttemptPrepared" for record in journal.records()
    )
    _reap(journal, 1)
    _admit(journal, 2, GenerationCapability.RECOVERY)

    recovery = _issue_intent_bound_recovery(
        journal,
        primary.authority_sha256,
    ).event.authority
    assert recovery.abandoned_create_request_sha256 == create.request_sha256
    after_issue = ExecutionJournal(journal.path).request_ledger_snapshot(primary.authority_sha256)
    assert after_issue.pending_requests == ()
    assert after_issue.last_ledger == create.ledger

    _record_intent_bound_probe_result(
        journal,
        recovery_authority_sha256=recovery.authority_sha256,
        primary=primary,
        persisted=persisted,
        generation=2,
    )


def test_intent_bound_recovery_reissues_gen3_same_lineage_without_fake_attempt(
    tmp_path,
) -> None:
    journal, primary, persisted = _bound_request_ledger(
        tmp_path,
        name="intent-bound-gen3",
    )
    _reap(journal, 1)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    recovery2 = _issue_intent_bound_recovery(
        journal,
        primary.authority_sha256,
    ).event.authority
    _record_intent_bound_probe_result(
        journal,
        recovery_authority_sha256=recovery2.authority_sha256,
        primary=primary,
        persisted=persisted,
        generation=2,
    )
    _reap(journal, 2)
    _admit(journal, 3, GenerationCapability.RECOVERY)
    recovery3 = _issue_intent_bound_recovery(
        journal,
        primary.authority_sha256,
    ).event.authority

    assert recovery3.authority_sha256 != recovery2.authority_sha256
    assert recovery3.generation == 3
    assert recovery3.source_generation == recovery2.source_generation == 1
    assert recovery3.primary_authority_sha256 == recovery2.primary_authority_sha256
    assert recovery3.intent_binding_sha256 == recovery2.intent_binding_sha256
    assert recovery3.source_authorization_id == recovery2.source_authorization_id
    assert recovery3.source_intent_sha256 == recovery2.source_intent_sha256
    assert recovery3.source_runtime_commit == recovery2.source_runtime_commit
    assert recovery3.source_session_nonce == recovery2.source_session_nonce
    assert recovery3.query_client_id == recovery2.query_client_id
    assert not any(
        type(record.event).__name__ == "_AttemptPrepared" for record in journal.records()
    )

    _record_intent_bound_probe_result(
        journal,
        recovery_authority_sha256=recovery3.authority_sha256,
        primary=primary,
        persisted=persisted,
        generation=3,
    )


def test_intent_bound_target_rejects_any_existing_mutation_attempt(tmp_path) -> None:
    journal, source = _unknown_attempt(tmp_path, MutationKind.CREATE)
    _admit(journal, 2, GenerationCapability.RECOVERY)
    primary, _intent = _ensure_test_request_chain(journal)

    with pytest.raises(
        ExecutionJournalError,
        match="INTENT_BOUND_RECOVERY_REQUIRES_NO_ATTEMPT",
    ):
        _issue_intent_bound_recovery(journal, primary.authority_sha256)
    assert source.attempt_id
