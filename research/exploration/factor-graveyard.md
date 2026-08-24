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
