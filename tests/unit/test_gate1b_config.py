from __future__ import annotations

from decimal import Decimal

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.identifiers import InstrumentId

from global_quant.gate1b.config import MAX_GROSS_NOTIONAL
from global_quant.gate1b.config import MAX_NOTIONAL_PER_INSTRUMENT
from global_quant.gate1b.config import MAX_SUBMITTED_ORDERS
from global_quant.gate1b.config import build_binance_client_configs
from global_quant.gate1b.safety import DemoCredentials


def test_demo_client_configs_are_explicit_and_have_no_overrides() -> None:
    credentials = DemoCredentials(
        api_key="demo-key-test-only",
        api_secret="demo-secret-test-only",
    )
    instruments = frozenset(
        {
            InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
            InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
        },
    )

    data_config, exec_config = build_binance_client_configs(
        credentials=credentials,
        instruments=instruments,
    )

    for config in (data_config, exec_config):
        assert config.environment is BinanceEnvironment.DEMO
        assert config.account_type is BinanceAccountType.USDT_FUTURES
        assert config.base_url_http is None
        assert config.base_url_ws is None
        assert config.proxy_url is None
        assert config.api_key == "demo-key-test-only"
        assert config.api_secret == "demo-secret-test-only"
        assert config.instrument_provider.load_ids == instruments
    assert exec_config.base_url_ws_stream is None
    assert exec_config.use_reduce_only is True


def test_frozen_risk_caps_are_machine_constants() -> None:
    assert MAX_NOTIONAL_PER_INSTRUMENT == Decimal("200")
    assert MAX_GROSS_NOTIONAL == Decimal("400")
    assert MAX_SUBMITTED_ORDERS == 32
