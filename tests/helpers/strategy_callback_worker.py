from __future__ import annotations

import json
import os
import signal
import sys
from decimal import Decimal
from pathlib import Path

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import DurableInbox
from global_quant.gate1a.strategy import FixedTargetConfig
from global_quant.gate1a.strategy import FixedTargetStrategy


BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"


class KillAfterDurableAppend(DurableInbox):
    def append(self, payload: dict) -> None:
        super().append(payload)
        os.kill(os.getpid(), signal.SIGKILL)


def load_oracle(root: Path) -> dict:
    path = (
        root
        / "src/global_quant/gate1a/fixtures"
        / "nt_gate_1a_strategy_callback_oracle_v2.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["scenarios"]


def make_strategy(root: Path, output_root: Path) -> FixedTargetStrategy:
    btc_id = InstrumentId.from_str(BTC)
    eth_id = InstrumentId.from_str(ETH)
    strategy = FixedTargetStrategy(
        FixedTargetConfig(
            btc_instrument_id=btc_id,
            eth_instrument_id=eth_id,
            btc_bar_type=BarType.from_str(
                "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
            ),
            eth_bar_type=BarType.from_str(
                "ETHUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
            ),
            ledger_path=str(output_root / "events.jsonl"),
            initial_wallet=Decimal("10000"),
            source_hash="v1.2-source",
            config_hash="v1.2-config",
        ),
    )
    strategy._coordinator = EventSourcedCoordinator(
        ledger=AppendOnlyLedger(output_root / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        strategy_id="GATE1A",
        run_id="strategy-callback-worker",
        process_start_id=f"pid-{os.getpid()}",
        source_hash="v1.2-source",
        config_hash="v1.2-config",
    )
    return strategy


def seed_order(
    coordinator: EventSourcedCoordinator,
    *,
    client_order_id: str,
) -> None:
    decision_id = "real-strategy-fill-decision"
    coordinator.persist_decision(decision_id, BTC, Decimal("1"))
    event = coordinator._event(
        event_type="ORDER_INTENT",
        event_id=f"intent:{client_order_id}",
        source_event_id=f"intent:{client_order_id}",
        dedupe_key=f"intent:{client_order_id}",
        decision_id=decision_id,
        instrument_id=BTC,
        client_order_id=client_order_id,
        correlation_id=decision_id,
        causation_id=f"decision:{decision_id}:{BTC}",
        order_intent={
            "side": "BUY",
            "quantity": "1",
            "role": "TARGET",
            "reduce_only": False,
            "trigger_price": None,
        },
    )
    coordinator._append_and_reduce(event)
    coordinator.mark_submitted(client_order_id)
    coordinator.mark_accepted(client_order_id, "venue-1")


def make_fill(payload: dict) -> OrderFilled:
    return OrderFilled(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("GATE1A-001"),
        instrument_id=InstrumentId.from_str(BTC),
        client_order_id=ClientOrderId(payload["client_order_id"]),
        venue_order_id=VenueOrderId("venue-1"),
        account_id=AccountId("BINANCE-001"),
        trade_id=TradeId(payload["fill_id"]),
        position_id=None,
        order_side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        last_qty=Quantity.from_str(payload["quantity"]),
        last_px=Price.from_str(payload["price"]),
        currency=USDT,
        commission=Money.from_str(f"{payload['fee']} USDT"),
        liquidity_side=LiquiditySide.TAKER,
        event_id=UUID4(),
        ts_event=1,
        ts_init=1,
    )


def main() -> int:
    output_root = Path(sys.argv[1])
    mode = sys.argv[2]
    project_root = Path(__file__).resolve().parents[2]
    scenarios = load_oracle(project_root)
    strategy = make_strategy(project_root, output_root)

    if mode == "known_fill_crash":
        payload = scenarios["real_strategy_fill_crash_recovery"]
        seed_order(
            strategy._require_coordinator(),
            client_order_id=payload["client_order_id"],
        )
        strategy._execution_inbox = KillAfterDurableAppend(
            output_root / "events.inbox.jsonl",
        )
        strategy.on_order_filled(make_fill(payload))
        raise AssertionError("Strategy callback did not crash after durable append")

    if mode == "unknown_fill":
        payload = scenarios["real_strategy_unknown_fill"]
        strategy._execution_inbox = DurableInbox(
            output_root / "events.inbox.jsonl",
        )
        strategy.on_order_filled(make_fill(payload))
        raise AssertionError("unknown fill did not fail closed")

    raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
