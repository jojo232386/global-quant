# Verdict: REJECT

study id: `study-2026-08-17-pit-xts-momentum`

## What fired

OOS Sharpe -4.427 < 0.5 and OOS stress -39.6% not > 0 → REJECT.

## What the evidence says

- 61 OOS rebalance events, 183 legs,
  OOS total -34.0%, max drawdown
  35.4%, win rate 0.3825.
- PIT benchmark (equal-weight first-OOS-day top-15): -8.1% gross.
- Bias comparison: biased run study-2026-08-16-multi-xts-momentum OOS +28.0% (PASS);
  PIT run -34.0%. The earlier PASS is explained by
  selecting today's winners; it is withdrawn as evidence of edge.

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data pins.
Unverified: candidate-pool survivorship residue, per-symbol corporate
actions. Limitations: single venue, 2-month OOS window.

## Status

Closed with REJECT. This study does not authorize live trading.
