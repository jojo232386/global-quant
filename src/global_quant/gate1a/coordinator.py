from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.ledger import LedgerEvent


ACTIVE_ORDER_STATES = {
    "INTENT",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
}
TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "REJECTED"}
VALID_ORDER_TRANSITIONS = {
    "INTENT": {"SUBMITTED", "REJECTED", "CANCELED"},
    "SUBMITTED": {"ACCEPTED", "REJECTED", "CANCEL_PENDING", "CANCELED"},
    "ACCEPTED": {"REJECTED", "CANCEL_PENDING", "CANCELED"},
    "PARTIALLY_FILLED": {"CANCEL_PENDING", "CANCELED"},
    "CANCEL_PENDING": {"ACCEPTED", "PARTIALLY_FILLED", "CANCELED"},
}


class UnexplainedEventError(RuntimeError):
    """Raised after an unknown economic event is durably recorded."""


@dataclass
class OrderState:
    client_order_id: str
    decision_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    role: str
    reduce_only: bool
    trigger_price: Decimal | None = None
    protection_group_id: str | None = None
    venue_order_id: str | None = None
    status: str = "INTENT"
    filled_quantity: Decimal = Decimal("0")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


@dataclass
class PositionState:
    instrument_id: str
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


class EventSourcedCoordinator:
    """Order-intent state machine backed by the durable event ledger."""

    def __init__(
        self,
        *,
        ledger: AppendOnlyLedger,
        initial_wallet: Decimal,
        strategy_id: str,
        run_id: str,
        process_start_id: str,
        source_hash: str,
        config_hash: str,
    ) -> None:
        self.ledger = ledger
        self.initial_wallet = Decimal(initial_wallet)
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.process_start_id = process_start_id
        self.source_hash = source_hash
        self.config_hash = config_hash
        self.orders: dict[str, OrderState] = {}
        self.positions: dict[str, PositionState] = {}
        self.marks: dict[str, Decimal] = {}
        self.decisions: dict[tuple[str, str], Decimal] = {}
        self.pending_targets: dict[str, tuple[str, Decimal]] = {}
        self.protection_groups: dict[str, set[str]] = {}
        self.realized_pnl = Decimal("0")
        self.cumulative_fees = Decimal("0")
        self.wallet_balance = self.initial_wallet
        self.fail_closed = False
        self._seen_fill_ids: set[str] = set()
        self._seen_source_event_ids: set[str] = set()
        self._applied_event_ids: set[str] = set()

    @staticmethod
    def _decimal(value: Decimal | str | int) -> Decimal:
        return Decimal(str(value))

    def _timestamp(self) -> str:
        sequence = self.ledger.next_sequence
        return f"2000-01-01T00:00:{sequence:02d}+00:00"

    def _event(
        self,
        *,
        event_type: str,
        event_id: str,
        decision_id: str | None = None,
        instrument_id: str | None = None,
        client_order_id: str | None = None,
        venue_order_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        source_event_id: str | None = None,
        dedupe_key: str | None = None,
        order_intent: dict[str, Any] | None = None,
        order_transition: dict[str, Any] | None = None,
        fill: dict[str, Any] | None = None,
        fee: Decimal | None = None,
        position_transition: dict[str, Any] | None = None,
        balance_transition: dict[str, Any] | None = None,
        protection_group_id: str | None = None,
        persistence_checkpoint: dict[str, Any] | None = None,
    ) -> LedgerEvent:
        timestamp = self._timestamp()
        return LedgerEvent(
            decision_id=decision_id,
            strategy_id=self.strategy_id,
            run_id=self.run_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            position_id=f"position:{instrument_id}" if instrument_id else None,
            correlation_id=correlation_id,
            causation_id=causation_id,
            event_id=event_id,
            source_event_id=source_event_id,
            dedupe_key=dedupe_key,
            event_sequence=self.ledger.next_sequence,
            event_timestamp=timestamp,
            receive_timestamp=timestamp,
            event_type=event_type,
            order_intent=order_intent,
            order_transition=order_transition,
            fill=fill,
            fee=str(fee) if fee is not None else None,
            position_transition=position_transition,
            balance_transition=balance_transition,
            protection_group_id=protection_group_id,
            persistence_checkpoint=persistence_checkpoint,
            process_start_id=self.process_start_id,
            source_hash=self.source_hash,
            config_hash=self.config_hash,
        )

    def _append_and_reduce(self, event: LedgerEvent) -> bool:
        appended = self.ledger.append(event)
        if appended:
            self._reduce(event)
        return appended

    def _deterministic_order_id(
        self,
        decision_id: str,
        instrument_id: str,
        role: str,
    ) -> str:
        raw = f"{self.strategy_id}|{decision_id}|{instrument_id}|{role}"
        return "G1A-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    def position(self, instrument_id: str) -> PositionState:
        return self.positions.setdefault(instrument_id, PositionState(instrument_id))

    def request_target(
        self,
        decision_id: str,
        instrument_id: str,
        target_quantity: Decimal,
    ) -> OrderState | None:
        if self.fail_closed:
            raise UnexplainedEventError("coordinator is fail-closed")
        target = self._decimal(target_quantity)
        decision_key = (decision_id, instrument_id)
        if decision_key in self.decisions:
            if self.decisions[decision_key] != target:
                self._record_anomaly(
                    f"decision {decision_id} changed target for {instrument_id}",
                    decision_id=decision_id,
                    instrument_id=instrument_id,
                )
                raise UnexplainedEventError("decision target changed")
            relevant_orders = [
                order
                for order in self.orders.values()
                if order.decision_id == decision_id
                and order.instrument_id == instrument_id
            ]
            if not relevant_orders:
                return self._plan_target(decision_id, instrument_id, target)
            current = self.position(instrument_id).quantity
            if current != target and not any(
                order.status in ACTIVE_ORDER_STATES
                for order in relevant_orders
            ):
                reversal_closed = any(
                    order.role == "REVERSAL_CLOSE" and order.status == "FILLED"
                    for order in relevant_orders
                )
                reversal_opened = any(
                    order.role == "REVERSAL_OPEN"
                    for order in relevant_orders
                )
                if reversal_closed and not reversal_opened and current == 0:
                    return self._create_order(
                        decision_id=decision_id,
                        instrument_id=instrument_id,
                        side="BUY" if target > 0 else "SELL",
                        quantity=abs(target),
                        role="REVERSAL_OPEN",
                        reduce_only=False,
                    )
            return None

        self.persist_decision(decision_id, instrument_id, target)
        return self._plan_target(decision_id, instrument_id, target)

    def persist_decision(
        self,
        decision_id: str,
        instrument_id: str,
        target_quantity: Decimal,
    ) -> bool:
        if self.fail_closed:
            raise UnexplainedEventError("coordinator is fail-closed")
        target = self._decimal(target_quantity)
        decision_key = (decision_id, instrument_id)
        existing = self.decisions.get(decision_key)
        if existing is not None:
            if existing != target:
                self._record_anomaly(
                    f"decision {decision_id} changed target for {instrument_id}",
                    decision_id=decision_id,
                    instrument_id=instrument_id,
                )
                raise UnexplainedEventError("decision target changed")
            return False
        decision_event = self._event(
            event_type="DECISION",
            event_id=f"decision:{decision_id}:{instrument_id}",
            decision_id=decision_id,
            instrument_id=instrument_id,
            correlation_id=decision_id,
            causation_id=decision_id,
            dedupe_key=f"decision:{decision_id}:{instrument_id}",
            order_intent={"target_quantity": str(target)},
        )
        self._append_and_reduce(decision_event)
        return True

    def _plan_target(
        self,
        decision_id: str,
        instrument_id: str,
        target: Decimal,
    ) -> OrderState | None:
        current = self.position(instrument_id).quantity
        if current == target:
            return None

        if current and target and (current > 0) != (target > 0):
            self.pending_targets[instrument_id] = (decision_id, target)
            side = "SELL" if current > 0 else "BUY"
            return self._create_order(
                decision_id=decision_id,
                instrument_id=instrument_id,
                side=side,
                quantity=abs(current),
                role="REVERSAL_CLOSE",
                reduce_only=True,
            )

        delta = target - current
        return self._create_order(
            decision_id=decision_id,
            instrument_id=instrument_id,
            side="BUY" if delta > 0 else "SELL",
            quantity=abs(delta),
            role="TARGET",
            reduce_only=target == 0 or abs(target) < abs(current),
        )

    def _create_order(
        self,
        *,
        decision_id: str,
        instrument_id: str,
        side: str,
        quantity: Decimal,
        role: str,
        reduce_only: bool,
        protection_group_id: str | None = None,
        trigger_price: Decimal | None = None,
    ) -> OrderState:
        client_order_id = self._deterministic_order_id(
            decision_id,
            instrument_id,
            role,
        )
        if client_order_id in self.orders:
            return self.orders[client_order_id]
        event = self._event(
            event_type="ORDER_INTENT",
            event_id=f"intent:{client_order_id}",
            source_event_id=f"intent:{client_order_id}",
            dedupe_key=f"intent:{client_order_id}",
            decision_id=decision_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            correlation_id=decision_id,
            causation_id=f"decision:{decision_id}:{instrument_id}",
            order_intent={
                "side": side,
                "quantity": str(quantity),
                "role": role,
                "reduce_only": reduce_only,
                "trigger_price": (
                    str(trigger_price) if trigger_price is not None else None
                ),
            },
            protection_group_id=protection_group_id,
        )
        self._append_and_reduce(event)
        return self.orders[client_order_id]

    def _transition(
        self,
        client_order_id: str,
        *,
        status: str,
        venue_order_id: str | None = None,
        source_event_id: str | None = None,
    ) -> bool:
        order = self.orders.get(client_order_id)
        if order is None:
            self._record_anomaly(f"unknown order {client_order_id}")
            raise UnexplainedEventError(f"unknown order {client_order_id}")
        source_id = source_event_id or (
            f"{status}:{client_order_id}:{self.ledger.next_sequence}"
        )
        if source_id in self._seen_source_event_ids:
            return False
        if order.status in TERMINAL_ORDER_STATES:
            event = self._event(
                event_type="STALE_ORDER_EVENT",
                event_id=f"stale-transition:{source_id}",
                source_event_id=source_id,
                dedupe_key=f"stale-transition:{source_id}",
                decision_id=order.decision_id,
                instrument_id=order.instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id or order.venue_order_id,
                correlation_id=order.decision_id,
                causation_id=f"intent:{client_order_id}",
                order_transition={"from": order.status, "attempted": status},
                protection_group_id=order.protection_group_id,
            )
            return self._append_and_reduce(event)
        allowed = VALID_ORDER_TRANSITIONS.get(order.status, set())
        if status not in allowed:
            self._record_anomaly(
                f"invalid order transition {order.status}->{status} for {client_order_id}",
                decision_id=order.decision_id,
                instrument_id=order.instrument_id,
                source_event_id=source_id,
            )
            raise UnexplainedEventError("invalid order transition")
        event = self._event(
            event_type="ORDER_TRANSITION",
            event_id=f"transition:{source_id}",
            source_event_id=source_id,
            dedupe_key=f"transition:{source_id}",
            decision_id=order.decision_id,
            instrument_id=order.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id or order.venue_order_id,
            correlation_id=order.decision_id,
            causation_id=f"intent:{client_order_id}",
            order_transition={"from": order.status, "to": status},
            protection_group_id=order.protection_group_id,
        )
        return self._append_and_reduce(event)

    def mark_submitted(
        self,
        client_order_id: str,
        source_event_id: str | None = None,
    ) -> bool:
        return self._transition(
            client_order_id,
            status="SUBMITTED",
            source_event_id=source_event_id,
        )

    def mark_accepted(
        self,
        client_order_id: str,
        venue_order_id: str,
        source_event_id: str | None = None,
    ) -> bool:
        return self._transition(
            client_order_id,
            status="ACCEPTED",
            venue_order_id=venue_order_id,
            source_event_id=source_event_id,
        )

    def mark_rejected(
        self,
        client_order_id: str,
        source_event_id: str | None = None,
    ) -> bool:
        return self._transition(
            client_order_id,
            status="REJECTED",
            source_event_id=source_event_id,
        )

    def request_cancel(self, client_order_id: str) -> bool:
        order = self.orders[client_order_id]
        if order.status not in ACTIVE_ORDER_STATES or order.status == "CANCEL_PENDING":
            return False
        return self._transition(client_order_id, status="CANCEL_PENDING")

    def mark_canceled(
        self,
        client_order_id: str,
        source_event_id: str | None = None,
    ) -> bool:
        return self._transition(
            client_order_id,
            status="CANCELED",
            source_event_id=source_event_id,
        )

    def mark_cancel_rejected(
        self,
        client_order_id: str,
        source_event_id: str | None = None,
    ) -> bool:
        order = self.orders[client_order_id]
        restore = "PARTIALLY_FILLED" if order.filled_quantity else "ACCEPTED"
        return self._transition(
            client_order_id,
            status=restore,
            source_event_id=source_event_id,
        )

    def apply_fill(
        self,
        client_order_id: str,
        *,
        fill_id: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
    ) -> bool:
        order = self.orders.get(client_order_id)
        if order is None:
            self._record_anomaly(
                f"fill {fill_id} references unknown order {client_order_id}",
                source_event_id=fill_id,
            )
            raise UnexplainedEventError(f"fill references unknown order {client_order_id}")
        if fill_id in self._seen_fill_ids:
            return False

        fill_quantity = self._decimal(quantity)
        fill_price = self._decimal(price)
        fill_fee = self._decimal(fee)
        if fill_quantity <= 0 or fill_quantity > order.remaining_quantity:
            self._record_anomaly(
                f"invalid fill quantity {fill_quantity} for {client_order_id}",
                source_event_id=fill_id,
            )
            raise UnexplainedEventError("invalid fill quantity")

        position_before = self.position(order.instrument_id)
        if order.reduce_only and (
            position_before.quantity == 0
            or (order.side == "SELL" and position_before.quantity < 0)
            or (order.side == "BUY" and position_before.quantity > 0)
            or fill_quantity > abs(position_before.quantity)
        ):
            self._record_anomaly(
                f"reduce-only fill {fill_id} would increase risk",
                decision_id=order.decision_id,
                instrument_id=order.instrument_id,
                source_event_id=fill_id,
            )
            raise UnexplainedEventError("reduce-only fill would increase risk")
        signed_fill = fill_quantity if order.side == "BUY" else -fill_quantity
        event = self._event(
            event_type="FILL",
            event_id=f"fill:{fill_id}",
            source_event_id=fill_id,
            dedupe_key=f"fill:{fill_id}",
            decision_id=order.decision_id,
            instrument_id=order.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=order.venue_order_id,
            correlation_id=order.decision_id,
            causation_id=f"intent:{client_order_id}",
            fill={
                "fill_id": fill_id,
                "side": order.side,
                "quantity": str(fill_quantity),
                "signed_quantity": str(signed_fill),
                "price": str(fill_price),
            },
            fee=fill_fee,
            position_transition={
                "quantity_before": str(position_before.quantity),
                "signed_fill_quantity": str(signed_fill),
            },
            balance_transition={"fee": str(fill_fee)},
            protection_group_id=order.protection_group_id,
        )
        appended = self._append_and_reduce(event)
        if not appended:
            return False

        if order.protection_group_id:
            if order.status == "FILLED":
                self._cancel_protection_siblings(order)
        elif self.position(order.instrument_id).quantity != 0:
            self._resize_protection_orders(order.instrument_id)
        if self.position(order.instrument_id).quantity == 0:
            self._cancel_orphan_protection(order.instrument_id)
        if order.role == "REVERSAL_CLOSE" and order.status == "FILLED":
            pending = self.pending_targets.pop(order.instrument_id, None)
            if pending is not None:
                decision_id, target = pending
                self._create_order(
                    decision_id=decision_id,
                    instrument_id=order.instrument_id,
                    side="BUY" if target > 0 else "SELL",
                    quantity=abs(target),
                    role="REVERSAL_OPEN",
                    reduce_only=False,
                )
        return True

    def create_protection_group(
        self,
        *,
        group_id: str,
        instrument_id: str,
        quantity: Decimal,
        stop_trigger_price: Decimal | None = None,
        take_trigger_price: Decimal | None = None,
    ) -> tuple[OrderState, OrderState]:
        position = self.position(instrument_id)
        if position.quantity == 0:
            raise ValueError("cannot protect a flat position")
        protected_quantity = min(abs(position.quantity), self._decimal(quantity))
        side = "SELL" if position.quantity > 0 else "BUY"
        decision_id = f"protection:{group_id}"
        stop = self._create_order(
            decision_id=decision_id,
            instrument_id=instrument_id,
            side=side,
            quantity=protected_quantity,
            role="PROTECTION_STOP",
            reduce_only=True,
            protection_group_id=group_id,
            trigger_price=stop_trigger_price,
        )
        take = self._create_order(
            decision_id=decision_id,
            instrument_id=instrument_id,
            side=side,
            quantity=protected_quantity,
            role="PROTECTION_TAKE",
            reduce_only=True,
            protection_group_id=group_id,
            trigger_price=take_trigger_price,
        )
        self.protection_groups[group_id] = {
            stop.client_order_id,
            take.client_order_id,
        }
        return stop, take

    def _resize_protection_orders(self, instrument_id: str) -> None:
        position_quantity = abs(self.position(instrument_id).quantity)
        if position_quantity == 0:
            return
        for order in sorted(
            self.orders.values(),
            key=lambda value: value.client_order_id,
        ):
            if (
                order.instrument_id != instrument_id
                or not order.protection_group_id
                or order.status not in ACTIVE_ORDER_STATES
            ):
                continue
            desired_quantity = order.filled_quantity + position_quantity
            if desired_quantity == order.quantity:
                continue
            event = self._event(
                event_type="PROTECTION_RESIZE",
                event_id=(
                    f"resize:{order.client_order_id}:{desired_quantity}:"
                    f"{self.ledger.next_sequence}"
                ),
                source_event_id=(
                    f"resize:{order.client_order_id}:{desired_quantity}:"
                    f"{self.ledger.next_sequence}"
                ),
                dedupe_key=(
                    f"resize:{order.client_order_id}:{desired_quantity}:"
                    f"{self.ledger.next_sequence}"
                ),
                decision_id=order.decision_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                correlation_id=order.protection_group_id,
                causation_id=f"position:{instrument_id}",
                order_transition={
                    "from_quantity": str(order.quantity),
                    "to_quantity": str(desired_quantity),
                    "status": order.status,
                },
                protection_group_id=order.protection_group_id,
            )
            self._append_and_reduce(event)

    def reconcile_protection_quantities(self) -> None:
        if self.fail_closed:
            raise UnexplainedEventError("coordinator is fail-closed")
        for instrument_id in sorted(self.positions):
            self._resize_protection_orders(instrument_id)

    def _cancel_protection_siblings(self, filled_order: OrderState) -> None:
        group = self.protection_groups.get(filled_order.protection_group_id or "", set())
        for sibling_id in sorted(group - {filled_order.client_order_id}):
            sibling = self.orders[sibling_id]
            if sibling.status in ACTIVE_ORDER_STATES and sibling.status != "CANCEL_PENDING":
                self.request_cancel(sibling_id)

    def _cancel_orphan_protection(self, instrument_id: str) -> None:
        for order in sorted(self.orders.values(), key=lambda value: value.client_order_id):
            if (
                order.instrument_id == instrument_id
                and order.protection_group_id
                and order.status in ACTIVE_ORDER_STATES
                and order.status != "CANCEL_PENDING"
            ):
                self.request_cancel(order.client_order_id)

    def mark_price(self, instrument_id: str, price: Decimal) -> None:
        mark = self._decimal(price)
        event = self._event(
            event_type="MARK_PRICE",
            event_id=f"mark:{instrument_id}:{self.ledger.next_sequence}",
            source_event_id=f"mark:{instrument_id}:{self.ledger.next_sequence}",
            dedupe_key=f"mark:{instrument_id}:{self.ledger.next_sequence}",
            instrument_id=instrument_id,
            fill={"price": str(mark)},
        )
        self._append_and_reduce(event)

    def reconcile_account_snapshot(
        self,
        *,
        source_event_id: str,
        wallet_balance: Decimal,
        positions: dict[str, Decimal],
    ) -> bool:
        if source_event_id in self._seen_source_event_ids:
            return False
        observed_wallet = self._decimal(wallet_balance)
        observed_positions = {
            instrument_id: self._decimal(quantity)
            for instrument_id, quantity in positions.items()
        }
        expected_positions = {
            instrument_id: position.quantity
            for instrument_id, position in self.positions.items()
            if position.quantity != 0 or instrument_id in observed_positions
        }
        if (
            observed_wallet != self.wallet_balance
            or observed_positions != expected_positions
        ):
            self._record_anomaly(
                "account snapshot mismatch",
                source_event_id=source_event_id,
            )
            raise UnexplainedEventError("account snapshot mismatch")
        event = self._event(
            event_type="RECONCILIATION",
            event_id=f"reconciliation:{source_event_id}",
            source_event_id=source_event_id,
            dedupe_key=f"reconciliation:{source_event_id}",
            correlation_id=source_event_id,
            causation_id=source_event_id,
            balance_transition={
                "wallet_balance": str(observed_wallet),
                "positions": {
                    key: str(value)
                    for key, value in sorted(observed_positions.items())
                },
            },
        )
        return self._append_and_reduce(event)

    def _record_anomaly(
        self,
        message: str,
        *,
        decision_id: str | None = None,
        instrument_id: str | None = None,
        source_event_id: str | None = None,
    ) -> None:
        event = self._event(
            event_type="ANOMALY",
            event_id=f"anomaly:{hashlib.sha256(message.encode()).hexdigest()[:24]}",
            source_event_id=source_event_id,
            dedupe_key=f"anomaly:{source_event_id or message}",
            decision_id=decision_id,
            instrument_id=instrument_id,
            fill={"message": message},
        )
        self._append_and_reduce(event)

    def _reduce(self, event: LedgerEvent) -> None:
        if event.event_id in self._applied_event_ids:
            return
        self._applied_event_ids.add(event.event_id)
        if event.source_event_id:
            self._seen_source_event_ids.add(event.source_event_id)
        if event.event_type == "DECISION":
            assert event.decision_id and event.instrument_id and event.order_intent
            self.decisions[(event.decision_id, event.instrument_id)] = Decimal(
                event.order_intent["target_quantity"],
            )
            return
        if event.event_type == "ORDER_INTENT":
            assert event.client_order_id and event.instrument_id and event.order_intent
            self.orders[event.client_order_id] = OrderState(
                client_order_id=event.client_order_id,
                decision_id=event.decision_id or "",
                instrument_id=event.instrument_id,
                side=event.order_intent["side"],
                quantity=Decimal(event.order_intent["quantity"]),
                role=event.order_intent["role"],
                reduce_only=bool(event.order_intent["reduce_only"]),
                trigger_price=(
                    Decimal(event.order_intent["trigger_price"])
                    if event.order_intent.get("trigger_price") is not None
                    else None
                ),
                protection_group_id=event.protection_group_id,
            )
            if event.protection_group_id:
                self.protection_groups.setdefault(event.protection_group_id, set()).add(
                    event.client_order_id,
                )
            if event.order_intent["role"] == "REVERSAL_CLOSE":
                target = self.decisions.get((event.decision_id or "", event.instrument_id))
                if target is None:
                    raise UnexplainedEventError(
                        "reversal close has no durable target decision",
                    )
                self.pending_targets[event.instrument_id] = (
                    event.decision_id or "",
                    target,
                )
            return
        if event.event_type == "ORDER_TRANSITION":
            assert event.client_order_id and event.order_transition
            order = self.orders[event.client_order_id]
            next_status = event.order_transition["to"]
            if event.order_transition["from"] != order.status:
                raise UnexplainedEventError("order transition source state mismatch")
            if next_status not in VALID_ORDER_TRANSITIONS.get(order.status, set()):
                raise UnexplainedEventError("invalid durable order transition")
            order.status = next_status
            if event.venue_order_id:
                order.venue_order_id = event.venue_order_id
            return
        if event.event_type in {"STALE_ORDER_EVENT", "RECONCILIATION"}:
            return
        if event.event_type == "PROTECTION_RESIZE":
            assert event.client_order_id and event.order_transition
            order = self.orders[event.client_order_id]
            resized = Decimal(event.order_transition["to_quantity"])
            if resized < order.filled_quantity:
                raise UnexplainedEventError(
                    "protection resize violates filled quantity",
                )
            order.quantity = resized
            return
        if event.event_type == "FILL":
            assert event.client_order_id and event.fill and event.fee is not None
            order = self.orders[event.client_order_id]
            fill_id = event.fill["fill_id"]
            if fill_id in self._seen_fill_ids:
                return
            self._seen_fill_ids.add(fill_id)
            quantity = Decimal(event.fill["quantity"])
            price = Decimal(event.fill["price"])
            fee = Decimal(event.fee)
            signed_quantity = quantity if order.side == "BUY" else -quantity
            position = self.position(order.instrument_id)
            if (
                event.fill.get("signed_quantity") != str(signed_quantity)
                or event.position_transition is None
                or event.position_transition.get("quantity_before")
                != str(position.quantity)
                or event.position_transition.get("signed_fill_quantity")
                != str(signed_quantity)
                or event.balance_transition is None
                or event.balance_transition.get("fee") != str(fee)
            ):
                raise UnexplainedEventError("fill economic transition mismatch")
            self._apply_account_fill(order, quantity, price, fee)
            order.filled_quantity += quantity
            order.status = (
                "FILLED"
                if order.filled_quantity == order.quantity
                else "PARTIALLY_FILLED"
            )
            return
        if event.event_type == "MARK_PRICE":
            assert event.instrument_id and event.fill
            self.marks[event.instrument_id] = Decimal(event.fill["price"])
            return
        if event.event_type == "ANOMALY":
            self.fail_closed = True
            return
        raise UnexplainedEventError(f"unknown ledger event type {event.event_type}")

    def _apply_account_fill(
        self,
        order: OrderState,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
    ) -> None:
        position = self.position(order.instrument_id)
        old_quantity = position.quantity
        signed_fill = quantity if order.side == "BUY" else -quantity
        new_quantity = old_quantity + signed_fill

        if old_quantity == 0 or (old_quantity > 0) == (signed_fill > 0):
            total_notional = (
                abs(old_quantity) * position.average_price + quantity * price
            )
            position.average_price = total_notional / abs(new_quantity)
        else:
            closed_quantity = min(abs(old_quantity), quantity)
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            self.realized_pnl += (
                price - position.average_price
            ) * closed_quantity * direction
            if new_quantity == 0:
                position.average_price = Decimal("0")
            elif (new_quantity > 0) != (old_quantity > 0):
                position.average_price = price

        position.quantity = new_quantity
        self.marks[order.instrument_id] = price
        self.cumulative_fees += fee
        self.wallet_balance = (
            self.initial_wallet + self.realized_pnl - self.cumulative_fees
        )

    def active_orders(self, instrument_id: str | None = None) -> list[OrderState]:
        return [
            order
            for order in self.orders.values()
            if order.status in ACTIVE_ORDER_STATES
            and (instrument_id is None or order.instrument_id == instrument_id)
        ]

    def unrealized_pnl(self) -> Decimal:
        total = Decimal("0")
        for instrument_id, position in self.positions.items():
            if position.quantity == 0:
                continue
            mark = self.marks.get(instrument_id, position.average_price)
            total += position.quantity * (mark - position.average_price)
        return total

    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl()

    def assert_invariants(self) -> None:
        expected_wallet = (
            self.initial_wallet + self.realized_pnl - self.cumulative_fees
        )
        if self.wallet_balance != expected_wallet:
            raise AssertionError("wallet accounting identity failed")
        if self.equity() != self.wallet_balance + self.unrealized_pnl():
            raise AssertionError("equity accounting identity failed")
        for order in self.orders.values():
            if not Decimal("0") <= order.filled_quantity <= order.quantity:
                raise AssertionError("order quantity conservation failed")
            if (
                order.protection_group_id
                and self.position(order.instrument_id).quantity == 0
                and order.status in ACTIVE_ORDER_STATES
                and order.status != "CANCEL_PENDING"
            ):
                raise AssertionError("flat position has live protection")

    def business_snapshot(self) -> dict[str, Any]:
        return {
            "initial_wallet": str(self.initial_wallet),
            "wallet_balance": str(self.wallet_balance),
            "realized_pnl": str(self.realized_pnl),
            "cumulative_fees": str(self.cumulative_fees),
            "unrealized_pnl": str(self.unrealized_pnl()),
            "equity": str(self.equity()),
            "fail_closed": self.fail_closed,
            "orders": {
                key: {
                    "decision_id": value.decision_id,
                    "instrument_id": value.instrument_id,
                    "side": value.side,
                    "quantity": str(value.quantity),
                    "filled_quantity": str(value.filled_quantity),
                    "role": value.role,
                    "reduce_only": value.reduce_only,
                    "trigger_price": (
                        str(value.trigger_price)
                        if value.trigger_price is not None
                        else None
                    ),
                    "status": value.status,
                    "venue_order_id": value.venue_order_id,
                    "protection_group_id": value.protection_group_id,
                }
                for key, value in sorted(self.orders.items())
            },
            "positions": {
                key: {
                    "quantity": str(value.quantity),
                    "average_price": str(value.average_price),
                }
                for key, value in sorted(self.positions.items())
            },
            "marks": {key: str(value) for key, value in sorted(self.marks.items())},
            "decisions": {
                f"{decision_id}|{instrument_id}": str(target)
                for (decision_id, instrument_id), target in sorted(self.decisions.items())
            },
            "pending_targets": {
                key: [value[0], str(value[1])]
                for key, value in sorted(self.pending_targets.items())
            },
            "protection_groups": {
                key: sorted(value)
                for key, value in sorted(self.protection_groups.items())
            },
        }

    def business_hash(self) -> str:
        payload = json.dumps(
            self.business_snapshot(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def replay(
        cls,
        *,
        ledger: AppendOnlyLedger,
        initial_wallet: Decimal,
    ) -> EventSourcedCoordinator:
        events = ledger.read_all()
        if not events:
            raise ValueError("cannot replay an empty ledger")
        first = events[0]
        replayed = cls(
            ledger=ledger,
            initial_wallet=initial_wallet,
            strategy_id=first.strategy_id,
            run_id=first.run_id,
            process_start_id=first.process_start_id,
            source_hash=first.source_hash,
            config_hash=first.config_hash,
        )
        for event in events:
            replayed._reduce(event)
        replayed.assert_invariants()
        return replayed

    def write_snapshot(self, path: Path | str) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_ledger_sequence": len(self.ledger.read_all()),
            "last_event_hash": self.ledger.last_event_hash,
            "business_snapshot": self.business_snapshot(),
            "business_hash": self.business_hash(),
        }
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
