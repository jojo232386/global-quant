from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from global_quant.gate1b.mutation_protocol import (
    LimitOrderFilters,
    MutationProtocolError,
    evaluate_credential_free_filter_preparation,
)


def _static_filters() -> tuple[dict[str, object], ...]:
    return (
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "39.86",
            "maxPrice": "306177",
            "tickSize": "0.01",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.001",
            "maxQty": "10000",
            "stepSize": "0.001",
        },
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.001",
            "maxQty": "10000",
            "stepSize": "0.001",
        },
        {"filterType": "MIN_NOTIONAL", "notional": "20"},
        {
            "filterType": "PERCENT_PRICE",
            "multiplierDown": "0.9500",
            "multiplierUp": "1.0500",
            "multiplierDecimal": "4",
        },
    )


def _strict_limit_filters() -> LimitOrderFilters:
    return LimitOrderFilters(
        min_price=Decimal("39.86"),
        max_price=Decimal("306177"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("10000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("20"),
        percent_price_multiplier_down=Decimal("0.9500"),
        percent_price_multiplier_up=Decimal("1.0500"),
    )


def test_supported_static_filters_remain_strictly_validated() -> None:
    admission = evaluate_credential_free_filter_preparation(_static_filters())

    assert admission.preparation_ready is True
    assert admission.static_filter_types == (
        "LOT_SIZE",
        "MARKET_LOT_SIZE",
        "MIN_NOTIONAL",
        "PERCENT_PRICE",
        "PRICE_FILTER",
    )
    assert admission.authenticated_check_required == ()
    assert admission.unresolved_exchange_rules == ()
    assert admission.order_authorization_ready is False
    with pytest.raises(
        MutationProtocolError,
        match="ORDER_AUTHORIZATION_DENIED:AUTHENTICATED_PREFLIGHT_REQUIRED",
    ):
        admission.require_order_authorization_ready()

    missing_price = tuple(
        item for item in _static_filters() if item["filterType"] != "PRICE_FILTER"
    )
    with pytest.raises(MutationProtocolError, match="FILTER_CARDINALITY_MISMATCH"):
        evaluate_credential_free_filter_preparation(missing_price)

    invalid_notional = tuple(
        {**item, "notional": "0"} if item["filterType"] == "MIN_NOTIONAL" else item
        for item in _static_filters()
    )
    with pytest.raises(MutationProtocolError, match="INVALID_FILTER_METADATA"):
        evaluate_credential_free_filter_preparation(invalid_notional)


def test_max_num_orders_is_preserved_as_authenticated_check() -> None:
    admission = evaluate_credential_free_filter_preparation(
        (*_static_filters(), {"filterType": "MAX_NUM_ORDERS", "limit": 10000})
    )

    assert admission.preparation_ready is True
    assert admission.order_authorization_ready is False
    assert [item.filter_type for item in admission.authenticated_check_required] == [
        "MAX_NUM_ORDERS"
    ]
    assert admission.authenticated_check_required[0].metadata == {
        "filterType": "MAX_NUM_ORDERS",
        "limit": 10000,
    }
    assert admission.unresolved_exchange_rules == ()
    with pytest.raises(
        MutationProtocolError,
        match="ORDER_AUTHORIZATION_DENIED:AUTHENTICATED_CHECK_REQUIRED",
    ):
        admission.require_order_authorization_ready()


def test_position_risk_control_is_preserved_and_blocks_order_authorization() -> None:
    admission = evaluate_credential_free_filter_preparation(
        (
            *_static_filters(),
            {
                "filterType": "MAX_NUM_ORDERS",
                "limit": 10000,
            },
            {
                "filterType": "POSITION_RISK_CONTROL",
                "positionControlSide": "NONE",
            },
        )
    )

    assert admission.preparation_ready is True
    assert admission.order_authorization_ready is False
    assert [item.filter_type for item in admission.authenticated_check_required] == [
        "MAX_NUM_ORDERS"
    ]
    assert [item.filter_type for item in admission.unresolved_exchange_rules] == [
        "POSITION_RISK_CONTROL"
    ]
    assert admission.unresolved_exchange_rules[0].metadata == {
        "filterType": "POSITION_RISK_CONTROL",
        "positionControlSide": "NONE",
    }
    with pytest.raises(
        MutationProtocolError,
        match="ORDER_AUTHORIZATION_DENIED:UNRESOLVED_EXCHANGE_RULES",
    ):
        admission.require_order_authorization_ready()


def test_arbitrary_future_filter_is_preserved_without_fail_open() -> None:
    future = {
        "filterType": "SOME_FUTURE_BINANCE_FILTER",
        "opaqueFlag": "UNDEFINED",
        "opaqueLimit": 7,
    }
    admission = evaluate_credential_free_filter_preparation((*_static_filters(), future))

    assert admission.preparation_ready is True
    assert admission.order_authorization_ready is False
    assert admission.unresolved_exchange_rules[0].metadata == future
    assert admission.evidence_payload == {
        "authenticated_check_required": [],
        "order_authorization_ready": False,
        "preparation_ready": True,
        "static_filter_types": [
            "LOT_SIZE",
            "MARKET_LOT_SIZE",
            "MIN_NOTIONAL",
            "PERCENT_PRICE",
            "PRICE_FILTER",
        ],
        "unresolved_exchange_rules": [
            {"filter_type": "SOME_FUTURE_BINANCE_FILTER", "metadata": future}
        ],
    }
    with pytest.raises(
        MutationProtocolError,
        match="ORDER_AUTHORIZATION_DENIED:UNRESOLVED_EXCHANGE_RULES",
    ):
        admission.require_order_authorization_ready()

    with pytest.raises(MutationProtocolError, match="UNKNOWN_APPLICABLE_FILTER"):
        replace(
            _strict_limit_filters(),
            uninterpreted_applicable_filter_types=("SOME_FUTURE_BINANCE_FILTER",),
        )


def test_malformed_max_num_orders_stays_unresolved() -> None:
    admission = evaluate_credential_free_filter_preparation(
        (*_static_filters(), {"filterType": "MAX_NUM_ORDERS", "limit": "10000"})
    )

    assert admission.authenticated_check_required == ()
    assert [item.filter_type for item in admission.unresolved_exchange_rules] == ["MAX_NUM_ORDERS"]
    assert admission.order_authorization_ready is False
