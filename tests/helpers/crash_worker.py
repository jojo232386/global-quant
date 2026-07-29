from __future__ import annotations

import hashlib
import os
import signal
import sys
from decimal import Decimal
from pathlib import Path

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import CheckpointStore
from global_quant.gate1a.recovery import DurableInbox


BTC = "BTCUSDT-PERP.BINANCE"
ROOT = Path(__file__).resolve().parents[2]
SOURCE_HASH = hashlib.sha256(
    (ROOT / "src/global_quant/gate1a/strategy.py").read_bytes(),
).hexdigest()
CONFIG_HASH = hashlib.sha256(
    (ROOT / "protocols/NT_GATE_1A.md").read_bytes(),
).hexdigest()


def kill_now() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def new_coordinator(root: Path) -> EventSourcedCoordinator:
    return EventSourcedCoordinator(
        ledger=AppendOnlyLedger(root / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        strategy_id="gate1a",
        run_id="crash-run",
        process_start_id=f"process-{os.getpid()}",
        source_hash=SOURCE_HASH,
        config_hash=CONFIG_HASH,
    )


def prepare_open_order(coordinator: EventSourcedCoordinator):
    order = coordinator.request_target("decision-1", BTC, Decimal("2"))
    if order is None:
        order = next(iter(coordinator.orders.values()))
    return order


def main() -> int:
    root = Path(sys.argv[1])
    phase = sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)

    if phase == "crash_during_replay":
        ledger = AppendOnlyLedger(root / "events.jsonl")
        EventSourcedCoordinator.replay(ledger=ledger, initial_wallet=Decimal("10000"))
        kill_now()

    coordinator = new_coordinator(root)
    order = prepare_open_order(coordinator)
    if phase == "decision_and_intent_persisted":
        kill_now()

    coordinator.mark_submitted(order.client_order_id)
    if phase in {"order_submitted", "submit_side_effect_unconfirmed"}:
        if phase == "submit_side_effect_unconfirmed":
            (root / "submit_side_effect.marker").write_text(
                order.client_order_id,
                encoding="utf-8",
            )
        kill_now()

    coordinator.mark_accepted(order.client_order_id, "venue-1")
    if phase == "order_acknowledged":
        kill_now()

    if phase == "execution_confirm_unpersisted":
        DurableInbox(root / "execution_inbox.jsonl").append(
            {
                "event_type": "FILL",
                "source_event_id": "pending-fill",
                "client_order_id": order.client_order_id,
                "quantity": "1",
                "price": "100",
                "fee": "0.1",
            },
        )
        kill_now()

    coordinator.apply_fill(
        order.client_order_id,
        fill_id="partial-fill",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.10"),
    )
    if phase == "partial_fill":
        kill_now()

    coordinator.request_cancel(order.client_order_id)
    if phase == "cancel_requested":
        kill_now()

    if phase in {
        "protection_update",
        "sibling_cancel_unpersisted",
        "ledger_before_checkpoint",
        "write_checkpoint",
    }:
        coordinator.mark_canceled(order.client_order_id)
        remainder = coordinator.request_target("decision-2", BTC, Decimal("1"))
        if remainder is not None:
            coordinator.mark_submitted(remainder.client_order_id)
            coordinator.mark_accepted(remainder.client_order_id, "venue-2")
            coordinator.apply_fill(
                remainder.client_order_id,
                fill_id="remainder-fill",
                quantity=remainder.quantity,
                price=Decimal("100"),
                fee=Decimal("0.10"),
            )
        stop, take = coordinator.create_protection_group(
            group_id="protect-1",
            instrument_id=BTC,
            quantity=Decimal("1"),
        )
        coordinator.mark_submitted(stop.client_order_id)
        coordinator.mark_accepted(stop.client_order_id, "venue-stop")
        coordinator.mark_submitted(take.client_order_id)
        coordinator.mark_accepted(take.client_order_id, "venue-take")

        if phase == "protection_update":
            kill_now()

        coordinator.apply_fill(
            stop.client_order_id,
            fill_id="stop-fill",
            quantity=Decimal("1"),
            price=Decimal("95"),
            fee=Decimal("0.095"),
        )
        if phase == "sibling_cancel_unpersisted":
            (root / "sibling_cancel.marker").write_text(
                take.client_order_id,
                encoding="utf-8",
            )
            kill_now()

        if phase == "ledger_before_checkpoint":
            kill_now()

        CheckpointStore(root / "checkpoint.json").save(coordinator)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
