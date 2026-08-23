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
