# Pre-2024 Multi-Symbol Universe Rule (DRAFT for review)

Status: `DRAFT` — awaiting user + Codex-App review. Fetching starts only
after this rule is approved and the user separately authorizes network
access to Binance public endpoints. This document freezes the selection
rule BEFORE any data is pulled (the selection-bias lesson: the rule must
not be informed by the data it selects on).

## Purpose

Unlock the data-blocked exploration cards (Family A cross-sections,
EXPL-013, full EXPL-010, EXPL-016) with clean pre-2024 train-window data,
through the existing raw → validated → curated V1 flow. No V2, no second
data source, no new governance.

## Scope of the fetch

- Instruments: Binance USD-M perpetuals, all symbols listed TODAY
  (public exchangeInfo), filtered by the PIT rule below.
- Bars: 1d klines ONLY (breadth over granularity; cross-sectional cards
  are daily/weekly). Funding: 8h rates (needed by Family B conditioning).
  Mark price: NOT fetched (no queued card needs it).
- Window: exchange launch (2019-09) .. 2023-12-31 inclusive. Nothing from
  the tainted 2024+ region is fetched, structurally preventing its use in
  selection.
- Endpoints: public, no credentials, read-only.

## PIT universe rule (frozen)

1. **Listing eligibility**: symbol's first available 1d bar must be
   ≤ 2023-06-30 (at least 6 months of history inside the train window).
   First-bar date is the listing proxy — computed from the fetched data
   itself, never from today's rankings.
2. **PIT liquidity floor**: at each calendar month-end m in the window, a
   symbol is IN the pool for the following month iff its median daily
   dollar volume over the trailing 90d (data up to m only) is ≥ $5M.
   Rationale: the cost baseline's 10bps slippage assumption is not
   defensible for thinner books; $5M median keeps breadth while excluding
   micro-caps. The floor is computed inside the pipeline from history,
   reproducibly.
3. **Known limitation, recorded not hidden**: symbols delisted before
   today cannot be recovered from public endpoints (survivorship bias in
   the pool). Every artifact consuming this dataset must carry that caveat.
   The prior PIT studies had the same limitation, honestly disclosed.

## V1 pipeline integration

- New curated dataset `pre2024-usdm-universe-1d` through the existing
  `gmaq_data` layer: raw jsonl per symbol → validation (gap/monotonic
  timestamp checks per symbol) → curated snapshot + manifest + schema +
  file SHAs; registry replay must verify VERIFIED/PASS like existing
  datasets.
- Cross-validation against existing VERIFIED data: BTC/ETH daily bars in
  the overlapping window must match curated `88d9ff34` values exactly
  (spot-check in validation).

## Acceptance criteria

- Symbol count and per-year pool sizes recorded; expected order of
  magnitude: tens of symbols eligible by 2022 (exchange-listed perps with
  $5M+ median volume), NOT hundreds.
- PIT pool membership is a pure function of the raw data (re-running the
  filter reproduces identical pools).
- BTC/ETH overlap spot-check passes against `88d9ff34`.
- Registry replay VERIFIED; quarantine zero.

## Cost and authorization

- One-time fetch: public API, rate-limited, estimated 1–3 hours wall
  clock; size roughly a few hundred MB raw for 1d + funding across all
  eligible symbols.
- Execution requires the user's explicit go (network access), given
  AFTER this rule is approved.
