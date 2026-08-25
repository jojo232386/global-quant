# Factor Graveyard

Status: `ACTIVE_NEGATIVE_MEMORY`

This file records price-factor hypotheses that failed their frozen
exploration contract. A graveyard entry cannot be revived by changing a
parameter after seeing its result. A materially different hypothesis requires
a new experiment id and a new pre-run freeze.

## Price Alpha batch v1 · 2026-08-23

Shared binding: curated Price V1 snapshot `a7d65a92`; archive-extended,
survivor-biased, exploration-only. All three experiments used next-open
execution, train/OOS separation, declared parameter neighborhoods, flat and
stressed fees/slippage, multi-period diagnostics, and symbol contribution
checks.

```ini
FUNDING_NOT_MODELED = TRUE
LIVE_PROMOTION = BLOCKED
ALPHA_PROMOTION_PASS = FALSE
LIVE_READY = FALSE
PRODUCTION_READY = FALSE
```

### EXPL-001 · FAIL · multi-horizon rank momentum

- Reason: positive OOS portfolio P&L was not accompanied by positive,
  statistically credible rank IC and did not reproduce the train behavior.
- Interpretation: a gated portfolio path happened to earn during the OOS
  window, but the price rank did not demonstrate repeatable cross-sectional
  prediction under the frozen test.
- Artifact: `expl-001-report.json`.
- Re-entry rule: no parameter rescue under EXPL-001. A new id needs a new
  mechanism and a pre-run distinction from this rank/gate specification.

### EXPL-003 · FAIL · low-volatility anomaly

- Reason: the primary train and OOS long/short portfolios were negative after
  costs and the declared parameter neighborhood was unstable.
- Interpretation: OOS rank IC alone did not translate into an investable
  top-versus-bottom spread; predictive ordering and portfolio profitability
  are therefore not treated as equivalent.
- Artifact: `expl-003-report.json`.
- Re-entry rule: no window/frequency rescue under EXPL-003. Any new id must
  explain why a different portfolio mapping tests a genuinely different
  mechanism rather than mining the same rank result.

### EXPL-004 · FAIL · liquidity tilt inside momentum

- Reason: the tilt inherited EXPL-001's train and IC failures and lagged the
  frozen untilted comparator under the declared nonzero cost lenses.
- Interpretation: lower-volume weighting added concentration/cost exposure
  without establishing incremental price prediction or portfolio value.
- Artifact: `expl-004-report.json`.
- Re-entry rule: no multiplier or decile rescue under EXPL-004. A new id needs
  measured liquidity/execution data or a genuinely different mechanism.

## EXPL-017 formal-003 · 2026-08-24

Shared binding: curated Price V1 snapshot `a7d65a92`; exception-only
Lifecycle V1 dataset `c7f0f9ea`; survivor-biased, exploration-only. Formal
run `EXPL-017-FORMAL-003` ran exactly once under freeze contract SHA256
`0543eba0641077afc7fec2d40d4aeb6b2873cc10a4225bd1c2826213dc9d0071`.

### EXPL-017 · HYPOTHESIS_FAIL · momentum sign by broad-volatility state

- Reason: the primary final-holdout Sharpe was `0.14 < 0.50`; the 30 bps
  stress holdout return was `-8.93%`; holdout signed mean IC was `-0.05`
  and combined IC t-stat was `0.79 < 1.50`; the high-volatility regime had
  `12 < 20` admitted observations; and maximum absolute incumbent/target
  weight was `16.30% > 15%`.
- Counter-evidence preserved: baseline OOS return was `45.66%` with Sharpe
  `1.44`, while final-holdout return fell to `0.37%`; multi-period,
  neighborhood, and lifecycle gates passed. The frozen contract required all
  gates, so partial success cannot rescue the hypothesis.
- Artifact: `expl-017-formal-003-result.json`; SHA256
  `c55a4ddc5e8919f5ddd87b19d810d009851661c0a6828a283a285e87b08e347f`.
- Lessons learned: process-level interpretation and non-repeat rules are
  recorded in `../process/LESSONS_FROM_EXPL_017.md`; this link does not alter
  the frozen result or reopen the hypothesis.
- Re-entry rule: no parameter, formula, split, cost, universe, lifecycle, or
  threshold rescue under EXPL-017, and no rerun of FORMAL-003. A future id
  requires a materially different mechanism and a fresh pre-result contract.

```ini
FUNDING_NOT_MODELED = TRUE
ALPHA_STATUS = HYPOTHESIS_FAIL
PROMOTABLE_ALPHA = FALSE
STRATEGY_READY = FALSE
LIVE_READY = FALSE
```

## Price/Lifecycle Sprint 001 · 2026-08-25

Shared binding: Price V1 snapshot `a7d65a92`; bounded PIT instrument
cohort `BINANCE_USDM_PERPETUAL_TRADING_20210104_195102Z`; canonical
Lifecycle V1; support strictly before `2023-11-14T00:00:00Z`. The whole
window, including 2023, was Tier 1 exploration data rather than an untouched
holdout. Both candidates were tested once in frozen order with no parameter,
window, universe, or success-criterion rescue.

### HYP-PLS001-001 · TIER1_FAIL · idiosyncratic shock reversal

- Reason: the 30 bps stress mean daily return was negative, annualized
  Sharpe was below the frozen threshold, maximum drawdown and median turnover
  breached their limits, all three subperiod returns were negative, both
  declared variants had negative stress means, and the fixed
  largest-positive-contributor removal did not pass.
- Counter-evidence preserved: mean rank IC was positive and the fixed lag-3
  HAC diagnostic was in the expected direction, while concentration and
  lifecycle/PIT correctness checks passed. Those partial positives cannot
  rescue the portfolio-level failure.
- Artifact: `price-lifecycle-sprint-001-result.json`; SHA256
  `96bae0c27e801c5fcb50118d64f12e76764a45a876ad2b23f9f911b8027e7101`.
- Re-entry rule: no sigma window, holding-period, rebalance, cohort, cost, or
  threshold rescue under this hypothesis ID.

### HYP-PLS001-002 · TIER1_FAIL · volume-share migration

- Reason: the primary stress mean and Sharpe were negative, mean rank IC was
  negative, only one of three subperiod returns was nonnegative, drawdown and
  median turnover breached their limits, both declared variants failed the
  expected direction, and the fixed removal sensitivity did not pass.
- Interpretation: quote-asset volume remains a turnover and attention proxy;
  this failure is not evidence about net capital flow.
- Artifact: `price-lifecycle-sprint-001-result.json`; SHA256
  `96bae0c27e801c5fcb50118d64f12e76764a45a876ad2b23f9f911b8027e7101`.
- Re-entry rule: no short/baseline volume window, cohort, cost, or threshold
  rescue under this hypothesis ID.

```ini
PROGRAM_STATUS = PRICE_LIFECYCLE_SPRINT_001_EXHAUSTED
CANDIDATES_PREREGISTERED = 2
CANDIDATES_TESTED = 2
PASS_COUNT = 0
FAIL_COUNT = 2
FUNDING_NOT_MODELED = TRUE
FORMAL_CONFIRMATION = FALSE
STRATEGY_READY = FALSE
LIVE_READY = FALSE
```

## Price/Lifecycle Sprint 002 Checkpoint A · 2026-08-25

No candidate performance was run. The custom path stopped at its independent
pre-performance review and switched to the OSS fallback required by the
mission; the OSS benchmark is not a replacement Alpha result.

### HYP-PLS002-001 · REJECTED_AS_VARIANT · market-beta residual momentum

- Reason: beta residualization changes the return representation but retains
  EXPL-001's closed information-diffusion and momentum mechanism.
- Artifact: `price-lifecycle-sprint-002-checkpoint-a.json`.
- Re-entry rule: no residual window, beta window, holding period, cohort, cost,
  or threshold rescue under this hypothesis ID.

### HYP-PLS002-002 · NOT_RUN · range-volume price acceptance

- Reason: the one-shot custom program stopped at Checkpoint A before any
  performance exposure. This is not a factor failure and makes no empirical
  claim about the mechanism.
- Artifact: `price-lifecycle-sprint-002-checkpoint-a.json`.
- Re-entry rule: do not run this candidate under Sprint 002 and do not infer
  failure or success from the OSS benchmark.

```ini
PROGRAM_STATUS = PRICE_LIFECYCLE_SPRINT_002_STOPPED_AT_CHECKPOINT_A
CANDIDATES_PREREGISTERED = 2
CANDIDATES_TESTED = 0
REJECTED_AS_VARIANT_COUNT = 1
NOT_RUN_COUNT = 1
CUSTOM_ALPHA_PATH_EXHAUSTED = TRUE
OSS_FALLBACK_TRIGGERED = TRUE
```
