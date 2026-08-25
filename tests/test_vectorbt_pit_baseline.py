from __future__ import annotations

import json

import pytest

from research.exploration.price_alpha_v1 import Bar, PriceDataset
from research.oss import vectorbt_pit_baseline as poc


def _synthetic_inputs(monkeypatch):
    first = poc._timestamp(poc.FIRST_EXECUTION)
    final_exit = poc._timestamp(poc.FINAL_EXIT)
    symbols = tuple(f"S{index:03d}USDT" for index in range(80))
    bars = {
        symbol: {
            timestamp: Bar(open=100.0 + index, close=101.0 + index, quote_volume=1.0)
            for timestamp in range(first - poc.DAY_MS, final_exit + poc.DAY_MS, poc.DAY_MS)
        }
        for index, symbol in enumerate(symbols)
    }
    dataset = PriceDataset(
        bars=bars,
        last_timestamp={symbol: final_exit for symbol in symbols},
        pit={},
        artifact_path=poc.ROOT,
        manifest_sha256=poc.MANIFEST_SHA256,
        pit_sha256=poc.PIT_SHA256,
        labels=(),
    )
    # A Monday terminal makes the implementation retain the scheduled open
    # order and then book a distinct same-day terminal-close liquidation.
    terminal = "2023-03-20T09:00:00.000Z"
    terminal_day = poc._timestamp(terminal) // poc.DAY_MS * poc.DAY_MS
    for timestamp in range(terminal_day + poc.DAY_MS, final_exit + poc.DAY_MS, poc.DAY_MS):
        del bars[symbols[0]][timestamp]
    master = {
        "records": [
            {"symbol": symbol, "terminal_timestamp_utc": terminal if symbol == symbols[0] else None}
            for symbol in symbols
        ]
    }

    def synthetic_universe(_master, timestamp: str):
        # The first execution loses one name after the completed decision; the
        # frozen decision denominator must remain 80, with cash residual.
        as_of = poc._timestamp(timestamp)
        if as_of == first or as_of > terminal_day:
            return symbols[1:]
        return symbols

    monkeypatch.setattr(poc, "universe_at", synthetic_universe)
    return dataset, master, symbols, first


def test_vectorbt_pit_baseline_is_deterministic_not_alpha_and_no_renormalization(
    monkeypatch, tmp_path
) -> None:
    dataset, master, symbols, first = _synthetic_inputs(monkeypatch)
    inputs = poc.build_inputs(dataset, master)
    portfolio = poc.build_portfolio(inputs)
    first_result = poc.run_poc_from_inputs(dataset, master)
    second_result = poc.run_poc_from_inputs(dataset, master)

    first_row = inputs.size.loc[inputs.close.index[0]]
    assert len(inputs.symbols) == poc.EXPECTED_COHORT_SIZE == 80
    assert first_row[symbols[0]] == 0.0
    assert first_row[symbols[1]] == 1.0 / 80.0
    assert first_row.sum() == pytest.approx(79.0 / 80.0)  # cancellation is cash, not reweighting
    assert inputs.pit_observations > 1
    assert inputs.terminal_liquidations == 1
    terminal_at = poc.pd.to_datetime("2023-03-20T09:00:00.000Z")
    post_terminal = portfolio.close.loc[portfolio.close.index > terminal_at, symbols[0]]
    assert post_terminal.notna().sum() == 0  # simulator did not fill the mark
    terminal_orders = portfolio.orders.records_readable.query(
        "Column == @symbols[0] and Timestamp == @terminal_at"
    )
    assert len(terminal_orders) == 1
    assert terminal_orders.iloc[0]["Side"] == "Sell"
    assert terminal_orders.iloc[0]["Fees"] > 0.0
    assert terminal_orders.iloc[0]["Price"] == pytest.approx(101.0 * (1.0 - poc.ONE_SIDE_SLIPPAGE))
    assert first_result["result"] == poc.RESULT_LABEL
    assert first_result["alpha_claim"] is False
    assert first_result["framework"]["version"] == "1.1.0"
    assert first_result["execution"]["entry"] == "next UTC open at t"
    assert first_result["execution"]["fee_rate"] == 0.0005
    assert first_result["execution"]["one_side_slippage"] == 0.001
    assert json.dumps(first_result, sort_keys=True) == json.dumps(second_result, sort_keys=True)
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    poc.write_artifact(first_path, first_result)
    poc.write_artifact(second_path, second_result)
    assert first_path.read_bytes() == second_path.read_bytes()
