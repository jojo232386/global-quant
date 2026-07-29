from __future__ import annotations

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
        order_intent={"side": "BUY", "quantity": "1"},
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
