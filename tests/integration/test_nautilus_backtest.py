from __future__ import annotations

import hashlib
import os
import subprocess
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


def test_same_strategy_source_runs_real_nautilus_backtest_and_finishes_flat(
    tmp_path,
) -> None:
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    btc_bar_type = BarType.from_str(
        "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
    )
    eth_bar_type = BarType.from_str(
        "ETHUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
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

    ledger_path = tmp_path / "nautilus-events.jsonl"
    source_hash = hashlib.sha256(
        (ROOT / "src/global_quant/gate1a/strategy.py").read_bytes(),
    ).hexdigest()
    config_hash = hashlib.sha256(
        (ROOT / "protocols/NT_GATE_1A.md").read_bytes(),
    ).hexdigest()
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

    ledger = AppendOnlyLedger(ledger_path)
    assert all(event.source_hash == source_hash for event in ledger.read_all())
    assert all(event.config_hash == config_hash for event in ledger.read_all())
    replayed = EventSourcedCoordinator.replay(
        ledger=ledger,
        initial_wallet=Decimal("10000"),
    )
    replayed.assert_invariants()
    assert replayed.position(str(btc.id)).quantity == 0
    assert replayed.position(str(eth.id)).quantity == 0
    assert len(replayed.decisions) == 10
    assert {order.side for order in replayed.orders.values()} == {"BUY", "SELL"}
    assert {
        order.role for order in replayed.orders.values()
    } == {"TARGET", "PROTECTION_STOP", "PROTECTION_TAKE"}
    first_btc_target = replayed.decisions[("schedule-1", str(btc.id))]
    first_eth_target = replayed.decisions[("schedule-1", str(eth.id))]
    assert Decimal("990") <= first_btc_target * Decimal("50001") <= Decimal("1010")
    assert Decimal("990") <= abs(first_eth_target) * Decimal("3001") <= Decimal("1010")
    assert all(
        order.status in {"FILLED", "CANCELED", "REJECTED"}
        for order in replayed.orders.values()
    )
    protection_orders = [
        order
        for order in replayed.orders.values()
        if order.protection_group_id is not None
    ]
    assert protection_orders
    assert all(order.reduce_only for order in protection_orders)
    account = engine.cache.account_for_venue(Venue("BINANCE"))
    assert account is not None
    assert account.balance_total(USDT).as_decimal() == replayed.wallet_balance
    assert engine.cache.positions_open() == []

    engine.dispose()


def test_real_strategy_start_pauses_on_uncertain_submitted_order(tmp_path) -> None:
    worker = ROOT / "tests/helpers/nautilus_uncertain_worker.py"
    completed = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(worker), str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_clean_restart_recovers_schedule_progress_without_duplicate_decisions(
    tmp_path,
) -> None:
    worker = ROOT / "tests/helpers/nautilus_uncertain_worker.py"
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for mode in ("complete", "restart"):
        completed = subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(worker), str(tmp_path), mode],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
