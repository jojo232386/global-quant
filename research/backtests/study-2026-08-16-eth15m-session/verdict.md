# Verdict: REJECT

study id: `study-2026-08-16-eth15m-session`

## What fired

OOS Sharpe -34.5069 < 0.5 and OOS stress net return
-22.5% not > 0 → **REJECT** per the
predeclared rule. Trade-count and sign conditions were met.

## What the evidence says

- 366 OOS trades, win rate 0.194, profit factor
  0.1648, max drawdown 11.1%.
- Mean net per trade is close to the round-trip cost: no gross edge.
- Benchmark buy-and-hold: 4.6% gross
  (4.3% net).

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data integrity
(sha256 pinned). Unverified: taker fee placeholder, funding simplification.
Limitations: single instrument, single rule, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
