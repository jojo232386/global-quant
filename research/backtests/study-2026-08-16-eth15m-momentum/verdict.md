# Verdict: REJECT

study id: `study-2026-08-16-eth15m-momentum`
run id: `run-2026-08-16-eth15m-momentum-1`

## What fired

Per the predeclared PASS/REJECT rule, PASS required ALL of:

- OOS annualized Sharpe >= 0.5 → actual **-48.3** ❌
- OOS net return > 0 under 2x cost stress → actual **-44.3%** ❌
- at least 30 OOS trades → actual 736 ✅
- train and OOS net returns same sign → both negative ✅

Two conditions failed → **REJECT**.

## What the evidence says

- The strategy loses almost exactly the round-trip cost: mean net return per
  trade is about -29 bps against a 30 bps cost, i.e. gross edge ≈ 0.
- Win rate 19.6% OOS, profit factor 0.22, max drawdown 21.2%, max 22
  consecutive losses on train.
- The buy-and-hold benchmark over the same OOS window returned +5.0% gross
  (+4.7% net); the strategy underperformed it decisively.

## Separation

- Verified results: metrics above, deterministic rule, no-lookahead
  execution (contract-tested), data integrity (18,817 bars, 0 gaps,
  0 duplicates, sha256 pinned).
- Unverified assumptions: taker fee is the VIP0 placeholder; funding not
  modeled beyond the 1 bps stress buffer.
- Limitations: single instrument, single rule, 2-month OOS window, no
  regime filter.

## Status

Study closed with REJECT. This is recorded failure evidence, exactly the
outcome the research loop is designed to surface honestly. It does not
authorize live trading and does not block other, separately preregistered
studies.
