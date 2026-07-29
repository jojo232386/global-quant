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


class FixedTargetConfig(StrategyConfig, frozen=True):
    btc_instrument_id: InstrumentId
    eth_instrument_id: InstrumentId
    btc_bar_type: BarType
    eth_bar_type: BarType
    btc_quantity: Decimal
    eth_quantity: Decimal
    ledger_path: str
    initial_wallet: Decimal
    source_hash: str
    config_hash: str
    decision_interval_bars: int = 2


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

    @classmethod
    def targets_for_step(cls, step: int) -> tuple[Decimal, Decimal]:
        return cls.TARGETS[step]

    def on_start(self) -> None:
        self._step = 0
        self._bar_count = 0
        ledger = AppendOnlyLedger(Path(self._gate_config.ledger_path))
        if ledger.read_all():
            self._coordinator = EventSourcedCoordinator.replay(
                ledger=ledger,
                initial_wallet=self._gate_config.initial_wallet,
            )
            if self._coordinator.fail_closed:
                raise RuntimeError("durable coordinator state is fail-closed")
            self._coordinator.reconcile_protection_quantities()
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
        self.subscribe_bars(self._gate_config.btc_bar_type)
        self.subscribe_bars(self._gate_config.eth_bar_type)

    def on_bar(self, bar: Bar) -> None:
        if self._require_coordinator().fail_closed:
            return
        if bar.bar_type != self._gate_config.btc_bar_type:
            return
        self._bar_count += 1
        if (self._bar_count - 1) % self._gate_config.decision_interval_bars:
            return
        if self._step >= len(self.TARGETS):
            return

        btc_weight, eth_weight = self.targets_for_step(self._step)
        decision_id = f"schedule-{self._step}"
        coordinator = self._require_coordinator()
        coordinator.request_target(
            decision_id,
            str(self._gate_config.btc_instrument_id),
            self._signed_quantity(btc_weight, self._gate_config.btc_quantity),
        )
        coordinator.request_target(
            decision_id,
            str(self._gate_config.eth_instrument_id),
            self._signed_quantity(eth_weight, self._gate_config.eth_quantity),
        )
        self._step += 1
        self._submit_pending_intents()

    @staticmethod
    def _signed_quantity(weight: Decimal, quantity: Decimal) -> Decimal:
        if weight == 0:
            return Decimal("0")
        return quantity if weight > 0 else -quantity

    def _submit_pending_intents(self) -> None:
        coordinator = self._require_coordinator()
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
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY if intent.side == "BUY" else OrderSide.SELL,
                quantity=instrument.make_qty(intent.quantity),
                reduce_only=intent.reduce_only,
                client_order_id=ClientOrderId(intent.client_order_id),
            )
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
        coordinator.apply_fill(
            str(event.client_order_id),
            fill_id=str(event.trade_id),
            quantity=event.last_qty.as_decimal(),
            price=event.last_px.as_decimal(),
            fee=event.commission.as_decimal(),
        )
        self._submit_pending_intents()

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
