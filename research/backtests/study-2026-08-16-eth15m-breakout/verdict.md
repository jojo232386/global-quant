# Verdict: REJECT

study id: `study-2026-08-16-eth15m-breakout`

## What fired

OOS Sharpe -17.6424 < 0.5 and OOS stress net return
-8.2% not > 0 → **REJECT** per the
predeclared rule. Trade-count and sign conditions were met.

## What the evidence says

- 121 OOS trades, win rate 0.2149, profit factor
  0.186, max drawdown 4.4%.
- Mean net per trade is close to the round-trip cost: no gross edge.
- Benchmark buy-and-hold: 5.5% gross
  (5.2% net).

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data integrity
(sha256 pinned). Unverified: taker fee placeholder, funding simplification.
Limitations: single instrument, single rule, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
