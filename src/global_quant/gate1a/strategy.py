from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderAccepted
from nautilus_trader.model.events import OrderCancelRejected
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderDenied
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import CheckpointStore
from global_quant.gate1a.recovery import DurableInbox
from global_quant.gate1a.recovery import RecoveryAction
from global_quant.gate1a.recovery import RecoverySupervisor


class FixedTargetConfig(StrategyConfig, frozen=True):
    btc_instrument_id: InstrumentId
    eth_instrument_id: InstrumentId
    btc_bar_type: BarType
    eth_bar_type: BarType
    ledger_path: str
    initial_wallet: Decimal
    source_hash: str
    config_hash: str
    decision_interval_bars: int = 2
    protection_stop_fraction: Decimal = Decimal("0.20")
    protection_take_fraction: Decimal = Decimal("0.20")


class FixedTargetStrategy(Strategy):
    """No-alpha strategy shell with a deterministic target schedule."""

    TARGETS = (
        (Decimal("0"), Decimal("0")),
        (Decimal("0.1"), Decimal("-0.1")),
        (Decimal("0"), Decimal("0")),
        (Decimal("-0.1"), Decimal("0.1")),
        (Decimal("0"), Decimal("0")),
    )

    def __init__(self, config: FixedTargetConfig) -> None:
        super().__init__(config)
        self._gate_config = config
        self._step = 0
        self._bar_count = 0
        self._coordinator: EventSourcedCoordinator | None = None
        self._recovery_actions: tuple[RecoveryAction, ...] = ()
        self._reconciliation_required = False
        self._last_prices: dict[str, Decimal] = {}
        self._cancel_requests_sent: set[str] = set()
        self._modify_requests_sent: dict[str, Decimal] = {}
        self._execution_inbox = DurableInbox(
            Path(config.ledger_path).with_suffix(".inbox.jsonl"),
        )

    @classmethod
    def targets_for_step(cls, step: int) -> tuple[Decimal, Decimal]:
        return cls.TARGETS[step]

    def on_start(self) -> None:
        self._step = 0
        self._bar_count = 0
        self._last_prices = {}
        self._cancel_requests_sent = set()
        self._modify_requests_sent = {}
        ledger = AppendOnlyLedger(Path(self._gate_config.ledger_path))
        if ledger.read_all():
            recovery = RecoverySupervisor(
                ledger=ledger,
                initial_wallet=self._gate_config.initial_wallet,
                checkpoint_path=Path(self._gate_config.ledger_path).with_suffix(
                    ".checkpoint.json",
                ),
                inbox_path=Path(self._gate_config.ledger_path).with_suffix(
                    ".inbox.jsonl",
                ),
                expected_source_hash=self._gate_config.source_hash,
                expected_config_hash=self._gate_config.config_hash,
            ).recover()
            self._coordinator = recovery.coordinator
            self._recovery_actions = recovery.actions
            self._reconciliation_required = any(
                action.kind != "SUBMIT_ORDER"
                for action in recovery.actions
            )
        else:
            self._coordinator = EventSourcedCoordinator(
                ledger=ledger,
                initial_wallet=self._gate_config.initial_wallet,
                strategy_id="GATE1A",
                run_id=str(self.id),
                process_start_id=f"pid-{os.getpid()}",
                source_hash=self._gate_config.source_hash,
                config_hash=self._gate_config.config_hash,
            )
        self._step = self._next_schedule_step()
        self.subscribe_bars(self._gate_config.btc_bar_type)
        self.subscribe_bars(self._gate_config.eth_bar_type)
        if self._recovery_actions and not self._reconciliation_required:
            self._submit_pending_intents()

    def on_bar(self, bar: Bar) -> None:
        instrument_id = str(bar.bar_type.instrument_id)
        price = bar.close.as_decimal()
        self._last_prices[instrument_id] = price
        self._require_coordinator().mark_price(instrument_id, price)
        if (
            self._require_coordinator().fail_closed
            or self._reconciliation_required
        ):
            return
        if bar.bar_type != self._gate_config.btc_bar_type:
            return
        self._bar_count += 1
        if (self._bar_count - 1) % self._gate_config.decision_interval_bars:
            return
        if self._step >= len(self.TARGETS):
            return
        if not self._all_prices_available():
            return

        btc_weight, eth_weight = self.targets_for_step(self._step)
        decision_id = f"schedule-{self._step}"
        coordinator = self._require_coordinator()
        coordinator.request_target(
            decision_id,
            str(self._gate_config.btc_instrument_id),
            self._target_quantity(
                btc_weight,
                self._gate_config.btc_instrument_id,
            ),
        )
        coordinator.request_target(
            decision_id,
            str(self._gate_config.eth_instrument_id),
            self._target_quantity(
                eth_weight,
                self._gate_config.eth_instrument_id,
            ),
        )
        self._step += 1
        self._submit_pending_intents()

    def _target_quantity(
        self,
        weight: Decimal,
        instrument_id: InstrumentId,
    ) -> Decimal:
        if weight == 0:
            return Decimal("0")
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"instrument unavailable: {instrument_id}")
        price = self._last_prices[str(instrument_id)]
        raw_quantity = (
            self._gate_config.initial_wallet * abs(weight) / price
        )
        quantity = instrument.make_qty(raw_quantity).as_decimal()
        return quantity if weight > 0 else -quantity

    def _all_prices_available(self) -> bool:
        return all(
            str(instrument_id) in self._last_prices
            for instrument_id in (
                self._gate_config.btc_instrument_id,
                self._gate_config.eth_instrument_id,
            )
        )

    def _next_schedule_step(self) -> int:
        coordinator = self._require_coordinator()
        instruments = (
            str(self._gate_config.btc_instrument_id),
            str(self._gate_config.eth_instrument_id),
        )
        for step in range(len(self.TARGETS)):
            decision_id = f"schedule-{step}"
            if not all(
                (decision_id, instrument_id) in coordinator.decisions
                for instrument_id in instruments
            ):
                return step
        return len(self.TARGETS)

    def _submit_pending_intents(self) -> None:
        coordinator = self._require_coordinator()
        for intent in sorted(
            coordinator.active_orders(),
            key=lambda order: order.client_order_id,
        ):
            if intent.status != "CANCEL_PENDING":
                continue
            if intent.client_order_id in self._cancel_requests_sent:
                continue
            cached_order = self.cache.order(ClientOrderId(intent.client_order_id))
            if cached_order is None:
                self._reconciliation_required = True
                continue
            self._cancel_requests_sent.add(intent.client_order_id)
            self.cancel_order(cached_order)

        for intent in sorted(
            coordinator.active_orders(),
            key=lambda order: order.client_order_id,
        ):
            if (
                intent.role not in {"PROTECTION_STOP", "PROTECTION_TAKE"}
                or intent.status not in {"SUBMITTED", "ACCEPTED"}
            ):
                continue
            cached_order = self.cache.order(ClientOrderId(intent.client_order_id))
            if cached_order is None:
                continue
            desired = intent.quantity
            if (
                cached_order.quantity.as_decimal() == desired
                or self._modify_requests_sent.get(intent.client_order_id) == desired
            ):
                continue
            instrument = self.cache.instrument(
                InstrumentId.from_str(intent.instrument_id),
            )
            if instrument is None:
                raise RuntimeError(f"instrument unavailable: {intent.instrument_id}")
            self._modify_requests_sent[intent.client_order_id] = desired
            self.modify_order(
                cached_order,
                quantity=instrument.make_qty(desired),
            )

        for intent in sorted(
            coordinator.active_orders(),
            key=lambda order: order.client_order_id,
        ):
            if intent.status != "INTENT":
                continue
            instrument_id = InstrumentId.from_str(intent.instrument_id)
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                raise RuntimeError(f"instrument unavailable: {instrument_id}")
            common = {
                "instrument_id": instrument_id,
                "order_side": (
                    OrderSide.BUY if intent.side == "BUY" else OrderSide.SELL
                ),
                "quantity": instrument.make_qty(intent.quantity),
                "reduce_only": intent.reduce_only,
                "client_order_id": ClientOrderId(intent.client_order_id),
            }
            if intent.role == "PROTECTION_STOP":
                if intent.trigger_price is None:
                    raise RuntimeError("stop protection has no trigger price")
                order = self.order_factory.stop_market(
                    **common,
                    trigger_price=instrument.make_price(intent.trigger_price),
                )
            elif intent.role == "PROTECTION_TAKE":
                if intent.trigger_price is None:
                    raise RuntimeError("take protection has no trigger price")
                order = self.order_factory.market_if_touched(
                    **common,
                    trigger_price=instrument.make_price(intent.trigger_price),
                )
            else:
                order = self.order_factory.market(**common)
            coordinator.mark_submitted(
                intent.client_order_id,
                source_event_id=f"submit:{intent.client_order_id}",
            )
            self.submit_order(order)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._require_coordinator().mark_accepted(
            str(event.client_order_id),
            str(event.venue_order_id),
            source_event_id=f"accepted:{event.venue_order_id}",
        )

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._require_coordinator().mark_rejected(
            str(event.client_order_id),
            source_event_id=f"rejected:{event.id}",
        )

    def on_order_denied(self, event: OrderDenied) -> None:
        self._require_coordinator().mark_rejected(
            str(event.client_order_id),
            source_event_id=f"denied:{event.id}",
        )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._require_coordinator().mark_canceled(
            str(event.client_order_id),
            source_event_id=f"canceled:{event.id}",
        )

    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
        self._require_coordinator().mark_cancel_rejected(
            str(event.client_order_id),
            source_event_id=f"cancel-rejected:{event.id}",
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        coordinator = self._require_coordinator()
        client_order_id = str(event.client_order_id)
        fill_id = str(event.trade_id)
        quantity = event.last_qty.as_decimal()
        price = event.last_px.as_decimal()
        fee = event.commission.as_decimal()
        self._execution_inbox.append(
            {
                "event_type": "FILL",
                "source_event_id": fill_id,
                "client_order_id": client_order_id,
                "quantity": str(quantity),
                "price": str(price),
                "fee": str(fee),
            },
        )
        coordinator.apply_fill(
            client_order_id,
            fill_id=fill_id,
            quantity=quantity,
            price=price,
            fee=fee,
        )
        intent = coordinator.orders[client_order_id]
        if (
            intent.role not in {"PROTECTION_STOP", "PROTECTION_TAKE"}
            and coordinator.position(intent.instrument_id).quantity != 0
            and not self._has_live_protection(intent.instrument_id)
        ):
            self._create_protection(
                intent=intent,
                reference_price=event.last_px.as_decimal(),
            )
        self._submit_pending_intents()

    def _has_live_protection(self, instrument_id: str) -> bool:
        return any(
            order.instrument_id == instrument_id
            and order.protection_group_id is not None
            and order.status
            in {"INTENT", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}
            for order in self._require_coordinator().orders.values()
        )

    def _create_protection(
        self,
        *,
        intent,
        reference_price: Decimal,
    ) -> None:
        coordinator = self._require_coordinator()
        position = coordinator.position(intent.instrument_id)
        if position.quantity > 0:
            stop_price = reference_price * (
                Decimal("1") - self._gate_config.protection_stop_fraction
            )
            take_price = reference_price * (
                Decimal("1") + self._gate_config.protection_take_fraction
            )
        else:
            stop_price = reference_price * (
                Decimal("1") + self._gate_config.protection_stop_fraction
            )
            take_price = reference_price * (
                Decimal("1") - self._gate_config.protection_take_fraction
            )
        coordinator.create_protection_group(
            group_id=f"protect:{intent.decision_id}:{intent.instrument_id}",
            instrument_id=intent.instrument_id,
            quantity=abs(position.quantity),
            stop_trigger_price=stop_price,
            take_trigger_price=take_price,
        )

    def on_stop(self) -> None:
        coordinator = self._coordinator
        if coordinator is None:
            return
        checkpoint_path = Path(self._gate_config.ledger_path).with_suffix(
            ".checkpoint.json",
        )
        CheckpointStore(checkpoint_path).save(coordinator)

    def _require_coordinator(self) -> EventSourcedCoordinator:
        if self._coordinator is None:
            raise RuntimeError("strategy coordinator is not initialized")
        return self._coordinator

    @property
    def recovery_actions(self) -> tuple[RecoveryAction, ...]:
        return self._recovery_actions
