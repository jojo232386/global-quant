# Hypothesis Preregistration: PIT funding-shock dollar-neutral reversal

> Locked before this study's result is generated. Any result-driven parameter
> change creates a new study id. This research never authorizes an order.

## Identity and hypothesis

- study id: `study-2026-08-17-pit-funding-shock-neutral`
- date: 2026-08-17 UTC
- claim: within each day's PIT top-15 Binance USD-M universe, contracts whose
  latest published funding rate falls most relative to the prior three records
  subsequently outperform contracts whose funding rises most.
- mechanism: a funding shock is treated as a change in leveraged crowding, not
  as a forecast from the absolute funding level.
- difference from prior failures: the old rule ranked funding levels and held
  only low-funding longs. This rule ranks changes, holds both tails, and is
  equal-notional dollar-neutral. It is not a rescue or parameter edit of an old
  result.

## Frozen timing and portfolio

- sample: 2026-02-01 through 2026-08-16 UTC.
- train: before 2026-06-16 00:00 UTC; OOS starts at that timestamp.
- universe for day D: top-15 by D-2 complete daily quote volume, as recorded in
  `pit-universes.json`; the candidate pool remains today's top-100 and therefore
  has declared residual survivorship bias.
- signal cutoff: D-1 23:45 UTC. Only `fundingTime <= cutoff` is visible.
- score: latest funding rate minus the mean of the preceding three records.
- entry/exit: D 00:00 open to D+1 00:00 open.
- portfolio: long the lowest 3 scores and short the highest 3; 100 USDT per leg,
  equal notional, 1x, no intraday re-entry.
- a day is skipped unless six distinct symbols have four funding observations
  and both entry and exit prices. Missing observations are never filled.

## Costs, funding, and stress

- baseline fee plus slippage comes only from `configs/execution-costs.json`.
- each leg pays both entry and exit cost.
- long funding is subtracted; short funding is added using published timestamps
  crossed during the holding period.
- stress doubles configured fee/slippage, multiplies published funding by 5,
  and subtracts an additional 1 bp uncertainty buffer per leg.
- robustness is frozen as funding lookbacks 2 and 4, leg counts 2 and 4, two
  OOS halves, and a 15-minute adverse execution delay. These are diagnostics,
  not tuning candidates.

## PASS, REJECT, and INCONCLUSIVE

PASS requires every condition below:

- OOS Sharpe >= 0.5, baseline return > 0, stress return > 0;
- at least 50 OOS rebalance days and train/OOS returns have the same sign;
- OOS max drawdown <= 20%; no symbol exceeds 40% of absolute symbol PnL;
- both long and short sides trade;
- both OOS halves, every frozen parameter neighbor, and the delayed stress run
  remain positive;
- complete funding request-window evidence, a historical listing master, and
  current account-specific cost/risk inputs exist.

REJECT applies when the study is scoreable and any statistical or robustness
condition fails. Missing evidence never turns an observed statistical failure
into a softer outcome.

INCONCLUSIVE applies when fewer than 50 OOS rebalance days are scoreable, or
when statistical conditions pass but promotion inputs are incomplete. An
INCONCLUSIVE result is not evidence that the strategy works.

## Frozen parameter record

`lookback=3`, `long_legs=3`, `short_legs=3`, `holding=24h`,
`notional_per_leg=100 USDT`, `leverage=1x`, `rebalance=00:00 UTC`,
`split=2026-06-16T00:00:00Z`.

No threshold or sample split may be changed after `results.json` is observed.
