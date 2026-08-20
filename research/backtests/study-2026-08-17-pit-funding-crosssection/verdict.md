# Verdict: REJECT

study id: `study-2026-08-17-pit-funding-crosssection`

## What fired

OOS Sharpe -5.897 < 0.5 and OOS stress -40.5% not > 0 → REJECT.

## What the evidence says

- 61 OOS rebalance events, 183 legs,
  OOS total -34.9%, max drawdown
  41.5%, win rate 0.4044.
- PIT benchmark (equal-weight first-OOS-day top-15): -8.1% gross.
- Bias comparison: biased run study-2026-08-16-multi-funding-crosssection OOS +13.1% (PASS);
  PIT run -34.9%. The earlier PASS is explained by
  selecting today's winners; it is withdrawn as evidence of edge.

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data pins.
Unverified: candidate-pool survivorship residue, per-symbol corporate
actions. Limitations: single venue, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
