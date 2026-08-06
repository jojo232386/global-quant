from __future__ import annotations

from decimal import Decimal

from global_quant.gate1a.strategy import FixedTargetStrategy


def test_target_notional_is_capped_without_environment_branch() -> None:
    assert FixedTargetStrategy.target_notional_for_weight(
        initial_wallet=Decimal("10000"),
        weight=Decimal("0.1"),
        max_notional=Decimal("200"),
    ) == Decimal("200")
    assert FixedTargetStrategy.target_notional_for_weight(
        initial_wallet=Decimal("1000"),
        weight=Decimal("0.1"),
        max_notional=Decimal("200"),
    ) == Decimal("100.0")
    assert FixedTargetStrategy.target_notional_for_weight(
        initial_wallet=Decimal("10000"),
        weight=Decimal("-0.1"),
        max_notional=Decimal("200"),
    ) == Decimal("200")


def test_uncapped_target_preserves_gate1a_behavior() -> None:
    assert FixedTargetStrategy.target_notional_for_weight(
        initial_wallet=Decimal("10000"),
        weight=Decimal("0.1"),
        max_notional=None,
    ) == Decimal("1000.0")
