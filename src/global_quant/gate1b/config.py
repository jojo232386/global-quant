from __future__ import annotations

from decimal import Decimal

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance import BinanceDataClientConfig
from nautilus_trader.adapters.binance import BinanceExecClientConfig
from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.identifiers import InstrumentId

from global_quant.gate1b.safety import DemoCredentials
from global_quant.gate1b.safety import resolve_demo_endpoints
from global_quant.gate1b.safety import validate_demo_endpoints


MAX_NOTIONAL_PER_INSTRUMENT = Decimal("200")
MAX_GROSS_NOTIONAL = Decimal("400")
MAX_SUBMITTED_ORDERS = 32


def build_binance_client_configs(
    *,
    credentials: DemoCredentials,
    instruments: frozenset[InstrumentId],
) -> tuple[BinanceDataClientConfig, BinanceExecClientConfig]:
    validate_demo_endpoints(resolve_demo_endpoints())
    provider = BinanceInstrumentProviderConfig(
        load_ids=instruments,
        query_commission_rates=True,
    )
    common = {
        "api_key": credentials.api_key,
        "api_secret": credentials.api_secret,
        "account_type": BinanceAccountType.USDT_FUTURES,
        "environment": BinanceEnvironment.DEMO,
        "instrument_provider": provider,
        "base_url_http": None,
        "base_url_ws": None,
        "proxy_url": None,
    }
    data_config = BinanceDataClientConfig(**common)
    exec_config = BinanceExecClientConfig(
        **common,
        base_url_ws_stream=None,
        use_reduce_only=True,
        max_retries=3,
        log_rejected_due_post_only_as_warning=False,
    )
    return data_config, exec_config

