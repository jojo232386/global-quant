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

Prior closure context: unconditional vol-scaled LS-TSMOM REJECTED (OOS
-11%, worst short adverse excursion 92%). Family C uses vol as a gate or
target, not as a signal direction.

### EXPL-008 · vol-regime gated trend · DRAFT
- Hypothesis: LS-TSMOM exposure only in below-median realized-vol regimes
  (trends persist in calm; churn in stress).
- Fixed grid: vol window ∈ {14d, 30d}; gate ∈ {median, tercile}.
- Delta vs closed: gate vs always-on; expected exposure roughly halved.

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

### EXPL-012 · BTC/ETH relative strength rotation · SCREENED 2026-08-22 · KEPT_THIN
- Screen result (train 2020-01-01..2023-12-31, dataset 88d9ff34, spot
  long-only, 15bps/side, full numbers in `expl-screen-results-2026-08-22.json`):
  baseline net Sharpe 1.131–1.229 vs daily-rebalanced 50/50 benchmark 1.115
  (all 4 grid points beat it; Calmar 1.044–1.236 vs 0.951). Under 2x cost
  stress only the 14d/3% point still beats the benchmark (Sharpe 1.170,
  Calmar 1.089); the other three lose their excess. Verdict: weak survivor —
  thin, cost-fragile edge concentrated at fast lookback + tight band. If
  graduated, the formal preregistration must re-test the FULL grid (no
  cherry-picking the stress survivor); expectations low.
- Original card: rotate between BTC and ETH by relative strength with
  hysteresis; grid lookback ∈ {14d, 30d}, hysteresis band ∈ {±3%, ±5%};
  benchmark static 50/50.

### EXPL-010 · target-vol portfolio of the universe · SCREENED 2026-08-22 (reduced breadth) · DROPPED (preliminary)
- Screen result (N=2 BTC/ETH only — the card specifies top-N; full breadth
  BLOCKED_ON_DATA, see below): target-vol scaling cuts MDD from -76% to
  -27%..-49% but Calmar lands 0.68–0.86 vs 0.95 for unscaled 50/50 on every
  grid point; Sharpe improvement is marginal (≤ +0.09). Risk-adjusted terms
  worsen. Mirrors the ASQ A5-1 lesson: vol scaling alone does not improve
  tail-risk-adjusted returns. Dropped at preliminary breadth; re-carding at
  top-N requires pre-2024 universe data.
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

### EXPL-013 · banded inverse-vol universe portfolio · DRAFT
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

### EXPL-015 · post-jump conditional drift · DRAFT
- Hypothesis: after a >4σ daily bar, subsequent 1–5d drift depends on
  funding regime (continuation in calm, reversal in crowded).
- Fixed grid: σ threshold ∈ {3, 4}; horizon ∈ {1d, 3d, 5d}.
- Delta vs closed: event-conditioned, two-state; unconditional single-bar
  studies were never run (the dead momentum family was multi-bar).

### EXPL-016 · weekend/session effect gated by vol regime · DRAFT
- Hypothesis: the (previously rejected, unconditional, single-symbol)
  session effect exists only in above-median vol regimes and across the
  universe rather than one symbol.
- Fixed grid: session ∈ {weekend, UTC 0–8h}; gate ∈ {median, tercile}.
- Delta vs closed: the closed session study was single-symbol and
  unconditional; conditioning and breadth are the delta.

## Screening order

1. ~~EXPL-010 and EXPL-013 first~~ — EXPL-010 screened (dropped at N=2,
   full breadth blocked on data); EXPL-013 blocked on data (needs top-N).
2. EXPL-012 screened 2026-08-22: KEPT_THIN.
3. Next screenable on clean BTC/ETH data: EXPL-008 (vol-regime gated
   trend), EXPL-015 (post-jump conditional drift), EXPL-016 (session ×
   vol regime, 1d granularity only).
4. Family A, EXPL-013, full EXPL-010 wait on the pre-2024 universe data
   decision (see DATA BLOCKERS).

## Escalation note

If Families A–F exhaust with no costs-clean survivor, the sanctioned next
step is the data-layer scope decision (one new dataset through V1 flow), per
the protocol's kill criterion — not more cards on the same data.
