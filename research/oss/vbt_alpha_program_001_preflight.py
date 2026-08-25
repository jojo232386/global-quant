#!/usr/bin/env python3
"""Metric-free synthetic long/short contract check for VBT Alpha Program 001."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

import numpy as np
import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.oss.vectorbt_pit_baseline import (
    BaselineInputs,
    FEE_RATE,
    ONE_SIDE_SLIPPAGE,
    build_portfolio,
    vbt,
)


def _inputs() -> BaselineInputs:
    if sys.modules.get("pandas") is not pd:
        sys.modules["pandas"] = pd
    index = pd.to_datetime(
        ["2023-01-01T00:00:00Z", "2023-01-01T12:00:00Z", "2023-01-02T00:00:00Z"]
    )
    columns = ("LONGUSDT", "SHORTUSDT")
    close = pd.DataFrame([[100.0, 100.0], [101.0, 99.0], [np.nan, 98.0]], index=index, columns=columns)
    valuation = pd.DataFrame([[99.0, 101.0], [101.0, 99.0], [np.nan, 99.0]], index=index, columns=columns)
    size = pd.DataFrame([[0.5, -0.5], [0.0, np.nan], [np.nan, 0.0]], index=index, columns=columns)
    price = pd.DataFrame([[100.0, 100.0], [101.0, np.nan], [np.nan, 98.0]], index=index, columns=columns)
    return BaselineInputs(tuple(index.view("int64") // 1_000_000), columns, close, valuation, size, price, 1, 1, 4)


def run_preflight() -> dict[str, object]:
    inputs = _inputs()
    portfolio = build_portfolio(inputs)
    records = portfolio.orders.records_readable
    first, terminal, final = inputs.close.index
    expected = [
        ("LONGUSDT", first, "Buy", 100.0 * (1.0 + ONE_SIDE_SLIPPAGE)),
        ("SHORTUSDT", first, "Sell", 100.0 * (1.0 - ONE_SIDE_SLIPPAGE)),
        ("LONGUSDT", terminal, "Sell", 101.0 * (1.0 - ONE_SIDE_SLIPPAGE)),
        ("SHORTUSDT", final, "Buy", 98.0 * (1.0 + ONE_SIDE_SLIPPAGE)),
    ]
    for column, timestamp, side, price in expected:
        rows = records[(records["Column"] == column) & (records["Timestamp"] == timestamp)]
        if len(rows) != 1 or rows.iloc[0]["Side"] != side:
            raise AssertionError(f"unexpected synthetic order {column}:{timestamp}:{side}")
        if not np.isclose(float(rows.iloc[0]["Price"]), price) or float(rows.iloc[0]["Fees"]) <= 0:
            raise AssertionError(f"cost semantics failed {column}:{timestamp}")
    if portfolio.close.loc[portfolio.close.index > terminal, "LONGUSDT"].notna().any():
        raise AssertionError("post-terminal close was filled")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "close": inputs.close.fillna("NA").values.tolist(),
                "valuation": inputs.valuation.fillna("NA").values.tolist(),
                "size": inputs.size.fillna("NA").values.tolist(),
                "price": inputs.price.fillna("NA").values.tolist(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "artifact_class": "GMAQ_VBT_ALPHA_PROGRAM_001_PREFLIGHT",
        "result": "PASS_METRIC_FREE",
        "framework": f"vectorbt=={vbt.__version__}",
        "synthetic_only": True,
        "target_percent": {"long": 0.5, "short": -0.5, "gross": 1.0, "net": 0.0},
        "next_open_order_price": True,
        "fees_and_one_side_slippage": {"fee_rate": FEE_RATE, "slippage": ONE_SIDE_SLIPPAGE, "verified": True},
        "terminal_exit_once": True,
        "post_terminal_fill_count": 0,
        "order_contract": ["Buy long", "Sell short", "Sell terminal long", "Buy final short cover"],
        "metrics_exposed": [],
        "deterministic_input_sha256": fingerprint,
    }


def main() -> None:
    print(json.dumps(run_preflight(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
