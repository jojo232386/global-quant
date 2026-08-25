#!/usr/bin/env python3
"""Deterministic vectorbt baseline over GMAQ's frozen Price/PIT/Lifecycle inputs.

This is deliberately a *benchmark*, not an alpha implementation.  It proves
that the selected research simulator can receive the authoritative data and
timing contract without becoming the source of truth for either one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
import vectorbt as vbt


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.data.pit_instrument_master_v1 import (  # noqa: E402
    EXPECTED_COHORT_SIZE,
    InstrumentMasterError,
    load_master,
    universe_at,
)
from research.exploration.price_alpha_v1 import (  # noqa: E402
    DAY_MS,
    MANIFEST_SHA256,
    PIT_SHA256,
    SNAPSHOT_ID,
    PriceAlphaError,
    PriceDataset,
    load_dataset,
)


POC_ID = "OSS-VBT-PIT-EQUALWEIGHT-LONG-001"
RESULT_LABEL = "BENCHMARK_ONLY_NOT_ALPHA"
FIRST_EXECUTION = "2021-01-11T00:00:00.000Z"
FINAL_EXECUTION = "2023-11-06T00:00:00.000Z"
FINAL_EXIT = "2023-11-13T00:00:00.000Z"
FEE_RATE = 0.0005
ONE_SIDE_SLIPPAGE = 0.001


class POCDataError(RuntimeError):
    """Raised when a frozen input or timing invariant cannot be preserved."""


@dataclass(frozen=True)
class BaselineInputs:
    dates: tuple[int, ...]
    symbols: tuple[str, ...]
    close: pd.DataFrame
    valuation: pd.DataFrame
    size: pd.DataFrame
    price: pd.DataFrame
    pit_observations: int
    terminal_liquidations: int
    order_observations: int


def _timestamp(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise POCDataError(f"DATA_ERROR_STOP: non-UTC timestamp {value}")
    return int(parsed.timestamp() * 1000)


def _iso(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bar(dataset: PriceDataset, symbol: str, timestamp: int):
    try:
        return dataset.bar(symbol, timestamp)
    except PriceAlphaError as error:
        raise POCDataError(str(error)) from error


def _master_symbols(master: Mapping[str, Any]) -> tuple[str, ...]:
    records = master.get("records")
    if not isinstance(records, list):
        raise POCDataError("DATA_ERROR_STOP: instrument master has no records")
    symbols = tuple(sorted(record.get("symbol") for record in records if isinstance(record, Mapping)))
    if len(symbols) != EXPECTED_COHORT_SIZE or len(set(symbols)) != EXPECTED_COHORT_SIZE:
        raise POCDataError("DATA_ERROR_STOP: bounded cohort is not exactly 80 unique symbols")
    return symbols


def _terminals(master: Mapping[str, Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for record in master["records"]:
        value = record.get("terminal_timestamp_utc")
        if isinstance(value, str):
            output[record["symbol"]] = _timestamp(value)
    return output


def _universe(master: Mapping[str, Any], timestamp: int) -> tuple[str, ...]:
    try:
        result = universe_at(master, _iso(timestamp))
    except InstrumentMasterError as error:
        raise POCDataError(f"DATA_ERROR_STOP: lifecycle/PIT query failed: {error}") from error
    if result != tuple(sorted(set(result))):
        raise POCDataError("DATA_ERROR_STOP: lifecycle/PIT universe is malformed")
    return result


def load_frozen_inputs(data_root: pathlib.Path | None = None) -> tuple[PriceDataset, dict[str, Any]]:
    """Load Price V1 and the bounded lifecycle master; neither is adapted."""
    try:
        dataset = load_dataset() if data_root is None else load_dataset(data_root)
        master = load_master()
    except (PriceAlphaError, InstrumentMasterError, OSError, ValueError) as error:
        raise POCDataError(f"DATA_ERROR_STOP: frozen input validation failed: {error}") from error
    symbols = _master_symbols(master)
    absent = sorted(set(symbols) - set(dataset.bars))
    if absent:
        raise POCDataError(f"DATA_ERROR_STOP: Price V1 lacks cohort symbols {absent}")
    # Explicitly touch all cohort series.  This prevents a simulator-level
    # column subset from silently bypassing Price V1's 80-name cohort.
    for symbol in symbols:
        if not dataset.bars[symbol]:
            raise POCDataError(f"DATA_ERROR_STOP: empty cohort series {symbol}")
    return dataset, master


def build_inputs(dataset: PriceDataset, master: Mapping[str, Any]) -> BaselineInputs:
    """Build next-open target-percent orders and terminal-close liquidations."""
    # Some repository runtime-contract tests substitute a minimal ``pandas``
    # module in ``sys.modules``. Keep the standalone research adapter bound to
    # the fully imported package rather than letting that test-only shim leak
    # into pandas' deferred internal imports.
    if sys.modules.get("pandas") is not pd:
        sys.modules["pandas"] = pd
    symbols = _master_symbols(master)
    first, final_execution, final_exit = (
        _timestamp(FIRST_EXECUTION),
        _timestamp(FINAL_EXECUTION),
        _timestamp(FINAL_EXIT),
    )
    if (final_execution - first) % (7 * DAY_MS) or final_exit != final_execution + 7 * DAY_MS:
        raise POCDataError("DATA_ERROR_STOP: frozen weekly benchmark schedule differs")
    base_dates = tuple(range(first, final_exit + DAY_MS, DAY_MS))
    terminal_rows = tuple(
        terminal
        for terminal in _terminals(master).values()
        if first <= terminal < final_exit
    )
    # A terminal is an intraday fact.  Add a second, exact timestamp so the
    # canonical close-price liquidation never overwrites a same-day next-open
    # rebalance (HNT is such a case in the frozen cohort).
    dates = tuple(sorted(set(base_dates) | set(terminal_rows)))
    close = pd.DataFrame(np.nan, index=pd.to_datetime(dates, unit="ms", utc=True), columns=symbols)
    valuation = close.copy()
    size = close.copy()
    price = close.copy()
    terminals = _terminals(master)
    terminal_liquidations = 0
    pit_observations: set[tuple[int, tuple[str, ...]]] = set()

    for current in base_dates:
        index = pd.to_datetime(current, unit="ms", utc=True)
        for symbol in symbols:
            terminal = terminals.get(symbol)
            # Never manufacture a post-terminal mark from Price V1's later
            # rows. Once the canonical terminal close has occurred, the
            # column is intentionally unavailable rather than forward-filled.
            if terminal is not None and current > terminal // DAY_MS * DAY_MS:
                continue
            close.loc[index, symbol] = _bar(dataset, symbol, current).close
            valuation.loc[index, symbol] = _bar(dataset, symbol, current).close

        if first <= current <= final_execution and (current - first) % (7 * DAY_MS) == 0:
            decision = current - DAY_MS
            decision_members = _universe(master, decision)
            execution_members = set(_universe(master, current))
            members = tuple(symbol for symbol in decision_members if symbol in execution_members)
            if not members:
                raise POCDataError("DATA_ERROR_STOP: no active lifecycle/PIT members at rebalance")
            pit_observations.add((current, members))
            # Decision values must be present at the completed prior close even
            # though this equal-weight control does not form an alpha signal.
            for symbol in members:
                decision_close = _bar(dataset, symbol, decision).close
                execution_open = _bar(dataset, symbol, current).open
                if not math.isfinite(decision_close) or decision_close <= 0 or not math.isfinite(execution_open) or execution_open <= 0:
                    raise POCDataError(f"DATA_ERROR_STOP: invalid decision/next-open input {symbol}")
            # Target-percent sizing is valued only on the completed decision
            # close, while actual execution still uses this row's next open.
            valuation.loc[index, list(execution_members)] = [
                _bar(dataset, symbol, decision).close for symbol in execution_members
            ]
            size.loc[index, :] = 0.0
            # Targets and denominator are fixed at completed decision time.
            # A member absent at execution is cancelled into cash; it is never
            # reweighted across the survivors with execution-time knowledge.
            size.loc[index, list(members)] = 1.0 / len(decision_members)
            price.loc[index, list(members)] = [
                _bar(dataset, symbol, current).open for symbol in members
            ]
        elif current == final_exit:
            # The final benchmark closeout is an explicit next UTC open order,
            # not an inferred close valuation or a left-open terminal state.
            active_at_exit = _universe(master, current)
            size.loc[index, :] = 0.0
            price.loc[index, list(active_at_exit)] = [
                _bar(dataset, symbol, current).open for symbol in active_at_exit
            ]
            valuation.loc[index, list(active_at_exit)] = [
                _bar(dataset, symbol, current - DAY_MS).close for symbol in active_at_exit
            ]

    for symbol, terminal in terminals.items():
        if terminal not in terminal_rows:
            continue
        index = pd.to_datetime(terminal, unit="ms", utc=True)
        terminal_day = terminal // DAY_MS * DAY_MS
        # Canonical Lifecycle V1 behaviour: retain through the terminal day's
        # open-to-final-close interval, then force a close-price exit.
        for cohort_symbol in symbols:
            cohort_terminal = terminals.get(cohort_symbol)
            if cohort_terminal is not None and cohort_terminal < terminal:
                continue
            close.loc[index, cohort_symbol] = _bar(dataset, cohort_symbol, terminal_day).close
            valuation.loc[index, cohort_symbol] = _bar(dataset, cohort_symbol, terminal_day).close
        size.loc[index, symbol] = 0.0
        price.loc[index, symbol] = _bar(dataset, symbol, terminal_day).close
        terminal_liquidations += 1

    observations = int(size.notna().sum().sum())
    if not pit_observations or len({members for _, members in pit_observations}) < 2:
        raise POCDataError("DATA_ERROR_STOP: lifecycle/PIT was not materially exercised")
    return BaselineInputs(dates, symbols, close, valuation, size, price, len(pit_observations), terminal_liquidations, observations)


def build_portfolio(inputs: BaselineInputs):
    """Build the simulator with no valuation/close forward-fill defaults."""
    return vbt.Portfolio.from_orders(
        close=inputs.close,
        size=inputs.size,
        size_type="targetpercent",
        price=inputs.price,
        val_price=inputs.valuation,
        fees=FEE_RATE,
        slippage=ONE_SIDE_SLIPPAGE,
        cash_sharing=True,
        group_by=True,
        call_seq="auto",
        ffill_val_price=False,
        fillna_close=False,
        freq="1D",
        init_cash=1.0,
    )


def run_poc_from_inputs(dataset: PriceDataset, master: Mapping[str, Any]) -> dict[str, Any]:
    """Exercise vectorbt without surfacing any PnL, return, or alpha metric."""
    inputs = build_inputs(dataset, master)
    portfolio = build_portfolio(inputs)
    # Force evaluation of the generated order records but never publish their
    # financial fields: the artifact is an integration checksum, not research.
    order_count = int(portfolio.orders.count().sum())
    fingerprint = hashlib.sha256(
        _canonical(
            {
                "dates": [_iso(value) for value in inputs.dates],
                "symbols": list(inputs.symbols),
                "size": [["NA" if pd.isna(value) else float(value) for value in row] for row in inputs.size.to_numpy()],
                "valuation": [["NA" if pd.isna(value) else float(value) for value in row] for row in inputs.valuation.to_numpy()],
                "price": [["NA" if pd.isna(value) else float(value) for value in row] for row in inputs.price.to_numpy()],
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        "poc_id": POC_ID,
        "result": RESULT_LABEL,
        "alpha_claim": False,
        "framework": {"name": "vectorbt", "version": vbt.__version__},
        "price_v1": {"snapshot_id": SNAPSHOT_ID, "manifest_sha256": MANIFEST_SHA256, "pit_sha256": PIT_SHA256},
        "bounded_cohort_records_loaded": len(inputs.symbols),
        "pit_observations": inputs.pit_observations,
        "terminal_liquidations": inputs.terminal_liquidations,
        "order_observations": inputs.order_observations,
        "vectorbt_order_records": order_count,
        "execution": {
            "decision": "completed UTC close at t-1 day",
            "entry": "next UTC open at t",
            "final_exit": "explicit UTC open",
            "terminal": "canonical terminal-day open-to-final-close then forced exit",
            "fee_rate": FEE_RATE,
            "one_side_slippage": ONE_SIDE_SLIPPAGE,
        },
        "deterministic_input_sha256": fingerprint,
    }


def run_poc(data_root: pathlib.Path | None = None) -> dict[str, Any]:
    dataset, master = load_frozen_inputs(data_root)
    return run_poc_from_inputs(dataset, master)


def write_artifact(path: pathlib.Path, result: Mapping[str, Any]) -> None:
    """Write an explicitly requested, metric-free replay artifact."""
    path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    print(json.dumps(run_poc(), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
