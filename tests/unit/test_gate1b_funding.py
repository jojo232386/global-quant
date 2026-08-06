from __future__ import annotations

from decimal import Decimal

import pytest

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.coordinator import UnexplainedEventError
from global_quant.gate1a.ledger import AppendOnlyLedger


def coordinator(tmp_path) -> EventSourcedCoordinator:
    return EventSourcedCoordinator(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("1000"),
        strategy_id="GATE1B",
        run_id="funding-test",
        process_start_id="process-1",
        source_hash="source-hash",
        config_hash="config-hash",
    )


def test_funding_is_applied_exactly_once_and_replays(tmp_path) -> None:
    state = coordinator(tmp_path)

    assert state.apply_funding(
        source_event_id="funding:BTCUSDT:202608060800",
        instrument_id="BTCUSDT-PERP.BINANCE",
        amount=Decimal("-0.25"),
    )
    assert not state.apply_funding(
        source_event_id="funding:BTCUSDT:202608060800",
        instrument_id="BTCUSDT-PERP.BINANCE",
        amount=Decimal("-0.25"),
    )
    assert state.cumulative_funding == Decimal("-0.25")
    assert state.wallet_balance == Decimal("999.75")

    replayed = EventSourcedCoordinator.replay(
        ledger=state.ledger,
        initial_wallet=Decimal("1000"),
    )
    assert replayed.cumulative_funding == Decimal("-0.25")
    assert replayed.wallet_balance == Decimal("999.75")
    assert replayed.business_hash() == state.business_hash()


def test_conflicting_funding_event_fails_closed(tmp_path) -> None:
    state = coordinator(tmp_path)
    state.apply_funding(
        source_event_id="funding:BTCUSDT:202608060800",
        instrument_id="BTCUSDT-PERP.BINANCE",
        amount=Decimal("-0.25"),
    )

    with pytest.raises(UnexplainedEventError, match="conflicting funding"):
        state.apply_funding(
            source_event_id="funding:BTCUSDT:202608060800",
            instrument_id="BTCUSDT-PERP.BINANCE",
            amount=Decimal("0.25"),
        )

    assert state.fail_closed is True
