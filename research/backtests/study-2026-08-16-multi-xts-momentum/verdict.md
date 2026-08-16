# Verdict: PASS (weak evidence)

study id: `study-2026-08-16-multi-xts-momentum`

## What fired

Per the predeclared rule: OOS Sharpe 2.2935 vs >= 0.5
(PASS), OOS stress net
22.2% vs > 0
(PASS), OOS
rebalance events 61 vs >= 30 (PASS), train/OOS sign
(PASS).
Result: **PASS**.

## What the evidence says

- OOS: 183 legs over 61 rebalances,
  total return 28.0%, Sharpe 2.2935,
  max drawdown 14.3%, win rate 0.3716.
- Stress (2x costs + funding buffer): 22.2%.
- Benchmark basket: 217.1% gross.

## Why the evidence is WEAK

1. The universe is today's top-15 by volume. Coins that pumped are
   over-represented by construction; backtesting "long recent winners" (or
   low-funding names during a melt-up) mechanically captures that pump.
   This is survivorship/selection bias, predeclared in the preregistration.
2. Single-symbol dominance: AKEUSDT (81x range, +3151% OOS bench leg) drives most of the OOS P&L.
3. A point-in-time universe (reconstructing the top-15 as of each rebalance
   date) is required before this is an investable claim.

## Separation

Verified: deterministic rule, no lookahead (contract-tested), data pins.
Unverified: taker fee placeholder, funding carry simplification, per-symbol
corporate actions. Limitations: single venue, 2-month OOS window,
selection-biased universe.

## Status

PASS by the predeclared rule, with weak evidence as documented.
This study does not authorize live trading.
