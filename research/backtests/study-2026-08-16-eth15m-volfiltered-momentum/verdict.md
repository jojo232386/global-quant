# Verdict: REJECT

study id: `study-2026-08-16-eth15m-volfiltered-momentum`

## What fired

OOS Sharpe -23.7021 < 0.5 and OOS stress net return
-23.9% not > 0 → **REJECT** per the
predeclared rule. Trade-count and sign conditions were met.

## What the evidence says

- 405 OOS trades, win rate 0.1877, profit factor
  0.2295, max drawdown 10.9%.
- Mean net per trade is close to the round-trip cost: no gross edge.
- Benchmark buy-and-hold: 5.0% gross
  (4.7% net).

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data integrity
(sha256 pinned). Unverified: taker fee placeholder, funding simplification.
Limitations: single instrument, single rule, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
