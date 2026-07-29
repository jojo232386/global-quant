from __future__ import annotations

import ast
import hashlib
import inspect
from decimal import Decimal
from pathlib import Path

from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from global_quant.gate1a.strategy import FixedTargetConfig
from global_quant.gate1a.strategy import FixedTargetStrategy
from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger


def test_shared_strategy_is_a_nautilus_strategy() -> None:
    assert issubclass(FixedTargetStrategy, Strategy)
    assert FixedTargetConfig.__name__ == "FixedTargetConfig"


def test_strategy_has_no_environment_behavior_branch() -> None:
    source = inspect.getsource(FixedTargetStrategy)
    tree = ast.parse(source)
    forbidden_names = {
        "backtest",
        "demo",
        "live",
        "testnet",
        "environment",
        "is_backtest",
        "is_live",
    }
    observed = {
        node.id.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    observed.update(
        node.attr.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )
    assert observed.isdisjoint(forbidden_names)


def test_strategy_source_hash_is_stable_and_nonempty() -> None:
    path = Path(inspect.getsourcefile(FixedTargetStrategy))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(digest) == 64
    assert digest != "0" * 64


def test_schedule_progress_is_recovered_from_durable_decisions(tmp_path) -> None:
    btc_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    eth_id = InstrumentId.from_str("ETHUSDT-PERP.BINANCE")
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
            ledger_path=str(tmp_path / "events.jsonl"),
            initial_wallet=Decimal("10000"),
            source_hash="source-hash",
            config_hash="config-hash",
        ),
    )
    strategy._coordinator = EventSourcedCoordinator(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        strategy_id="GATE1A",
        run_id="run-1",
        process_start_id="process-1",
        source_hash="source-hash",
        config_hash="config-hash",
    )
    strategy._coordinator.persist_decision(
        "schedule-0",
        "BTCUSDT-PERP.BINANCE",
        Decimal("0"),
    )
    strategy._coordinator.persist_decision(
        "schedule-0",
        "ETHUSDT-PERP.BINANCE",
        Decimal("0"),
    )
    strategy._coordinator.persist_decision(
        "schedule-1",
        "BTCUSDT-PERP.BINANCE",
        Decimal("0.02"),
    )

    assert strategy._next_schedule_step() == 1
