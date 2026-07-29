from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.ledger import LedgerEvent
from global_quant.gate1a.ledger import LedgerIntegrityError


def make_event(event_id: str = "event-1", sequence: int = 1) -> LedgerEvent:
    return LedgerEvent(
        decision_id="decision-1",
        strategy_id="gate1a",
        run_id="run-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        client_order_id="order-1",
        venue_order_id=None,
        position_id="position-BTC",
        correlation_id="decision-1",
        causation_id="decision-1",
        event_id=event_id,
        event_sequence=sequence,
        event_timestamp="2026-01-01T00:00:00+00:00",
        receive_timestamp="2026-01-01T00:00:01+00:00",
        event_type="ORDER_INTENT",
        order_intent={
            "side": "BUY",
            "quantity": "1",
            "role": "TARGET",
            "reduce_only": False,
            "trigger_price": None,
        },
        order_transition=None,
        fill=None,
        fee=None,
        position_transition=None,
        balance_transition=None,
        protection_group_id=None,
        persistence_checkpoint=None,
        process_start_id="process-1",
        source_hash="source-hash",
        config_hash="config-hash",
    )


def raw_event_for(event_type: str) -> dict:
    raw = make_event().to_dict()
    raw["event_type"] = event_type
    raw["order_intent"] = None
    raw["order_transition"] = None
    raw["fill"] = None
    raw["fee"] = None
    raw["position_transition"] = None
    raw["balance_transition"] = None
    raw["protection_group_id"] = None

    if event_type == "DECISION":
        raw["order_intent"] = {"target_quantity": "1"}
    elif event_type == "ORDER_INTENT":
        raw["order_intent"] = {
            "side": "BUY",
            "quantity": "1",
            "role": "TARGET",
            "reduce_only": False,
            "trigger_price": None,
        }
    elif event_type == "STALE_ORDER_EVENT":
        raw["order_transition"] = {"from": "FILLED", "attempted": "CANCELED"}
    elif event_type == "ORDER_TRANSITION":
        raw["order_transition"] = {"from": "INITIALIZED", "to": "SUBMITTED"}
    elif event_type == "FILL":
        raw["fill"] = {
            "fill_id": "fill-1",
            "side": "BUY",
            "quantity": "1",
            "signed_quantity": "1",
            "price": "100.00",
        }
        raw["fee"] = "0.10"
        raw["position_transition"] = {
            "quantity_before": "0",
            "signed_fill_quantity": "1",
        }
        raw["balance_transition"] = {"fee": "0.10"}
    elif event_type == "PROTECTION_RESIZE":
        raw["order_transition"] = {
            "from_quantity": "1",
            "to_quantity": "0.5",
            "status": "ACCEPTED",
        }
        raw["protection_group_id"] = "protection-1"
    elif event_type == "MARK_PRICE":
        raw["fill"] = {"price": "100.00"}
    elif event_type == "RECONCILIATION":
        raw["balance_transition"] = {
            "wallet_balance": "10000.00",
            "positions": {"BTCUSDT-PERP.BINANCE": "1"},
        }
    elif event_type == "ANOMALY":
        raw["fill"] = {"message": "account mismatch"}
    return raw


def canonical_event_hash(raw: dict) -> str:
    payload = dict(raw)
    payload["event_hash"] = ""
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_ledger_writes_every_required_field_as_canonical_json(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    ledger = AppendOnlyLedger(path)

    assert ledger.append(make_event()) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == set(LedgerEvent.required_fields())
    assert raw["fee"] is None
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_duplicate_event_is_idempotent_and_not_appended_twice(tmp_path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    event = make_event()

    assert ledger.append(event) is True
    assert ledger.append(event) is False
    assert len(ledger.read_all()) == 1


def test_same_event_id_with_different_payload_is_integrity_error(tmp_path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    ledger.append(make_event())

    conflicting = make_event()
    conflicting.order_intent["quantity"] = "2"

    with pytest.raises(LedgerIntegrityError, match="conflicting duplicate"):
        ledger.append(conflicting)


def test_sequence_must_be_strictly_monotonic(tmp_path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    ledger.append(make_event(sequence=1))

    with pytest.raises(LedgerIntegrityError, match="sequence"):
        ledger.append(make_event(event_id="event-2", sequence=3))


def test_business_hash_ignores_only_declared_volatile_fields(tmp_path) -> None:
    first = AppendOnlyLedger(tmp_path / "first.jsonl")
    second = AppendOnlyLedger(tmp_path / "second.jsonl")
    event_a = make_event()
    event_b = make_event()
    event_b.run_id = "run-2"
    event_b.process_start_id = "process-2"
    event_b.receive_timestamp = "2027-01-01T00:00:00+00:00"

    first.append(event_a)
    second.append(event_b)

    assert first.business_hash() == second.business_hash()

    event_c = make_event(event_id="event-3")
    event_c.order_intent["quantity"] = str(Decimal("1.1"))
    third = AppendOnlyLedger(tmp_path / "third.jsonl")
    third.append(event_c)
    assert first.business_hash() != third.business_hash()


def test_truncated_last_line_is_never_silently_ignored(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":"broken"', encoding="utf-8")

    with pytest.raises(LedgerIntegrityError, match="invalid JSON"):
        AppendOnlyLedger(path)


def test_appended_event_cannot_be_mutated_through_caller_or_reader(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    ledger = AppendOnlyLedger(path)
    event = make_event()
    ledger.append(event)
    original_hash = ledger.business_hash()

    event.order_intent["quantity"] = "999"
    ledger.read_all()[0].order_intent["quantity"] = "888"

    assert ledger.business_hash() == original_hash
    assert AppendOnlyLedger(path).business_hash() == original_hash


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("schema_version", "2.0"),
        ("settlement_currency", "BTC"),
        ("quantity_precision", 6),
        ("quantity_precision", 8.0),
        ("price_precision", 4),
        ("price_precision", 2.0),
    ],
)
def test_from_dict_rejects_invalid_frozen_contract_fields(
    field: str,
    invalid_value: object,
) -> None:
    raw = make_event().to_dict()
    raw[field] = invalid_value

    with pytest.raises(LedgerIntegrityError, match=field):
        LedgerEvent.from_dict(raw)


@pytest.mark.parametrize("invalid_event_type", ["FUTURE_EVENT", 1, None, []])
def test_from_dict_rejects_unknown_event_type(invalid_event_type: object) -> None:
    raw = make_event().to_dict()
    raw["event_type"] = invalid_event_type

    with pytest.raises(LedgerIntegrityError, match="unknown event_type"):
        LedgerEvent.from_dict(raw)


@pytest.mark.parametrize(
    "event_type",
    [
        "DECISION",
        "ORDER_INTENT",
        "STALE_ORDER_EVENT",
        "ORDER_TRANSITION",
        "FILL",
        "PROTECTION_RESIZE",
        "MARK_PRICE",
        "RECONCILIATION",
        "ANOMALY",
    ],
)
def test_from_dict_accepts_every_gate1a_event_type(event_type: str) -> None:
    assert LedgerEvent.from_dict(raw_event_for(event_type)).event_type == event_type


@pytest.mark.parametrize(
    ("event_type", "container", "field"),
    [
        ("DECISION", "order_intent", "target_quantity"),
        ("ORDER_INTENT", "order_intent", "quantity"),
        ("ORDER_INTENT", "order_intent", "trigger_price"),
        ("FILL", "fill", "quantity"),
        ("FILL", "fill", "price"),
        ("FILL", "position_transition", "quantity_before"),
        ("FILL", "balance_transition", "fee"),
        ("PROTECTION_RESIZE", "order_transition", "to_quantity"),
        ("MARK_PRICE", "fill", "price"),
        ("RECONCILIATION", "balance_transition", "wallet_balance"),
        ("RECONCILIATION", "balance_transition", "positions"),
    ],
)
def test_from_dict_rejects_missing_required_economic_fields(
    event_type: str,
    container: str,
    field: str,
) -> None:
    raw = raw_event_for(event_type)
    raw[container].pop(field)

    with pytest.raises(LedgerIntegrityError, match="required economic field"):
        LedgerEvent.from_dict(raw)


@pytest.mark.parametrize(
    "invalid_quantity",
    [
        1,
        "01",
        "+1",
        "1e0",
        "NaN",
        "Infinity",
        "-0",
    ],
)
def test_from_dict_rejects_noncanonical_decimal_values(
    invalid_quantity: object,
) -> None:
    raw = make_event().to_dict()
    raw["order_intent"]["quantity"] = invalid_quantity

    with pytest.raises(LedgerIntegrityError, match="canonical Decimal"):
        LedgerEvent.from_dict(raw)


@pytest.mark.parametrize(
    "field",
    ["source_hash", "config_hash", "strategy_id", "account_id"],
)
def test_ledger_rejects_identity_drift_before_writing(
    tmp_path,
    field: str,
) -> None:
    path = tmp_path / "events.jsonl"
    ledger = AppendOnlyLedger(path)
    ledger.append(make_event())
    original_payload = path.read_bytes()
    conflicting = make_event(event_id="event-2", sequence=2)
    setattr(conflicting, field, f"different-{field}")

    with pytest.raises(LedgerIntegrityError, match="ledger identity mismatch"):
        ledger.append(conflicting)

    assert path.read_bytes() == original_payload
    assert len(ledger.read_all()) == 1


def test_semantic_tampering_is_rejected_even_with_a_recomputed_valid_hash(
    tmp_path,
) -> None:
    path = tmp_path / "events.jsonl"
    ledger = AppendOnlyLedger(path)
    ledger.append(make_event())
    raw = json.loads(path.read_text(encoding="utf-8"))
    original_hash = raw["event_hash"]

    raw["order_intent"]["quantity"] = "01"
    raw["event_hash"] = canonical_event_hash(raw)
    assert raw["event_hash"] != original_hash
    assert raw["event_hash"] == canonical_event_hash(raw)
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(LedgerIntegrityError, match="canonical Decimal"):
        AppendOnlyLedger(path)
