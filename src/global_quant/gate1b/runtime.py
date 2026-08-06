from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance import BinanceLiveExecClientFactory
from nautilus_trader.common import Environment
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import LiveDataEngineConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId

from global_quant.gate1a.strategy import FixedTargetConfig
from global_quant.gate1a.strategy import FixedTargetStrategy
from global_quant.gate1b.config import MAX_NOTIONAL_PER_INSTRUMENT
from global_quant.gate1b.config import MAX_SUBMITTED_ORDERS
from global_quant.gate1b.config import build_binance_client_configs
from global_quant.gate1b.safety import DemoCredentials


BTC_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
ETH_ID = InstrumentId.from_str("ETHUSDT-PERP.BINANCE")
BTC_BAR = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")
ETH_BAR = BarType.from_str("ETHUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")


@dataclass(frozen=True)
class DemoRuntimeInputs:
    credentials: DemoCredentials
    evidence_dir: Path
    ledger_path: Path
    initial_wallet: Decimal
    source_hash: str
    config_hash: str


def build_demo_node_config(inputs: DemoRuntimeInputs) -> TradingNodeConfig:
    instruments = frozenset({BTC_ID, ETH_ID})
    data_config, exec_config = build_binance_client_configs(
        credentials=inputs.credentials,
        instruments=instruments,
    )
    return TradingNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId("GATE1B-001"),
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="INFO",
            log_directory=str(inputs.evidence_dir),
            log_file_name="nautilus-gate1b",
            log_file_format="json",
            log_colors=False,
            use_pyo3=True,
            fileout_sync_on_flush=True,
        ),
        data_engine=LiveDataEngineConfig(
            external_clients=[ClientId(BINANCE)],
            validate_data_sequence=True,
            graceful_shutdown_on_exception=True,
        ),
        risk_engine=LiveRiskEngineConfig(
            bypass=False,
            max_order_submit_rate=f"{MAX_SUBMITTED_ORDERS}/12:00:00",
            max_order_modify_rate=f"{MAX_SUBMITTED_ORDERS}/12:00:00",
            max_notional_per_order={
                str(BTC_ID): int(MAX_NOTIONAL_PER_INSTRUMENT),
                str(ETH_ID): int(MAX_NOTIONAL_PER_INSTRUMENT),
            },
            graceful_shutdown_on_exception=True,
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_instrument_ids=[BTC_ID, ETH_ID],
            open_check_open_only=False,
            filter_unclaimed_external_orders=False,
            generate_missing_orders=True,
            snapshot_orders=True,
            snapshot_positions=True,
            graceful_shutdown_on_exception=True,
        ),
        cache=CacheConfig(
            timestamps_as_iso8601=True,
            flush_on_start=False,
        ),
        data_clients={BINANCE: data_config},
        exec_clients={BINANCE: exec_config},
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
        timeout_shutdown=5.0,
    )


def build_demo_node(
    inputs: DemoRuntimeInputs,
) -> tuple[TradingNode, FixedTargetStrategy]:
    inputs.evidence_dir.mkdir(parents=True, exist_ok=True)
    node = TradingNode(config=build_demo_node_config(inputs))
    strategy = FixedTargetStrategy(
        FixedTargetConfig(
            strategy_id="GATE1B-001",
            btc_instrument_id=BTC_ID,
            eth_instrument_id=ETH_ID,
            btc_bar_type=BTC_BAR,
            eth_bar_type=ETH_BAR,
            ledger_path=str(inputs.ledger_path),
            initial_wallet=Decimal(inputs.initial_wallet),
            source_hash=inputs.source_hash,
            config_hash=inputs.config_hash,
            max_notional_per_instrument=MAX_NOTIONAL_PER_INSTRUMENT,
            external_order_claims=[BTC_ID, ETH_ID],
        ),
    )
    node.trader.add_strategy(strategy)
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
    node.build()
    return node, strategy
