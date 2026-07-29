from __future__ import annotations

import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.strategy import FixedTargetConfig
from global_quant.gate1a.strategy import FixedTargetStrategy


ROOT = Path(__file__).resolve().parents[2]


def make_bars(instrument, bar_type, start_price: float):
    prices = start_price + np.arange(18, dtype=float) * 0.1
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": prices + 0.2,
            "low": prices - 0.2,
            "close": prices + 0.05,
        },
        index=pd.date_range("2026-01-01", periods=len(prices), freq="1min", tz="UTC"),
    )
    return BarDataWrangler(bar_type, instrument).process(frame)


def main() -> int:
    output_root = Path(sys.argv[1])
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    btc_bar_type = BarType.from_str(
        "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
    )
    eth_bar_type = BarType.from_str(
        "ETHUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
    )
    ledger_path = output_root / "uncertain-events.jsonl"
    source_hash = hashlib.sha256(
        (ROOT / "src/global_quant/gate1a/strategy.py").read_bytes(),
    ).hexdigest()
    config_hash = hashlib.sha256(
        (ROOT / "protocols/NT_GATE_1A.md").read_bytes(),
    ).hexdigest()
    coordinator = EventSourcedCoordinator(
        ledger=AppendOnlyLedger(ledger_path),
        initial_wallet=Decimal("10000"),
        strategy_id="GATE1A",
        run_id="crashed-run",
        process_start_id="crashed-process",
        source_hash=source_hash,
        config_hash=config_hash,
    )
    uncertain = coordinator.request_target(
        "precrash-decision",
        str(btc.id),
        Decimal("0.02"),
    )
    assert uncertain is not None
    coordinator.mark_submitted(
        uncertain.client_order_id,
        source_event_id="submit-side-effect-before-crash",
    )

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(log_level="ERROR"),
        ),
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
        base_currency=USDT,
        default_leverage=Decimal("1"),
    )
    engine.add_instrument(btc)
    engine.add_instrument(eth)
    engine.add_data(make_bars(btc, btc_bar_type, 50_000.0))
    engine.add_data(make_bars(eth, eth_bar_type, 3_000.0))
    strategy = FixedTargetStrategy(
        FixedTargetConfig(
            btc_instrument_id=btc.id,
            eth_instrument_id=eth.id,
            btc_bar_type=btc_bar_type,
            eth_bar_type=eth_bar_type,
            decision_interval_bars=2,
            ledger_path=str(ledger_path),
            initial_wallet=Decimal("10000"),
            source_hash=source_hash,
            config_hash=config_hash,
        ),
    )
    engine.add_strategy(strategy)
    engine.run()

    assert {action.kind for action in strategy.recovery_actions} == {
        "RECONCILE_ORDER",
    }
    assert engine.cache.orders() == []
    recovered = EventSourcedCoordinator.replay(
        ledger=AppendOnlyLedger(ledger_path),
        initial_wallet=Decimal("10000"),
    )
    assert set(recovered.decisions) == {
        ("precrash-decision", str(btc.id)),
    }
    assert list(recovered.orders) == [uncertain.client_order_id]
    assert recovered.orders[uncertain.client_order_id].status == "SUBMITTED"
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
