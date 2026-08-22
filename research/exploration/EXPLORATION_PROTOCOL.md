# Exploration Tier Protocol

Status: `ACTIVE_PROTOCOL` (adopted 2026-08-22)

## Purpose

The formal research loop (`research/README.md`) is deliberately expensive:
preregistration, V1 dataset binding, one-shot runs, no parameter rescue.
That discipline is correct and stays unchanged. What has been missing is a
cheap layer in front of it. This protocol defines that layer: a train-only
screening space where many hypotheses can die quickly, without contaminating
formal evidence.

Two rules summarize the whole document:

1. Exploration output is never evidence.
2. Nothing graduates without a fresh formal preregistration.

## Hard boundaries

- **Train-only window.** Exploration screens evaluate on data ending
  `2023-12-31`. The 2024–2026 region is already observed by three formal V1
  studies and is treated as tainted for selection. Any selection decision
  (keep/drop/tune a card) that looked at post-2023 data invalidates the card.
- **Artifact containment.** All exploration artifacts live under
  `research/exploration/` and use the `expl-` prefix. They are never written
  to `research/backtests/`, never named `results.json`, and never read by
  admission, Control Room, or any formal runner.
- **No claims.** Exploratory numbers may not be quoted as PASS, edge, or
  performance anywhere — including commit messages, the vault, and
  conversations with humans. The only permitted phrasing is "screened,
  kept/dropped".
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
   hypotheses.
2. `SCREENED` — run once on the train window with registered grid. Record
   kept/dropped plus one line of reason. Dropped cards stay in the backlog as
   negative memory; they are evidence of coverage, not of performance.
3. `GRADUATED` — at most 2–3 survivors per cycle proceed to a fresh formal
   preregistration (`research/preregistration/HYPOTHESIS_TEMPLATE.md`), a V1
   `VERIFIED` curated dataset binding, and a one-shot formal run under the
   existing gate. Graduation does not weaken any formal rule.

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
