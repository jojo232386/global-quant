# Exploration Hypothesis Backlog

Status: `ACTIVE` · Protocol: `EXPLORATION_PROTOCOL.md`

All cards are exploratory seeds under the exploration tier. Nothing here is
evidence, a PASS, or a promotion claim. Screens run train-only (data ending
2023-12-31) with the conservative cost baseline. Every card states its delta
from formally closed hypotheses; a card that cannot state a delta is invalid.

Drop line default for all cards: train-window net-of-cost Sharpe < 0.5 after
cost/funding stress, or any stress scenario flipping the sign of a thin
positive → `DROPPED`.

## Price Alpha batch v1 · FROZEN BEFORE RUN 2026-08-23

This block is the preregistered comparison and judgment contract for
`EXPL-001`, `EXPL-003`, and `EXPL-004`. It was frozen before their shared
runner or result artifacts existed. Any change after a result is observed
requires a new experiment id; the old id remains classified and, if failed,
is copied to the Factor Graveyard.

Pre-run review amendment: before runner implementation or any result, the
execution point was moved to next-day open, the dispersion gate was defined
on raw composite-return IQR, and the train gate became expanding-only. This
paragraph records that final pre-run freeze; it is not a post-result rescue.

- Dataset: curated Price V1
  `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`;
  registry integrity and every manifest file hash must replay as `VERIFIED`
  before calculation. Fields are only `open_time_utc_ms`, `open`, `close`,
  `quote_volume`, and the monthly `pit_universe`. No funding, OI, news, or
  external data.
- Common sample: signals and returns from `2021-01-01` through
  `2023-12-31`; train is calendar 2021 and OOS is calendar 2022–2023.
  Earlier rows may warm trailing calculations only. Nothing after 2023 is
  read or used for selection.
- Timing: a signal calculated with fields known through close t is executed
  at open t+1 and first earns the open-t+1 to open-t+2 return. A terminal
  contract bar is valued from its final open to its final close, liquidated
  at that close, charged an exit cost, and counted; it is never forward-filled
  or silently skipped. A required entry open, internal timestamp, duplicate,
  non-positive price, or effective PIT record that is absent is a hard stop.
- Portfolio: every long/short book is dollar neutral with gross exposure one:
  long leg `+0.5`, short leg `-0.5`. A rebalance cost is the sum of absolute
  asset-weight changes, so a leg switch charges both sides. Gross exposure is
  never levered above one.
- Cost grid, per unit of one-way turnover: zero-cost diagnostic, 15 bps
  baseline (5 bps fee plus 10 bps slippage), and 30 bps stress. Funding is
  deliberately not modeled, so even a survivor cannot become a formal or
  live claim.
- Required output: gross and each cost-lens total/annual return, volatility,
  Sharpe, maximum drawdown, win rate, turnover, trade/rebalance count, IC and
  rank IC, HAC lag-3 one-sided normal p-value for mean rank IC, forward-return
  quintiles, long/short spread, symbol contribution concentration, and
  regime diagnostics.
- Regimes are diagnostics only and are known at t: trailing BTC 90-day return
  above 20% is bull, below -20% is bear, otherwise sideways; the label is
  attached to the next return bar. It never gates or sizes a position.
- Common OOS graduation bars for the frozen primary configuration: train
  baseline Sharpe and mean rank IC are positive; OOS baseline annual return
  is positive and Sharpe is at least 0.5; OOS 30 bps total return and Sharpe
  are positive; OOS mean rank IC is positive with one-sided p at most 0.05;
  no symbol exceeds 25% of absolute symbol P&L contribution; at least two of
  bull/bear/sideways have positive OOS baseline P&L and no one positive
  regime supplies more than 75% of positive regime P&L. The card-specific
  parameter-neighborhood rule must also pass.
- Classification: all common and card-specific bars →
  `EXPLORATION_PASS`; otherwise → `FAIL` and Factor Graveyard. Because the
  dataset is survivor-biased, `ALPHA_PROMOTION_PASS`, `LIVE_READY`, and
  `PRODUCTION_READY` are impossible outcomes.

## Family A — Cross-section on a real PIT universe

Prior closure context: PIT funding cross-section and 24h momentum were
formally REJECTED on a top-100 PIT pool; the two-symbol "cross-sections"
were not cross-sections at all. Family A retries the layer (not the signal)
with conditioning.

### EXPL-001 · multi-horizon rank momentum · FAIL
- Screened 2026-08-23 under the frozen Price Alpha batch v1 contract.
  Portfolio-level OOS returns survived the declared cost stress, but train
  performance and train/OOS rank IC did not support a repeatable predictive
  factor. The card failed without parameter rescue; exact figures remain in
  `expl-001-report.json` and the negative record is in `factor-graveyard.md`.
- Hypothesis: dollar-neutral long/short by multi-horizon (1w + 2w + 4w
  averaged) return rank across the top-N (N=30) PIT-by-dollar-volume
  universe, rebalanced weekly, only when cross-sectional rank dispersion is
  above its train median.
- Mechanism: information diffusion and under-reaction can persist at several
  horizons; requiring agreement across ranks and high cross-sectional
  dispersion avoids trading when relative information is weak.
- Formula and fields: within the monthly PIT universe effective at execution
  t+1, select top N by median `quote_volume` over the 90 completed daily bars
  through decision close t. For each
  name rank `close[t]/close[t-h]-1` at h in {7, 14, 28}; signal is the mean
  of the three normalized cross-sectional ranks. Long the top 20% and short
  the bottom 20%. The gate statistic is the cross-sectional IQR of the raw
  mean of the three horizon returns. In train it is compared only with the
  expanding median of at least four prior valid train rebalance dates; in OOS
  it is compared with the fixed median of all train rebalance-date IQRs for
  the same N/frequency.
- Holding and direction: execute every 7 or 14 calendar days anchored on the
  `2021-01-01` open, using the prior close's signal; hold the dollar-neutral
  long-winners/short-losers book to the next scheduled execution. The frozen
  primary is N=30, every 7 days.
- Data: curated price V1 `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`;
  archive-extended, survivor-biased, exploration-only.
- Fixed grid: N ∈ {20, 30, 50}; rebalance ∈ {w, 2w}. Nothing else.
- Parameter stability: at least four of six grid points must have positive
  OOS baseline Sharpe and at least three of six must have positive OOS
  stress Sharpe. The primary cannot be replaced by the best grid point.
- Expected failure: rapid reversals, crowded momentum unwinds, weak rank
  agreement, high turnover/cost, OOS IC loss, concentration, regime
  dependence, or instability across N/frequency.
- Delta vs closed: dispersion gating + multi-horizon averaging + real N≥20
  universe, vs unconditional 24h momentum on top-100.

### EXPL-002 · short-term reversal conditioned on funding pressure · BLOCKED_ON_DATA
- Hypothesis: 1–3 day reversal within the PIT universe, taken only in names
  whose funding z-score is extreme (crowded positioning unwinds harder).
- Fixed grid: reversal lookback ∈ {1d, 2d, 3d}; funding z threshold ∈ {2, 3}.
- Delta vs closed: funding as a conditioning gate, never as the signal; the
  closed funding cross-section used funding itself as the ranking signal.
- Data blocker: price V1 is ready, but `funding_verdict=FAIL`; this card does
  not run until a funding-complete snapshot exists.

### EXPL-003 · low-volatility anomaly cross-section · FAIL
- Screened 2026-08-23 under the frozen Price Alpha batch v1 contract.
  OOS rank ordering was detectable, but it did not monetize into a positive
  primary long/short portfolio after costs; train, stress, and parameter
  neighborhood requirements failed. Exact figures remain in
  `expl-003-report.json` and the negative record is in `factor-graveyard.md`.
- Hypothesis: long bottom realized-vol tercile / short top tercile of the PIT
  universe, monthly, vol computed on trailing 30d.
- Mechanism: leverage constraints and preference for lottery-like high-beta
  assets can overprice high-volatility names; the anomaly fails if crypto
  compensates volatility with higher forward returns or crash reversals.
- Formula and fields: realized volatility is the sample standard deviation
  of daily close-to-close returns over 14 or 30 completed bars through t,
  annualized by square-root 365. Signal is negative realized volatility.
  Within the current PIT universe, long the lowest-vol tercile and short the
  highest-vol tercile with equal weight inside each leg. Only `close`,
  `open_time_utc_ms`, and monthly `pit_universe` are used.
- Holding and direction: execute at the first open of every one or two
  calendar months, anchored in January 2021 and using the prior close's
  signal, then hold the dollar-neutral long-low-vol/short-high-vol book. The
  frozen primary is 30 bars and monthly.
- Fixed grid: vol window ∈ {14d, 30d}; rebalance ∈ {m, 2m}.
- Parameter stability: at least three of four grid points must have positive
  OOS baseline Sharpe and at least two of four must have positive OOS stress
  Sharpe. The primary cannot be replaced by the best grid point.
- Expected failure: volatility risk is positively rewarded, the short leg
  suffers convex squeezes, the effect is a single-regime artifact, costs erase
  the spread, rank IC is absent OOS, or results flip across window/frequency.
- Data: curated price V1 `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`;
  archive-extended, survivor-biased, exploration-only.
- Delta vs closed: equities-style low-vol anomaly was never formally tested
  in GMAQ; single-symbol vol-filtered momentum is a different family.

### EXPL-004 · liquidity tilt inside momentum · FAIL
- Screened 2026-08-23 under the frozen Price Alpha batch v1 contract.
  The tilted book inherited EXPL-001's train/IC failures and did not improve
  on the untilted comparator across the declared nonzero cost lenses. Exact
  figures remain in `expl-004-report.json` and the negative record is in
  `factor-graveyard.md`.
- Hypothesis: EXPL-001's momentum legs tilted toward lower dollar-volume
  deciles within the universe (slower crowding), with cost model penalizing
  the tilt honestly.
- Mechanism: information may diffuse more slowly in less liquid contracts,
  but any extra predictability is useful only if it exceeds their higher
  execution uncertainty.
- Formula and fields: freeze the EXPL-001 N=30, 7-day signal and dispersion
  gate independently of EXPL-001's outcome. The `none` comparator is equal
  weight inside each selected leg. `bottom-3-decays-only` multiplies selected
  names in quote-volume deciles 1, 2, and 3 by 1.5, 4/3, and 7/6 respectively,
  and all others by one, then renormalizes each leg to 0.5. Volume deciles use
  the same trailing 90-bar median `quote_volume` known through decision close
  t.
- Holding and direction: execute every 7 calendar days from the
  `2021-01-01` open using the prior close's signal, long momentum winners and
  short losers, and hold until the next execution.
- Fixed grid: tilt ∈ {none, bottom-3-decays-only}.
- Liquidity cost lens: in addition to flat 15/30 bps, apply per-side
  surcharges of 15, 10, and 5 bps to volume deciles 1, 2, and 3; the stress
  lens doubles fee, slippage, and surcharge together. The same asset-level
  schedule applies to tilted and untilted books.
- Card-specific graduation: the tilted primary must meet the common bars
  under flat and liquidity-aware baseline/stress costs and beat `none` on
  OOS Sharpe in at least three of the four nonzero cost lenses. Momentum IC
  is reported but cannot by itself prove that the liquidity tilt adds value.
- Expected failure: lower-volume names only add cost/noise, performance comes
  from the un-tilted momentum base, the tilt concentrates symbol or regime
  contribution, or the apparent improvement disappears under the
  liquidity-aware cost lens.
- Data: curated price V1 `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`;
  archive-extended, survivor-biased, exploration-only.
- Delta vs closed: liquidity as a portfolio construction axis is untested;
  explicitly cost-lens-first.

## Family B — Conditioned funding / basis structure

Prior closure context: unconditional spot-perp carry REJECTED (9 active
symbol-weeks, all stresses negative); funding-shock neutral reversal
REJECTED. Family B never trades funding level as a standalone signal.

### EXPL-005 · funding-extremity × momentum interaction · DRAFT
- Hypothesis: cross-sectional momentum works differently when funding
  extremes flag crowding; interact momentum rank with funding z (reduce
  momentum exposure when funding is extreme in the direction of the trade).
- Fixed grid: funding z ∈ {1.5, 2.5}; interaction ∈ {halve, zero}.
- Delta vs closed: interaction term, not additive funding signal.

### EXPL-006 · regime-gated carry · DRAFT
- Hypothesis: spot-perp carry taken only when realized-vol regime is below
  its train median and funding slope is positive; flat otherwise.
- Fixed grid: vol window ∈ {7d, 30d}; carry threshold ∈ {median, +1σ}.
- Delta vs closed: unconditional carry is dead; this tests conditionality,
  and expects far fewer than 9 symbol-weeks of exposure is acceptable only
  if per-week edge is materially higher — screen reports exposure count.

### EXPL-007 · funding settlement window drift · DRAFT
- Hypothesis: hour-level drift around 8h funding settlements in
  high-funding-extremity names.
- Fixed grid: window ∈ {±1h, ±2h}; extremity z ∈ {2, 3}.
- Delta vs closed: event-study family, never tested; hourly granularity from
  existing 1h klines.

## Family C — Volatility structure

Prior closure context: unconditional vol-scaled LS-TSMOM was formally
REJECTED with materially negative OOS return and extreme short-side
adverse excursion (figures in the formal study artifacts). Family C uses
vol as a gate or target, not as a signal direction.

### EXPL-008 · vol-regime gated trend · SCREENED 2026-08-22 · DROPPED_COST_FRAGILE
- Screen result: the gate never beats the ungated benchmark on Sharpe and
  Calmar jointly at any grid point, baseline or stressed; the calm-regime
  concentration it buys costs more return than risk it saves. Numbers in
  `expl-screen-results-v2-2026-08-22.json`.
- Comparison spec (frozen before code; deviations may only be labeled
  diagnostic):
  - Universe: BTC/ETH per-asset diagnostic breadth (no cross-sectional
    claim). Data: curated 88d9ff34 1d closes + funding.
  - Signal: per-asset TSMOM, position ∈ {+1, -1} = sign of trailing 30d
    return (lookback fixed at 30d), evaluated daily at close t, effective
    for bar t.
  - Gate: per-asset realized vol over grid window ∈ {14d, 30d} versus its
    own EXPANDING percentile (history up to t only); gate ∈ {on below
    expanding median, on below expanding bottom-tercile boundary}.
    Position = signal × gate.
  - Costs: |Δposition| × per-side cost per change day; shorts are perps,
    funding applied as position × daily funding sum (from curated funding
    files; the formal layer's 5x funding stress is not part of the
    screen — noted, not claimed).
  - Stress: 2x per-side trade costs.
  - Benchmark: the SAME ungated TSMOM signal with identical costs and
    funding. Beats = gated net Sharpe > AND net Calmar > ungated, full
    precision. Primary = best baseline net Sharpe among grid points that
    still beat ungated under stress; none → DROPPED_COST_FRAGILE.
  - Grid (frozen): vol window {14d, 30d} × gate {median, tercile}.
- Original card: LS-TSMOM exposure only in below-median realized-vol
  regimes (trends persist in calm; churn in stress).
- Delta vs closed: gate vs always-on; the formally rejected study was
  unconditional and vol-scaled in sizing, not gated in time.

### EXPL-009 · vol-of-vol leverage filter · DRAFT
- Hypothesis: scale any base exposure down when vol-of-vol (std of realized
  vol changes) is in its top tercile.
- Fixed grid: lookback ∈ {21d, 63d}; scale ∈ {0.5, 0}.
- Delta vs closed: risk-overlay family; tested as an overlay on EXPL-001's
  base, not standalone.

### EXPL-017 · cross-sectional momentum × broad-volatility state · PRE_FORMAL_ACTIVE
- Hypothesis status: active; no formal freeze, formal run, performance result,
  market conclusion, or Factor Graveyard entry exists.
- Mechanism: keep the frozen EXPL-001 7/14/28 cross-sectional momentum
  measurement, remove its dispersion gate, and map the same rank to
  continuation in calm broad-universe volatility and reversal in high
  broad-universe volatility. Volatility is a market state, never a standalone
  per-name direction rank or sizing input.
- Delta: unlike EXPL-001 this is a two-direction state interaction, not an
  always-continuation rank with an on/off dispersion gate; unlike EXPL-003 it
  does not trade the volatility rank; unlike EXPL-004 it has no liquidity
  tilt; unlike EXPL-008 it is PIT cross-sectional and reverses direction in
  high volatility rather than gating BTC/ETH TSMOM off; unlike EXPL-014 it
  does not vol-scale weights.
- `EXPL-017-IMPL-001` through `EXPL-017-IMPL-013` are
  `INVALID_PRE_FORMAL`:
  implementation defects invalidated those attempts only.
  `EXPL-017-IMPL-014` is retained as `CORRECTNESS_PASS_CORE_ONLY` at reviewed
  implementation SHA `f143ad8ee09479e7c74d95acf3af29bdca5bbbd2`.
  `EXPL-017-FORMAL-001` froze at `0e0b7f8` but is closed as
  `PROCESS_DEFECT / NOT_RUN`: its last scheduled decision requires a
  seven-day IC ending in 2024 while the same freeze prohibits 2024 bars.
  `EXPL-017-FORMAL-002` passed its static horizon preflight (157 schedule
  rows; IC 44/51/51 in train/OOS/holdout) and froze at `5e36196`, but an
  independent contract review closed it as `INVALID_BEFORE_EXECUTION` because
  the bound reviewed implementation remains formal-locked and no complete
  formal IC/runtime consumer was committed before freeze.
  `EXPL-017-IMPL-015` then attempted only the missing formal consumer, but
  stopped before code as `DATA_UNAVAILABLE_PRE_FORMAL`: the exact VERIFIED
  Price V1 snapshot has 208 kline roles plus PIT universe and summary, with no
  lifecycle/event role capable of supplying `lifecycle_as_of`. Inferring a
  terminal event from a future missing bar is prohibited lookahead. The only
  historical lifecycle audit is outside this lineage and is itself FAIL on
  `AKROUSDT:TERMINATED_UNCONFIRMED`, so it was not imported or used.
  Formal run count is zero and formal OOS/holdout performance remains unread;
  EXPL-017 stays active and has no market verdict or Factor Graveyard entry.

## Family D — Regime and breadth

### EXPL-011 · breadth risk switch · DRAFT
- Hypothesis: any long-biased exposure is cut when market breadth (fraction
  of PIT universe above 200d MA) falls below its train 20th percentile.
- Fixed grid: MA ∈ {100d, 200d}; threshold ∈ {10th, 20th pct}.
- Delta vs closed: breadth is a market-level gate, unavailable to
  single-symbol studies; overlay on EXPL-010 base.

### EXPL-012 · BTC/ETH relative strength rotation · SCREENED 2026-08-22 (corrected, tested) · KEPT_PRIMARY_SELECTED
- Status: kept under the frozen judgment rule; primary config selected and
  recorded in the results JSON. Passes both the baseline and the 2x-cost
  stress gate at the surviving points; the margin is thin. Formal
  confirmation, if any, tests the primary config only; the remaining grid
  is sensitivity diagnostics that cannot rescue the primary result.
  Implementation is covered by `tests/test_exploration_screens.py`
  (benchmark semantics, window alignment, cost placement, full-precision
  judgments, timestamp validation). Full numbers:
  `expl-screen-results-2026-08-22.json`.
- Original card: rotate between BTC and ETH by relative strength with
  hysteresis; grid lookback ∈ {14d, 30d}, hysteresis band ∈ {±3%, ±5%};
  benchmark static 50/50.

### EXPL-010 · target-vol portfolio of the universe · NOT_SELECTED
- `EXPL-010_FULL`: not run. Route review found that expanding the failed N=2
  target-vol mechanism to the wider Price V1 universe is a breadth change,
  not a sufficiently distinct market mechanism. It is not a fallback or
  parameter-rescue candidate for this Price Alpha line.
- `EXPL-010_N2_DIAGNOSTIC = DROPPED`: N=2 BTC/ETH diagnostic fails the
  card's Calmar/MDD graduation bar at every grid point. Direction consistent
  with the ASQ A5-1 outcome; directional consistency only, no
  cross-market claim is made.
- Original card: top-N universe portfolio at constant target vol with
  rebalance bands vs unscaled buy-and-hold; graduation bar Calmar/MDD.
  Grid target ∈ {15%, 20%, 30%}; band ∈ {±10%, ±20%}.

## DATA AVAILABILITY (updated 2026-08-23)

- Price breadth is ready: curated V1
  `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`,
  labeled archive-extended, survivor-biased, exploration-only. It unlocks
  price-only Family A cards, EXPL-013, and full-breadth EXPL-010.
- Funding remains blocked by `funding_verdict=FAIL`; EXPL-002 and all other
  funding-conditioned breadth cards remain blocked.
- Clean pre-2024 data available: curated `88d9ff34` (BTC/ETH 1d + funding +
  mark-8h, 2020-01..2026-08) and curated `9601a8ff` (BTC/ETH spot/perp 8h).
- The prior 2026-only continuation PIT files remain tainted and are not used
  for these train-window screens.

## Family E — Portfolio construction

### EXPL-013 · banded inverse-vol universe portfolio · DATA_ERROR_STOP
- The frozen run stopped before producing performance results. The May 2022
  PIT/volume rule selected `LUNAUSDT`, whose verified curated series ends on
  2022-05-13; the required 2022-05-14 open is absent. The frozen contract says
  a missing held-symbol value is `DATA_ERROR_STOP`, so no interpolation,
  post-run delisting exit rule, PASS/FAIL classification, or Factor Graveyard
  entry is permitted. Bound incident evidence:
  `expl-013-data-error.json`.
- Frozen contract: `expl-013-preregistration.json`. The contract must be
  committed before implementation or result generation; any later change to
  mechanism, formula, data, split, parameters, costs, benchmarks, or gates
  requires a new experiment id. The primary cannot be replaced by a grid
  winner.
- Hypothesis: heterogeneous crypto volatility makes an equal-weight broad
  portfolio concentrate risk in the most volatile contracts. Inverse-vol
  sizing can distribute risk more evenly; a wide relative drift band should
  preserve most of the unbanded inverse-vol risk benefit while using at most
  75% of its turnover. This is a portfolio-construction hypothesis, not a
  directional return-prediction factor.
- Formula: on each scheduled decision close, first form the eligible set from
  names active in the current PIT universe with complete 90-bar volume and
  31-close history; fewer than N is a hard stop. Select by median quote volume
  over the 90 completed UTC daily bars, descending, with canonical symbol
  ascending as the tie-break. Estimate each name's sample volatility from its 30 completed
  close-to-close returns through that close and set a long-only target weight
  proportional to inverse annualized volatility, normalized to gross one.
  Volatility only sizes positions; it never ranks names into long/short
  directions. Zero/non-finite volatility or missing required history is a
  hard data stop.
- Band: at each monthly decision close, mark actual incumbent holdings at that
  close and normalize by total portfolio equity, then compare those drifted
  weights with the new target. The first allocation or a membership change
  immediately triggers a full rebalance without evaluating a relative ratio.
  When memberships match, compare common members only; any relative deviation
  `abs(current-target)/target` strictly above the band triggers a full
  rebalance at the next UTC daily open, otherwise no trade occurs. At the next open, turnover/cost uses the then-current marked
  incumbent weights versus the precomputed target. Initial allocation and
  final liquidation are charged.
- Primary: N=30, relative band=20%. Frozen neighborhood only:
  N ∈ {10, 30} × band ∈ {20%, 35%}. Vol window=30 completed days and monthly
  frequency are fixed, not tunable.
- Data: Data Layer V1 curated snapshot
  `a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b`,
  manifest SHA256
  `cd2ae988fac8bca1b4c67d5985d93d3dcc145c7b7c598a9e5a0377c7c49bf166`;
  only `open_time_utc_ms`, `open`, `close`, `quote_volume`, and monthly
  `pit_universe` are used. The data is archive-extended, survivor-biased, and
  exploration-only.
- Split: one continuous path with calendar 2021 train, calendar 2022 OOS,
  calendar 2023 final holdout; positions are not reset or liquidated at split
  boundaries. Only the final 2023 exit is forced. Pre-2021 rows are warmup
  only and nothing from 2024 onward may be read for
  calculation. All rules are frozen before any segment is evaluated.
- Timing and costs: information through close t is executed at open t+1;
  returns are next-open to next-open with the existing terminal close/exit
  rule. Cost is sum absolute weight change × 15 bps baseline (5 bps fee +
  10 bps slippage) or 30 bps stress. Funding is not modeled even though the
  source is USD-M perpetual price data, so 30 bps is only a price-execution
  cost stress and the result cannot be a tradable
  total-return, formal-promotion, or live claim.
- Benchmarks: (1) equal-weight top-N, fully rebalanced monthly under the same
  PIT selection, timing, and costs; (2) the identical inverse-vol target fully
  rebalanced monthly with no band. The first tests risk allocation; the second
  tests whether the band saves turnover without discarding the risk benefit.
- Required gates: every frozen gate in `expl-013-preregistration.json` must
  pass, including OOS and final-holdout benchmark improvement, 30 bps stress,
  ≤75% turnover versus unbanded inverse-vol, 3/4 parameter-neighborhood
  stability, multi-symbol contribution/weight concentration, and four
  half-year multi-period checks. Any failure → `FAIL` and the pure Price Alpha
  line stops without parameter rescue. All pass → `EXPLORATION_PASS` only;
  survivor bias and unmodeled funding still block alpha promotion.
- Expected failure: inverse-vol overweights contracts whose low historical
  volatility does not persist, correlations jump in stress, the band delays a
  necessary risk rebalance, equal weight is already sufficiently diversified,
  turnover reduction is too small, or relative benefit is concentrated in one
  symbol or subperiod.
- Delta vs closed: unlike EXPL-003, volatility sets size and never direction;
  unlike EXPL-010_N2, there is no aggregate target-vol overlay and
  the frozen mechanism is cross-sectional N≥10 risk budgeting plus an explicit
  turnover budget. Moving from two names to N≥10 is acknowledged as a breadth
  change, not claimed as new alpha; cross-project A-share results are context
  only and are not evidence for this contract. It must not be stacked on
  failed momentum signals.

### EXPL-014 · rank momentum with vol scaling · DRAFT
- Hypothesis: EXPL-001 base with per-name vol-scaled position sizes
  (not direction), capped at 2x equal-weight.
- Fixed grid: vol window ∈ {14d, 30d}; cap ∈ {1.5x, 2x}.
- Delta vs closed: vol scaling applied to sizing inside a cross-section, vs
  the closed always-on vol-scaled directional TSMOM.

## Family F — Event behavior

### EXPL-015 · post-jump conditional drift · SCREENED 2026-08-22 · DROPPED_COST_FRAGILE
- Screen result: only a minority of grid points beat cash at baseline and
  none survive 2x costs; the funding-conditioned jump drift is not
  extractable after realistic trading costs. Numbers in
  `expl-screen-results-v2-2026-08-22.json`.
- Comparison spec (frozen before code; deviations may only be labeled
  diagnostic):
  - Universe: BTC/ETH per-asset event study (no breadth claim). Data:
    curated 88d9ff34 1d closes + funding.
  - Jump: |daily return| > k × trailing-30d daily vol, evaluated at close
    t; k ∈ {3, 4} (grid).
  - Condition: funding regime at t — z of the day's total funding rate
    against its own expanding history (min 30d): calm z ≤ 0, crowded
    z > 0.
  - Trade: after a jump at close t — calm: take sign(jump) (continuation);
    crowded: take -sign(jump) (reversal); hold h ∈ {1, 3, 5} bars from
    bar t+1. Overlapping events net; position capped at ±1 per asset.
  - Costs: entry + exit = 2 sides per nonzero position change (|Δposition|
    × per-side cost); funding applied while holding.
  - Stress: 2x per-side trade costs.
  - Benchmark: cash (zero). Beats = net Sharpe > 0, full precision.
    Primary = best baseline net Sharpe among grid points still > 0 under
    stress; none → DROPPED_COST_FRAGILE.
  - Grid (frozen): k {3, 4} × h {1, 3, 5}.
- Original card: after a >4σ daily bar, subsequent 1–5d drift depends on
  funding regime (continuation in calm, reversal in crowded).
- Delta vs closed: event-conditioned two-state behavior; the dead momentum
  family was multi-bar and unconditional.

### EXPL-016 · weekend/session effect gated by vol regime · BLOCKED_ON_DATA

> Blocked 2026-08-22 (plan review): the session effect claims
> cross-sectional breadth; running it on BTC/ETH alone would overclaim.
> Waits on the pre-2024 multi-symbol universe data.

- Hypothesis: the (previously rejected, unconditional, single-symbol)
  session effect exists only in above-median vol regimes and across the
  universe rather than one symbol.
- Fixed grid: session ∈ {weekend, UTC 0–8h}; gate ∈ {median, tercile}.
- Delta vs closed: the closed session study was single-symbol and
  unconditional; conditioning and breadth are the delta.

## Screening order

1. EXPL-001, EXPL-003, and EXPL-004: FAIL under Price Alpha batch v1; all are
   recorded in the Factor Graveyard with no parameter rescue.
2. EXPL-012: KEPT_PRIMARY_SELECTED (primary config in the results JSON).
3. EXPL-010_FULL: NOT_SELECTED; breadth expansion is not a distinct mechanism.
4. EXPL-013: DATA_ERROR_STOP before performance evaluation; do not reinterpret
   it as PASS/FAIL or change the frozen exit semantics under the same ID.
5. EXPL-008: DROPPED_COST_FRAGILE (2026-08-22 screen).
6. EXPL-015: DROPPED_COST_FRAGILE (2026-08-22 screen).
7. EXPL-016: BLOCKED_ON_DATA (claims cross-sectional breadth; must not be
   force-run on two symbols).
8. EXPL-002: BLOCKED_ON_DATA (funding audit failed; price readiness does not
   unlock a funding-conditioned card).
9. EXPL-017: PRE_FORMAL_ACTIVE; implementation attempt EXPL-017-IMPL-014 is
   CORRECTNESS_PASS_CORE_ONLY and EXPL-017-IMPL-015 is
   DATA_UNAVAILABLE_PRE_FORMAL. EXPL-017-FORMAL-001 is PROCESS_DEFECT / NOT_RUN
   and EXPL-017-FORMAL-002 is INVALID_BEFORE_EXECUTION after contract review;
   aggregate formal run count remains zero, with no market verdict or Factor
   Graveyard entry.

## Escalation note

If Families A–F exhaust with no costs-clean survivor, the sanctioned next
step is the data-layer scope decision (one new dataset through V1 flow), per
the protocol's kill criterion — not more cards on the same data.
