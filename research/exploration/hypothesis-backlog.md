# Exploration Hypothesis Backlog

Status: `ACTIVE` · Protocol: `EXPLORATION_PROTOCOL.md`

All cards are exploratory seeds under the exploration tier. Nothing here is
evidence, a PASS, or a promotion claim. Screens run train-only (data ending
2023-12-31) with the conservative cost baseline. Every card states its delta
from formally closed hypotheses; a card that cannot state a delta is invalid.

Drop line default for all cards: train-window net-of-cost Sharpe < 0.5 after
cost/funding stress, or any stress scenario flipping the sign of a thin
positive → `DROPPED`.

## Family A — Cross-section on a real PIT universe

Prior closure context: PIT funding cross-section and 24h momentum were
formally REJECTED on a top-100 PIT pool; the two-symbol "cross-sections"
were not cross-sections at all. Family A retries the layer (not the signal)
with conditioning.

### EXPL-001 · multi-horizon rank momentum · DRAFT
- Hypothesis: dollar-neutral long/short by multi-horizon (1w + 2w + 4w
  averaged) return rank across the top-N (N=30) PIT-by-dollar-volume
  universe, rebalanced weekly, only when cross-sectional rank dispersion is
  above its train median.
- Data: existing PIT universe + klines snapshots (V1 preferred, legacy
  continuation acceptable for screening).
- Fixed grid: N ∈ {20, 30, 50}; rebalance ∈ {w, 2w}. Nothing else.
- Delta vs closed: dispersion gating + multi-horizon averaging + real N≥20
  universe, vs unconditional 24h momentum on top-100.

### EXPL-002 · short-term reversal conditioned on funding pressure · DRAFT
- Hypothesis: 1–3 day reversal within the PIT universe, taken only in names
  whose funding z-score is extreme (crowded positioning unwinds harder).
- Fixed grid: reversal lookback ∈ {1d, 2d, 3d}; funding z threshold ∈ {2, 3}.
- Delta vs closed: funding as a conditioning gate, never as the signal; the
  closed funding cross-section used funding itself as the ranking signal.

### EXPL-003 · low-volatility anomaly cross-section · DRAFT
- Hypothesis: long bottom realized-vol tercile / short top tercile of the PIT
  universe, monthly, vol computed on trailing 30d.
- Fixed grid: vol window ∈ {14d, 30d}; rebalance ∈ {m, 2m}.
- Delta vs closed: equities-style low-vol anomaly was never formally tested
  in GMAQ; single-symbol vol-filtered momentum is a different family.

### EXPL-004 · liquidity tilt inside momentum · DRAFT
- Hypothesis: EXPL-001's momentum legs tilted toward lower dollar-volume
  deciles within the universe (slower crowding), with cost model penalizing
  the tilt honestly.
- Fixed grid: tilt ∈ {none, bottom-3-decays-only}.
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

### EXPL-010 · target-vol portfolio of the universe · SCREENED 2026-08-22 · BLOCKED_ON_DATA
- `EXPL-010_FULL = BLOCKED_ON_DATA`: pre-2024 top-N universe data absent;
  the registered card has not run.
- `EXPL-010_N2_DIAGNOSTIC = DROPPED`: N=2 BTC/ETH diagnostic fails the
  card's Calmar/MDD graduation bar at every grid point. Direction consistent
  with the ASQ A5-1 outcome; directional consistency only, no
  cross-market claim is made.
- Original card: top-N universe portfolio at constant target vol with
  rebalance bands vs unscaled buy-and-hold; graduation bar Calmar/MDD.
  Grid target ∈ {15%, 20%, 30%}; band ∈ {±10%, ±20%}.

## DATA BLOCKERS (2026-08-22 inventory)

- PIT universe files (`global-quant-continuation/user_data/data/pit/`, 82
  symbols, 15m klines + funding) cover **2026-02..2026-08 only** — entirely
  inside the tainted region. Family A cards (EXPL-001..004), EXPL-013, and
  the full-breadth EXPL-010 cannot be screened on the train window.
- Clean pre-2024 data available: curated `88d9ff34` (BTC/ETH 1d + funding +
  mark-8h, 2020-01..2026-08) and curated `9601a8ff` (BTC/ETH spot/perp 8h).
- Implication: screening proceeds on BTC/ETH cards until the data-layer
  scope decision (protocol escalation path) delivers a pre-2024 multi-symbol
  dataset through the raw → validated → curated V1 flow.

## Family E — Portfolio construction

### EXPL-013 · banded inverse-vol universe portfolio · BLOCKED_ON_DATA
- Hypothesis: inverse-vol weights across the top-N universe with wide
  rebalance bands (trade only when weight drift > band) retains the
  diversification benefit at a fraction of the turnover.
- Fixed grid: N ∈ {10, 30}; band ∈ {±20%, ±35% relative}.
- Delta vs closed: the dead two-asset inverse-vol test (ASQ A5-1) and the
  9-symbol-week carry both suffered tiny breadth; this is N≥10 with an
  explicit turnover budget.

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

1. EXPL-010: BLOCKED_ON_DATA (full card never ran; N2 diagnostic DROPPED
   on unified semantics).
2. EXPL-012: KEPT_PRIMARY_SELECTED (primary config in the results JSON).
3. EXPL-013: BLOCKED_ON_DATA (needs top-N universe).
4. EXPL-008: DROPPED_COST_FRAGILE (2026-08-22 screen).
5. EXPL-015: DROPPED_COST_FRAGILE (2026-08-22 screen).
6. EXPL-016: BLOCKED_ON_DATA (claims cross-sectional breadth; must not be
   force-run on two symbols).
7. Family A cards wait on the pre-2024 universe data decision (see DATA
   BLOCKERS).

## Escalation note

If Families A–F exhaust with no costs-clean survivor, the sanctioned next
step is the data-layer scope decision (one new dataset through V1 flow), per
the protocol's kill criterion — not more cards on the same data.
