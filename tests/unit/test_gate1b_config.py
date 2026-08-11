from __future__ import annotations

import inspect
from decimal import Decimal

import global_quant.gate1b.config as config_module
from global_quant.gate1b.config import (
    MAX_GROSS_NOTIONAL,
    MAX_NOTIONAL_PER_INSTRUMENT,
    MAX_SUBMITTED_ORDERS,
)


def test_frozen_risk_caps_are_machine_constants() -> None:
    assert Decimal("200") == MAX_NOTIONAL_PER_INSTRUMENT
    assert Decimal("400") == MAX_GROSS_NOTIONAL
    assert MAX_SUBMITTED_ORDERS == 32


def test_config_module_has_no_live_execution_client_or_retry_owner() -> None:
    source = inspect.getsource(config_module)

    assert not hasattr(config_module, "build_binance_client_configs")
    assert "BinanceExecClientConfig" not in source
    assert "max_retries" not in source
