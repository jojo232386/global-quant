from __future__ import annotations

from dataclasses import InitVar, dataclass
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from global_quant.gate1a.strategy import FixedTargetConfig, FixedTargetStrategy
from global_quant.gate1b.config import MAX_NOTIONAL_PER_INSTRUMENT, MAX_SUBMITTED_ORDERS

BTC_ID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
ETH_ID = InstrumentId.from_str("ETHUSDT-PERP.BINANCE")
BTC_BAR = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")
ETH_BAR = BarType.from_str("ETHUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")


@dataclass(frozen=True)
class DemoRuntimeInputs:
    evidence_dir: Path
    ledger_path: Path
    initial_wallet: Decimal
    source_hash: str
    config_hash: str
    # Kept as an init-only compatibility slot for the retired v1.4 build-only
    # caller.  It is deliberately neither inspected nor retained.
    credentials: InitVar[object | None] = None


@dataclass(frozen=True, slots=True)
class OfflineBuildConfig:
    """Credential-free description used only to validate strategy assembly."""

    instrument_ids: tuple[InstrumentId, ...]
    max_notional_per_instrument: Decimal
    max_submitted_orders: int
    network_enabled: bool = False
    mutation_enabled: bool = False


@dataclass(slots=True)
class OfflineBuildNode:
    """Inert compatibility facade for the old ``run_build_only`` command."""

    config: OfflineBuildConfig
    disposed: bool = False

    def dispose(self) -> None:
        self.disposed = True


def build_demo_node_config(_inputs: DemoRuntimeInputs) -> OfflineBuildConfig:
    """Return a credential-free, non-network build description."""

    return OfflineBuildConfig(
        instrument_ids=(BTC_ID, ETH_ID),
        max_notional_per_instrument=MAX_NOTIONAL_PER_INSTRUMENT,
        max_submitted_orders=MAX_SUBMITTED_ORDERS,
    )


def build_offline_strategy(inputs: DemoRuntimeInputs) -> FixedTargetStrategy:
    """Assemble the frozen strategy without constructing any live client."""

    return FixedTargetStrategy(
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


def build_demo_node(
    inputs: DemoRuntimeInputs,
) -> tuple[OfflineBuildNode, FixedTargetStrategy]:
    """Retired live-node entry point, retained as an inert build-only facade."""

    inputs.evidence_dir.mkdir(parents=True, exist_ok=True)
    return OfflineBuildNode(build_demo_node_config(inputs)), build_offline_strategy(inputs)
