# Exploration Tier Protocol

Status: `ACTIVE_PROTOCOL` (adopted 2026-08-22)

## Purpose

The formal research loop (`research/README.md`) is deliberately expensive:
preregistration, V1 dataset binding, one-shot runs, no parameter rescue.
That discipline is correct and stays unchanged. What has been missing is a
cheap layer in front of it. This protocol defines that layer: train-only cheap
screens plus explicitly preregistered pre-2024 price experiments when the user
requires OOS/final-holdout evidence, without contaminating formal evidence.

Two rules summarize the whole document:

1. Exploration output is never evidence.
2. Nothing graduates without a fresh formal preregistration.

## Hard boundaries

- **Pre-2024 containment.** Cheap card screens are train-only. A stricter
  price experiment may instead freeze one primary, train, OOS, and final
  holdout before code, as Price Alpha v1 already did; every split must end by
  `2023-12-31`, the primary cannot be chosen from the grid, and the final
  holdout cannot rescue failed train/OOS gates. The 2024–2026 region is
  already observed by formal V1 studies and is tainted for exploration. Any
  selection or tuning that looked at post-2023 data invalidates the card.
- **Artifact containment.** All exploration artifacts live under
  `research/exploration/` and use the `expl-` prefix. They are never written
  to `research/backtests/`, never named `results.json`, and never read by
  admission, Control Room, or any formal runner.
- **No promotion claims.** Cheap screens use only `kept/dropped`. A frozen
  price experiment with preregistered OOS/holdout gates may record
  `EXPLORATION_PASS/FAIL`, but neither label is evidence of alpha promotion,
  a tradable edge, or live readiness. Numeric performance stays in the
  contained result JSON and is not quoted in backlog, commits, the vault, or
  human summaries.
- **Fixed small grids.** Each card registers its parameter grid at creation.
  Expanding a grid after seeing screen results is parameter rescue and is
  forbidden; it requires a new card with a stated reason.
- **Same cost lens.** Screening decisions use the conservative baseline in
  `costs/COST_MODEL_BASELINE.md`. A survivor that only survives without costs
  is dead on arrival.
- **Data.** Exploration may read locally available snapshots (V1 curated
  preferred; legacy continuation PIT data is acceptable for screening only).
  The card notes must record the snapshot identity and window used. No new
  fetching pipelines are built for exploration; data-breadth decisions follow
  the escalation path below, not ad-hoc acquisition.

## Card lifecycle

1. `DRAFT` — hypothesis card written in `hypothesis-backlog.md` with family,
   hypothesis, data plan, fixed grid, and how it differs from already-dead
   hypotheses. **A DRAFT must freeze its comparison spec before any code is
   written**: scope (full card vs diagnostic), universe/N, base portfolio
   construction, benchmark construction (exact, e.g. "fixed-share
   buy-and-hold rebased at window start", not "static"), cost application
   points, and the permitted claim wording. A screen that deviates from
   its frozen spec may only be labeled a diagnostic of that card, never
   the card's registered screen.
2. `SCREENED` — run once on the train window with registered grid. Record
   kept/dropped plus one line of reason. Dropped cards stay in the backlog as
   negative memory; they are evidence of coverage, not of performance.
3. `GRADUATED` — at most 2–3 survivors per cycle proceed to a fresh formal
   preregistration (`research/preregistration/HYPOTHESIS_TEMPLATE.md`), a V1
   `VERIFIED` curated dataset binding, and a one-shot formal run under the
   existing gate. Graduation does not weaken any formal rule.

A frozen price experiment is the stricter alternative used when the user
requires OOS and a final holdout before deciding whether the price-only line
continues. Its immutable preregistration is committed before code, it runs
once, and `EXPLORATION_PASS` is still only a signal to acquire independent
confirmation data. `FAIL` is permanent for that id and cannot be parameter-
rescued.

## Graduation selection rule (frozen 2026-08-22, per Codex-App review)

- A graduated card designates **one primary configuration**, selected by a
  deterministic rule written in the screen code before the formal run:
  the primary is the grid point with the best baseline net Sharpe among
  points that still beat the primary benchmark under 2x cost stress.
- The formal preregistration confirms **the primary configuration only**.
  The remaining grid points may be reported as sensitivity diagnostics;
  they can never rescue or replace a failed primary result. Re-opening
  grid competition on formal/OOS data is winner selection and is
  forbidden.

## Record containment (reinforced)

- Backlog entries, commit messages, the vault, and agent-to-agent
  summaries record only status (`DRAFT / FROZEN BEFORE RUN / SCREENED / KEPT /
  DROPPED / EXPLORATION_PASS / FAIL / BLOCKED_ON_DATA`), the primary config,
  and non-numeric reasons.
- All performance figures live exclusively in the exploration results
  JSON under `research/exploration/`.

## Project-level kill criterion

Formal research stops when: 6 consecutive formal REJECTs occur across
genuinely distinct families whose exploration screens were clean, with no
costs-free survivor anywhere. At that point the recorded conclusion is "no
accessible edge under the current data and cost regime", formal runs pause,
and the only sanctioned next move is an explicit data-layer scope decision
(one new dataset through the existing raw → validated → curated V1 flow),
not more screening of the same data.

## Current backlog

See `hypothesis-backlog.md`. Families already closed by formal REJECTs
(single-symbol timing rules on ETH/BTC; unconditional carry; unconditional
vol-scaled LS-TSMOM; unconditional PIT funding cross-section and 24h
momentum; funding-shock neutral reversal) are off-limits as-is. Cards must
state their delta from those closures.
