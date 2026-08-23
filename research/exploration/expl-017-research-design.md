# EXPL-017 Lead Research Design

Status: `HYPOTHESIS_ACTIVE_PRE_FORMAL`
Main baseline: `94100c7ca60444d8e72f0a8ff6fc70f57206aabb`

```ini
HYPOTHESIS_ID = EXPL-017
IMPLEMENTATION_ATTEMPT_ID = EXPL-017-IMPL-012
PRIOR_IMPLEMENTATION_ATTEMPT_ID = EXPL-017-IMPL-001 THROUGH EXPL-017-IMPL-011 INVALID_PRE_FORMAL
FORMAL_RUN_ID = NONE
MECHANISM = Cross-sectional momentum changes sign with the broad PIT universe volatility state: calm markets permit slow information diffusion and continuation, while high-volatility markets are dominated by forced deleveraging, correlation spikes, and rebound, producing relative reversal.
WHY_EDGE_MAY_EXIST = Pool-wide volatility is a state variable for the price-formation mechanism, not a second standalone rank. Conditioning the same ex-ante momentum score on that state can reveal opposing conditional rank relations that cancel in an unconditional test.
DELTA_FROM_FAILED_WORK = EXPL-001 always mapped winners long and losers short and used return dispersion only as an on/off gate; EXPL-017 removes that gate and reverses the rank-to-position mapping in high broad-market volatility. EXPL-003 ranked volatility itself as direction; EXPL-017 never ranks names by volatility for direction. EXPL-004 changed liquidity weights; EXPL-017 uses equal weights within legs. EXPL-008 was a BTC/ETH per-asset TSMOM low-vol on/off gate; EXPL-017 is a real PIT cross-section and tests a two-direction interaction. EXPL-014 is a sizing proposal; EXPL-017 keeps equal leg weights.
PREDICTION_HORIZON = 7 calendar days, open t+1 to open t+8, with the frozen terminal contract
HOLDING_HORIZON = 7 calendar days, rebalanced on the schedule anchored at 2021-01-01 UTC
REQUIRED_DATA = VERIFIED Price V1 only: open_time_utc_ms, open, close, quote_volume, monthly pit_universe
EXPECTED_FAILURE_MODE = Conditional rank IC remains absent; high-volatility reversal is too brief or too costly; the expanding state boundary is unstable; one regime, symbol, or subperiod dominates; lifecycle exits or turnover erase the apparent spread.
```

## Mechanism and formula

At decision close `t`, use the PIT universe effective at execution open `t+1`.
Select the top `N` names by median quote volume over the 90 completed UTC
daily bars through `t`, descending with canonical symbol as tie-break. A name
must have every required bar and the execution open.

For each selected name, calculate completed-close returns over 7, 14, and 28
days. Rank each horizon cross-sectionally from 0 to 1 with average ranks for
ties, then average the three ranks. This is the frozen EXPL-001 momentum
measurement, deliberately held fixed so the tested delta is the interaction.

For the same selected names, calculate annualized sample volatility from 30
completed close-to-close returns through `t`; the broad volatility statistic
is the median across names. During train, compare it only with the expanding
median of at least eight prior valid train decisions. In OOS and holdout, use
the single median of all valid 2021 train decision statistics. Equality is
calm. No OOS or holdout value enters the boundary.

- Calm: long the top momentum quintile and short the bottom quintile.
- High volatility: long the bottom momentum quintile and short the top
  quintile.
- Each leg has absolute gross 0.5 and is equal-weighted; portfolio gross is
  one and net is zero.
- The first eight train decisions are regime warmup and hold cash.

The primary is `N=30`, 30-day volatility, 7-day rebalance, and the fixed
7/14/28-day momentum composite. The only small neighborhood permitted for the
later freeze is `N in {20, 30}` by volatility window in `{21, 30}` days.
Momentum horizons, quintile legs, expanding-median state rule, costs, schedule,
and splits are not tunable.

## Timing, lifecycle, and costs

Signals use data known through UTC close `t` and execute at UTC open `t+1`.
The first earned return is open `t+1` to the next UTC open. Internal missing
bars, missing entry opens, invalid values, or malformed PIT records are a data
stop. If a held contract reaches its verified terminal bar, it earns final
open-to-final-close return, is liquidated at that close, and pays an exit cost;
it is never forward-filled. The portfolio remains continuous across split
boundaries and is finally liquidated on 2023-12-31.

Turnover is `sum(abs(target_weight - incumbent_weight))`, including both legs,
terminal exits, and final liquidation. Baseline cost is 15 bps per unit of
one-way turnover (5 bps fee plus 10 bps slippage); stress is 30 bps. Funding is
not modeled, so no result can be a tradable total-return or live claim.

## Split containment

- Train: 2021-01-01 through 2021-12-31.
- OOS: 2022-01-01 through 2022-12-31.
- Final untouched holdout for EXPL-017: 2023-01-01 through 2023-12-31.
- Pre-2021 rows are warmup only. No 2024+ price bar may be read.

Implementation must expose a correctness-only path that loads the dataset,
replays the gold sample, and checks timing/lifecycle/cost invariants without
computing or serializing OOS or holdout performance. Formal performance stays
locked until the correctness review passes and a separate frozen contract is
committed.

## Stop and classification rules

- Dataset cannot satisfy this design: `DATA_UNAVAILABLE` for EXPL-017 only.
- Gold oracle or implementation invariant fails: mark only the current
  `IMPLEMENTATION_ATTEMPT_ID` as `INVALID_PRE_FORMAL`; the hypothesis remains
  active and no market conclusion exists.
- Frozen gates fail after the one allowed run: `HYPOTHESIS_FAIL` and enter the
  Factor Graveyard.
- Every frozen gate passes: `EXPLORATION_PASS` only.

Price V1 is survivor-biased and exploration-only. `PROMOTABLE_ALPHA`,
`STRATEGY_READY`, and `LIVE_READY` are prohibited conclusions.

This document fixes the EXPL-017 mechanism. It is not a formal freeze commit.
No `FORMAL_RUN_ID` may exist until a clean implementation attempt passes the
independent correctness review.

`EXPL-017-GOLD-ORACLE-002` was committed before either production attempt and
its arithmetic passed independent review. It is reused byte-for-byte by
EXPL-017-IMPL-012 because the hypothesis, mechanism, parameters, costs, and
lifecycle contract are unchanged.
