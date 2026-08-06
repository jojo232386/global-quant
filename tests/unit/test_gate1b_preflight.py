from __future__ import annotations

from decimal import Decimal

import pytest

from global_quant.gate1b.preflight import AccountPreflight
from global_quant.gate1b.preflight import PreflightError
from global_quant.gate1b.preflight import evaluate_account_preflight


def clean_preflight(**overrides) -> AccountPreflight:
    values = {
        "can_trade": True,
        "dual_side_position": False,
        "wallet_balance": Decimal("10000"),
        "nonzero_positions": (),
        "open_regular_order_ids": (),
        "open_algo_order_ids": (),
        "server_time_skew_ms": 250,
        "trading_instruments": frozenset({"BTCUSDT", "ETHUSDT"}),
    }
    values.update(overrides)
    return AccountPreflight(**values)


def test_clean_demo_account_preflight_passes_without_cleanup() -> None:
    result = evaluate_account_preflight(clean_preflight())

    assert result.status == "PASS"
    assert result.initial_wallet == Decimal("10000")
    assert result.automated_cleanup_allowed is False
    assert result.reason_codes == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"dual_side_position": True}, "HEDGE_MODE_FORBIDDEN"),
        (
            {"nonzero_positions": (("BTCUSDT", Decimal("0.01")),)},
            "UNCLEAN_DEMO_ACCOUNT",
        ),
        ({"open_regular_order_ids": ("123",)}, "UNCLEAN_DEMO_ACCOUNT"),
        ({"open_algo_order_ids": ("456",)}, "UNCLEAN_DEMO_ACCOUNT"),
        ({"can_trade": False}, "DEMO_TRADING_PERMISSION_MISSING"),
        ({"server_time_skew_ms": 5001}, "SERVER_TIME_SKEW"),
        ({"wallet_balance": Decimal("0")}, "DEMO_BALANCE_UNAVAILABLE"),
        ({"trading_instruments": frozenset({"BTCUSDT"})}, "INSTRUMENT_UNAVAILABLE"),
    ],
)
def test_unsafe_or_unusable_account_cannot_pass(overrides, reason) -> None:
    with pytest.raises(PreflightError, match=reason):
        evaluate_account_preflight(clean_preflight(**overrides))

