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

- Instruments: Binance USD-M perpetuals from the CURRENT `exchangeInfo`,
  frozen filter `contractType=PERPETUAL AND status=TRADING AND
  quoteAsset=USDT` (official docs: exchangeInfo describes CURRENT trading
  rules and symbols — it is not a historical PIT universe).
- Dataset id: `pre2024-usdm-current-survivors-1d` — the survivorship
  limitation is encoded in the name.
- Bars: 1d klines ONLY (breadth over granularity). Funding: the COMPLETE
  funding event stream as returned by the API — every `fundingTime` and
  `fundingRate`, sorted and deduplicated; no fixed 8h interval is assumed
  (`fundingIntervalHours` officially varies). Mark price: NOT fetched.
- Window: exchange launch (2019-09) .. 2023-12-31 inclusive. No 2024+
  bar data is fetched. This does NOT make the dataset PIT-clean by itself:
  the candidate list comes from today's `exchangeInfo`, so 2026 listing
  status participates in pool construction.
- Honest labeling: the candidate set cannot cover contracts delisted
  before today (public endpoints expose no delisting archive). The
  dataset is therefore a **survivor-biased exploration set** and every
  consuming artifact must carry that label; it must not be described as
  a structurally taint-free or complete historical domain. The prior PIT
  studies carried the same limitation.
- Endpoints: public, no credentials, read-only.

## PIT universe rule (frozen)

1. **Listing eligibility**: symbol's first available 1d bar must be
   ≤ 2023-06-30 (at least 6 months of history inside the train window).
   First-bar date is the listing proxy — computed from the fetched data
   itself, never from today's rankings.
2. **PIT liquidity floor**: at each calendar month-end m in the window, a
   symbol is IN the pool for the following month iff the median of its
   `quote asset volume` (the klines quote-volume field, never base
   volume) over the 90 COMPLETED UTC daily bars ending at m is ≥ $5M.
   The floor is an exploratory screening heuristic only: daily volume
   does not evidence order-book execution cost, and no slippage claim
   may cite it. Computed inside the pipeline from history, reproducibly.
3. **Known limitation, recorded not hidden**: symbols delisted before
   today cannot be recovered from current endpoints (survivorship bias in
   the pool). Every artifact consuming this dataset must carry that caveat.
   The prior PIT studies had the same limitation, honestly disclosed.

## Graduation boundary (hard)

This survivor-biased dataset screens exploration hypotheses ONLY. It
can never produce a formal Family A PASS, and nothing screened on it
promotes toward live trading. A survivor must either (a) re-confirm on
an unbiased historical instrument domain once one exists, or (b) pass a
true prospective (forward) validation window.

## V1 pipeline integration

- New curated dataset `pre2024-usdm-current-survivors-1d` through the
  existing `gmaq_data` layer: raw jsonl per symbol (COMPLETE source
  responses preserved in the raw layer; the field whitelist below
  constrains the curated layer only) → validation → curated snapshot +
  manifest + schema + file SHAs; registry replay must verify
  VERIFIED/PASS like existing datasets.
- Cross-validation against existing VERIFIED data: BTC/ETH daily bars
  in the overlapping window compared over the FULL window, per bar, on
  UTC timestamps and OHLC as ORIGINAL decimal strings (no float parsing
  anywhere in the comparison). The reference dataset `88d9ff34` carries
  no quote volume, so that field is validated per bar only by the range
  invariant `volume x low <= quote volume <= volume x high`.

## Acceptance criteria (exact)

- Symbol count and per-year pool sizes recorded; expected order of
  magnitude: tens of symbols eligible by 2022, NOT hundreds.
- PIT pool membership is a pure function of the raw data: re-running the
  filter must reproduce identical pools (pipeline asserts set equality).
- BTC/ETH full-window per-bar comparison against curated `88d9ff34`
  passes with zero mismatches; any mismatch fails the snapshot.
- Registry replay must be `VERIFIED` with `quarantine = 0`. Missing,
  duplicate, or out-of-window rows go to quarantine — zero-filling and
  silent gap interpolation are forbidden. If quarantine is nonzero the
  snapshot FAILS: investigate and re-curate; relaxing the check to force
  a pass is forbidden.
- Curated field scope per symbol file: 1d bars (open_time, OHLC, quote
  volume) and funding events (fundingTime, fundingRate); anything else
  fails validation.

## Archive index probe (2026-08-22, metadata only, user-authorized)

- Method: S3-style directory listing of
  `s3.ap-northeast-1.amazonaws.com/data.binance.vision` under
  `data/futures/um/daily/klines/` (2 listing rounds), diffed against
  current `fapi.binance.com/fapi/v1/exchangeInfo` with the frozen filter.
  No data files downloaded.
- Findings: the archive retains **1007** symbol folders; all **527**
  current TRADING PERPETUAL USDT symbols are present; **480** are
  archive-only (delisted USDT perps such as `1000BTTCUSDT`/`AERGOUSDT`,
  plus USDC/BUSD-quoted pairs, stock perps, and `*SETTLED` variants the
  filter excludes anyway).
- Consequence for this rule: the candidate set should be
  **current 527 ∪ archive-only USDT-like symbols** whose archive shows
  monthly 1d klines starting ≤ 2023-06-30. Archive-only symbols lack a
  current `contractType`; treat symbol naming (USDT quote suffix, no
  `SETTLED`) as the working filter and flag the residual uncertainty.
  The survivor-bias label is thereby REDUCED but not eliminated
  (pre-launch delistings with no archive folder, and the naming
  heuristic, remain).
- The dataset id stays `pre2024-usdm-current-survivors-1d` until the
  archive-extended candidate set is actually validated; if adopted, a
  rename to `pre2024-usdm-archive-extended-1d` accompanies the revision
  of this rule (naming must not outrun validation).
- Per the user's decision (2026-08-22): with this probe recorded, the
  bulk public fetch is GO without a further CLI review round.

## Cost and authorization

- One-time fetch: public API, rate-limited, estimated 1–3 hours wall
  clock; size roughly a few hundred MB raw for 1d + funding across all
  eligible symbols.
- Execution requires the user's explicit go (network access), given
  AFTER this rule is approved.
