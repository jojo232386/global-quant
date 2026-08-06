from __future__ import annotations

from decimal import Decimal

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

from global_quant.gate1a.strategy import FixedTargetStrategy
from global_quant.gate1b.runtime import DemoRuntimeInputs
from global_quant.gate1b.runtime import build_demo_node
from global_quant.gate1b.runtime import build_demo_node_config
from global_quant.gate1b.safety import DemoCredentials


def runtime_inputs(tmp_path) -> DemoRuntimeInputs:
    return DemoRuntimeInputs(
        credentials=DemoCredentials(
            api_key="demo-key-test-only",
            api_secret="demo-secret-test-only",
        ),
        evidence_dir=tmp_path / "evidence",
        ledger_path=tmp_path / "events.jsonl",
        initial_wallet=Decimal("10000"),
        source_hash="source-hash",
        config_hash="config-hash",
    )


def test_trading_node_config_is_demo_reconciled_and_risk_bounded(tmp_path) -> None:
    config = build_demo_node_config(runtime_inputs(tmp_path))

    data = config.data_clients[BINANCE]
    execution = config.exec_clients[BINANCE]
    assert data.environment is BinanceEnvironment.DEMO
    assert execution.environment is BinanceEnvironment.DEMO
    assert data.account_type is BinanceAccountType.USDT_FUTURES
    assert execution.account_type is BinanceAccountType.USDT_FUTURES
    assert config.exec_engine.reconciliation is True
    assert config.exec_engine.open_check_open_only is False
    assert config.exec_engine.graceful_shutdown_on_exception is True
    assert config.risk_engine.bypass is False
    assert config.risk_engine.max_notional_per_order == {
        "BTCUSDT-PERP.BINANCE": 200,
        "ETHUSDT-PERP.BINANCE": 200,
    }
    assert config.risk_engine.max_order_submit_rate == "32/12:00:00"


def test_node_builds_offline_with_shared_strategy_and_does_not_connect(tmp_path) -> None:
    node, strategy = build_demo_node(runtime_inputs(tmp_path))
    try:
        assert isinstance(strategy, FixedTargetStrategy)
        assert strategy.config.max_notional_per_instrument == Decimal("200")
        assert set(strategy.config.external_order_claims) == {
            strategy.config.btc_instrument_id,
            strategy.config.eth_instrument_id,
        }
    finally:
        node.dispose()
