from __future__ import annotations

from decimal import Decimal
from datetime import datetime

import pytest

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.coordinator import UnexplainedEventError
from global_quant.gate1a.ledger import AppendOnlyLedger


BTC = "BTCUSDT-PERP.BINANCE"


@pytest.fixture
def coordinator(tmp_path) -> EventSourcedCoordinator:
    return EventSourcedCoordinator(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        strategy_id="gate1a",
        run_id="run-1",
        process_start_id="process-1",
        source_hash="source-hash",
        config_hash="config-hash",
    )


def open_long(
    coordinator: EventSourcedCoordinator,
    *,
    decision_id: str = "decision-1",
    quantity: Decimal = Decimal("1"),
    price: Decimal = Decimal("100"),
) -> str:
    order = coordinator.request_target(decision_id, BTC, quantity)
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    coordinator.mark_accepted(order.client_order_id, "venue-1")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id=f"fill-{decision_id}",
        quantity=quantity,
        price=price,
        fee=Decimal("0.10"),
    )
    return order.client_order_id


def test_same_decision_and_target_cannot_create_duplicate_order(coordinator) -> None:
    first = coordinator.request_target("decision-1", BTC, Decimal("1"))
    duplicate = coordinator.request_target("decision-1", BTC, Decimal("1"))

    assert first is not None
    assert duplicate is None
    assert list(coordinator.orders) == [first.client_order_id]


def test_duplicate_fill_changes_position_and_wallet_once(coordinator) -> None:
    order_id = open_long(coordinator)
    wallet_after_first = coordinator.wallet_balance

    applied = coordinator.apply_fill(
        order_id,
        fill_id="fill-decision-1",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.10"),
    )

    assert applied is False
    assert coordinator.position(BTC).quantity == Decimal("1")
    assert coordinator.wallet_balance == wallet_after_first
    coordinator.assert_invariants()


def test_conflicting_duplicate_fill_fails_closed(coordinator) -> None:
    order_id = open_long(coordinator)

    with pytest.raises(UnexplainedEventError, match="conflicting duplicate fill"):
        coordinator.apply_fill(
            order_id,
            fill_id="fill-decision-1",
            quantity=Decimal("0.5"),
            price=Decimal("100"),
            fee=Decimal("0.05"),
        )

    assert coordinator.fail_closed is True
    assert coordinator.position(BTC).quantity == Decimal("1")


def test_duplicate_public_order_event_is_idempotent(coordinator) -> None:
    order = coordinator.request_target("decision-1", BTC, Decimal("1"))
    assert order is not None

    first = coordinator.mark_submitted(
        order.client_order_id,
        source_event_id="venue-event-submit-1",
    )
    duplicate = coordinator.mark_submitted(
        order.client_order_id,
        source_event_id="venue-event-submit-1",
    )

    assert first is True
    assert duplicate is False
    assert coordinator.orders[order.client_order_id].status == "SUBMITTED"


def test_conflicting_reuse_of_order_source_event_id_fails_closed(coordinator) -> None:
    order = coordinator.request_target("decision-1", BTC, Decimal("1"))
    assert order is not None
    coordinator.mark_submitted(
        order.client_order_id,
        source_event_id="venue-event-1",
    )

    with pytest.raises(UnexplainedEventError, match="conflicting duplicate order event"):
        coordinator.mark_accepted(
            order.client_order_id,
            "venue-1",
            source_event_id="venue-event-1",
        )

    assert coordinator.fail_closed is True


def test_late_cancel_cannot_change_filled_order(coordinator) -> None:
    order_id = open_long(coordinator)

    applied = coordinator.mark_canceled(
        order_id,
        source_event_id="late-cancel-after-fill",
    )

    assert applied is True
    assert coordinator.orders[order_id].status == "FILLED"
    assert coordinator.ledger.read_all()[-1].event_type == "STALE_ORDER_EVENT"
    assert coordinator.fail_closed is False


def test_matching_account_snapshot_is_idempotent(coordinator) -> None:
    open_long(coordinator)
    positions = {BTC: Decimal("1")}

    first = coordinator.reconcile_account_snapshot(
        source_event_id="account-snapshot-1",
        wallet_balance=coordinator.wallet_balance,
        positions=positions,
    )
    duplicate = coordinator.reconcile_account_snapshot(
        source_event_id="account-snapshot-1",
        wallet_balance=coordinator.wallet_balance,
        positions=positions,
    )

    assert first is True
    assert duplicate is False
    assert coordinator.fail_closed is False


def test_conflicting_duplicate_account_snapshot_fails_closed(coordinator) -> None:
    open_long(coordinator)
    coordinator.reconcile_account_snapshot(
        source_event_id="account-snapshot-1",
        wallet_balance=coordinator.wallet_balance,
        positions={BTC: Decimal("1")},
    )

    with pytest.raises(UnexplainedEventError, match="conflicting account snapshot"):
        coordinator.reconcile_account_snapshot(
            source_event_id="account-snapshot-1",
            wallet_balance=coordinator.wallet_balance,
            positions={BTC: Decimal("2")},
        )

    assert coordinator.fail_closed is True


def test_account_snapshot_mismatch_is_persisted_and_fails_closed(coordinator) -> None:
    open_long(coordinator)

    with pytest.raises(UnexplainedEventError, match="account snapshot mismatch"):
        coordinator.reconcile_account_snapshot(
            source_event_id="account-snapshot-bad",
            wallet_balance=coordinator.wallet_balance,
            positions={BTC: Decimal("2")},
        )

    assert coordinator.fail_closed is True
    assert coordinator.ledger.read_all()[-1].event_type == "ANOMALY"


def test_perpetual_accounting_identity_after_close(coordinator) -> None:
    open_long(coordinator, price=Decimal("100"))
    close_order = coordinator.request_target("decision-2", BTC, Decimal("0"))
    assert close_order is not None and close_order.reduce_only
    coordinator.mark_submitted(close_order.client_order_id)
    coordinator.mark_accepted(close_order.client_order_id, "venue-2")
    coordinator.apply_fill(
        close_order.client_order_id,
        fill_id="fill-close",
        quantity=Decimal("1"),
        price=Decimal("110"),
        fee=Decimal("0.11"),
    )

    assert coordinator.position(BTC).quantity == 0
    assert coordinator.realized_pnl == Decimal("10")
    assert coordinator.cumulative_fees == Decimal("0.21")
    assert coordinator.wallet_balance == Decimal("10009.79")
    assert coordinator.equity() == Decimal("10009.79")
    coordinator.assert_invariants()


def test_partial_fill_then_cancel_remainder(coordinator) -> None:
    order = coordinator.request_target("decision-1", BTC, Decimal("2"))
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    coordinator.mark_accepted(order.client_order_id, "venue-1")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="partial-1",
        quantity=Decimal("0.75"),
        price=Decimal("100"),
        fee=Decimal("0.075"),
    )
    coordinator.request_cancel(order.client_order_id)
    coordinator.mark_canceled(order.client_order_id)

    stored = coordinator.orders[order.client_order_id]
    assert stored.status == "CANCELED"
    assert stored.filled_quantity == Decimal("0.75")
    assert coordinator.position(BTC).quantity == Decimal("0.75")
    coordinator.assert_invariants()


def test_reversal_waits_until_old_position_is_fully_closed(coordinator) -> None:
    open_long(coordinator, quantity=Decimal("2"))

    close_order = coordinator.request_target("decision-2", BTC, Decimal("-1"))
    assert close_order is not None
    assert close_order.reduce_only is True
    assert close_order.quantity == Decimal("2")
    assert len(coordinator.active_orders(BTC)) == 1

    coordinator.mark_submitted(close_order.client_order_id)
    coordinator.mark_accepted(close_order.client_order_id, "venue-close")
    coordinator.apply_fill(
        close_order.client_order_id,
        fill_id="close-partial",
        quantity=Decimal("1"),
        price=Decimal("99"),
        fee=Decimal("0.099"),
    )
    assert coordinator.position(BTC).quantity == Decimal("1")
    assert len(coordinator.active_orders(BTC)) == 1

    coordinator.apply_fill(
        close_order.client_order_id,
        fill_id="close-final",
        quantity=Decimal("1"),
        price=Decimal("98"),
        fee=Decimal("0.098"),
    )
    open_orders = coordinator.active_orders(BTC)
    assert coordinator.position(BTC).quantity == 0
    assert len(open_orders) == 1
    assert open_orders[0].side == "SELL"
    assert open_orders[0].quantity == Decimal("1")
    assert open_orders[0].reduce_only is False


def test_protection_fill_cancels_siblings_and_flat_has_no_live_protection(
    coordinator,
) -> None:
    open_long(coordinator)
    stop, take = coordinator.create_protection_group(
        group_id="protect-1",
        instrument_id=BTC,
        quantity=Decimal("1"),
    )
    coordinator.mark_submitted(stop.client_order_id)
    coordinator.mark_accepted(stop.client_order_id, "venue-stop")
    coordinator.mark_submitted(take.client_order_id)
    coordinator.mark_accepted(take.client_order_id, "venue-take")

    coordinator.apply_fill(
        stop.client_order_id,
        fill_id="stop-fill",
        quantity=Decimal("1"),
        price=Decimal("95"),
        fee=Decimal("0.095"),
    )

    assert coordinator.orders[take.client_order_id].status == "CANCEL_PENDING"
    assert coordinator.position(BTC).quantity == 0
    coordinator.mark_canceled(take.client_order_id)
    coordinator.assert_invariants()


def test_partial_entry_protection_quantity_tracks_only_filled_position(
    coordinator,
) -> None:
    order = coordinator.request_target("decision-1", BTC, Decimal("1"))
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    coordinator.mark_accepted(order.client_order_id, "venue-1")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="partial-entry",
        quantity=Decimal("0.4"),
        price=Decimal("100"),
        fee=Decimal("0.04"),
    )

    stop, take = coordinator.create_protection_group(
        group_id="protect-partial",
        instrument_id=BTC,
        quantity=Decimal("1"),
    )

    assert stop.quantity == Decimal("0.4")
    assert take.quantity == Decimal("0.4")

    coordinator.apply_fill(
        order.client_order_id,
        fill_id="remaining-entry",
        quantity=Decimal("0.6"),
        price=Decimal("101"),
        fee=Decimal("0.0606"),
    )

    assert coordinator.orders[stop.client_order_id].quantity == Decimal("1")
    assert coordinator.orders[take.client_order_id].quantity == Decimal("1")


def test_second_protection_fill_before_cancel_ack_fails_closed_without_reopening(
    coordinator,
) -> None:
    open_long(coordinator)
    stop, take = coordinator.create_protection_group(
        group_id="protect-race",
        instrument_id=BTC,
        quantity=Decimal("1"),
    )
    coordinator.mark_submitted(stop.client_order_id)
    coordinator.mark_accepted(stop.client_order_id, "venue-stop")
    coordinator.mark_submitted(take.client_order_id)
    coordinator.mark_accepted(take.client_order_id, "venue-take")
    coordinator.apply_fill(
        stop.client_order_id,
        fill_id="stop-fill",
        quantity=Decimal("1"),
        price=Decimal("95"),
        fee=Decimal("0.095"),
    )
    assert coordinator.orders[take.client_order_id].status == "CANCEL_PENDING"

    with pytest.raises(UnexplainedEventError, match="reduce-only"):
        coordinator.apply_fill(
            take.client_order_id,
            fill_id="late-take-fill",
            quantity=Decimal("1"),
            price=Decimal("105"),
            fee=Decimal("0.105"),
        )

    assert coordinator.position(BTC).quantity == 0
    assert coordinator.fail_closed is True


def test_decision_persisted_without_intent_recovers_one_deterministic_order(
    coordinator,
) -> None:
    coordinator.persist_decision("decision-only", BTC, Decimal("1"))
    assert coordinator.orders == {}

    recovered = EventSourcedCoordinator.replay(
        ledger=coordinator.ledger,
        initial_wallet=Decimal("10000"),
    )
    order = recovered.request_target("decision-only", BTC, Decimal("1"))

    assert order is not None
    assert len(recovered.orders) == 1
    assert recovered.request_target("decision-only", BTC, Decimal("1")) is None


def test_fail_closed_state_refuses_new_target(coordinator) -> None:
    with pytest.raises(UnexplainedEventError):
        coordinator.apply_fill(
            "external-order",
            fill_id="external-fill",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
        )

    with pytest.raises(UnexplainedEventError, match="fail-closed"):
        coordinator.request_target("decision-after-anomaly", BTC, Decimal("1"))

    assert coordinator.orders == {}


def test_unknown_fill_is_persisted_and_fails_closed(coordinator) -> None:
    with pytest.raises(UnexplainedEventError, match="unknown order"):
        coordinator.apply_fill(
            "external-order",
            fill_id="external-fill",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
        )

    assert coordinator.fail_closed is True
    assert coordinator.ledger.read_all()[-1].event_type == "ANOMALY"


def test_replay_matches_runtime_snapshot_field_by_field(coordinator) -> None:
    open_long(coordinator)
    coordinator.mark_price(BTC, Decimal("105"))
    expected = coordinator.business_snapshot()

    replayed = EventSourcedCoordinator.replay(
        ledger=coordinator.ledger,
        initial_wallet=Decimal("10000"),
    )

    assert replayed.business_snapshot() == expected
    assert replayed.business_hash() == coordinator.business_hash()


def test_deterministic_event_timestamps_remain_valid_after_one_minute(
    coordinator,
) -> None:
    for index in range(65):
        coordinator.mark_price(BTC, Decimal("100") + index)

    timestamps = [
        datetime.fromisoformat(event.event_timestamp)
        for event in coordinator.ledger.read_all()
    ]

    assert timestamps == sorted(timestamps)
    assert timestamps[-1].minute >= 1
