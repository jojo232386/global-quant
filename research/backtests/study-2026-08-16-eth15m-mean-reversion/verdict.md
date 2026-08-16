# Verdict: REJECT

study id: `study-2026-08-16-eth15m-mean-reversion`

## What fired

OOS Sharpe -35.9161 < 0.5 and OOS stress net return
-42.0% not > 0 → **REJECT** per the
predeclared rule. Trade-count and sign conditions were met.

## What the evidence says

- 715 OOS trades, win rate 0.193, profit factor
  0.2172, max drawdown 19.8%.
- Mean net per trade is close to the round-trip cost: no gross edge.
- Benchmark buy-and-hold: 5.2% gross
  (4.9% net).

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data integrity
(sha256 pinned). Unverified: taker fee placeholder, funding simplification.
Limitations: single instrument, single rule, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
