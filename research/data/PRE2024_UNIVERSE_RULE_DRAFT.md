# Pre-2024 Multi-Symbol Universe Rule

Status: `APPROVED_FOR_EXPLORATION_DATA` — public archive acquisition was
authorized and `acquisition-attempt-001` completed on 2026-08-22. The price
V1 remains gated on the repair and acceptance rules below; funding remains
blocked by its separate audit verdict.

## Purpose

Unlock the data-blocked exploration cards (Family A cross-sections,
EXPL-013, full EXPL-010, EXPL-016) with clean pre-2024 train-window data,
through the existing raw → validated → curated V1 flow. No V2, no second
data source, no new governance.

## Scope of the fetch

- Instruments: the frozen 209-symbol candidate set: 138 symbols included from
  the CURRENT `exchangeInfo` filter (`PERPETUAL`, `TRADING`, `USDT`) plus 71
  archive-only USDT-like symbols with a first monthly 1d file no later than
  2023-06. This is broader than today's survivors but is not a complete
  historical contract master.
- Acquisition id: `pre2024-usdm-current-survivors`; curated price dataset id:
  `pre2024-usdm-archive-extended-1d`. Every snapshot and consumer retains an
  explicit survivor-bias label.
- Curated bars: 1d klines ONLY (breadth over granularity). The monthly 1d
  archive remains immutable. If it has an intra-month missing day, the exact
  checksum-verified official daily 1d object may fill that day. If a 1d row's
  archived base volume makes its quote-volume range invariant fail, the
  checksum-verified official daily 1m object may be fetched solely as
  repair evidence: its 1440 UTC minutes must aggregate exactly to the 1d OHLC;
  their summed base and quote volume replace only the disputed 1d volume
  fields. No individual 1m field enters curated data or a strategy.
  Funding: the COMPLETE
  funding event stream as returned by the API — every `fundingTime` and
  `fundingRate`, sorted and deduplicated; no fixed 8h interval is assumed
  (`fundingIntervalHours` officially varies). Mark price: NOT fetched.
- Window: exchange launch (2019-09) .. 2023-12-31 inclusive. No 2024+
  bar data is fetched. This does NOT make the dataset PIT-clean by itself:
  the candidate list comes from today's `exchangeInfo`, so 2026 listing
  status participates in pool construction.
- Honest labeling: the archive recovers some contracts delisted before today,
  but symbol-name inference and archive availability cannot prove a complete
  historical domain. The dataset is therefore a **survivor-biased exploration
  set** and every
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

- New curated dataset `pre2024-usdm-archive-extended-1d` through the existing
  `gmaq_data` layer. Complete source ZIPs remain preserved in the external raw
  acquisition directory. The raw V1 snapshot binds the canonical acquisition
  manifest, audit report, frozen candidate set, repair manifest, and every
  consumed source SHA; validated and curated stages contain only whitelisted
  JSONL. Registry replay must verify VERIFIED/PASS like existing datasets.
- Cross-validation against existing VERIFIED data: BTC/ETH daily bars
  in the overlapping window compared over the FULL window, per bar, on
  UTC timestamps and OHLC as ORIGINAL decimal strings (no float parsing
  anywhere in the comparison). The reference dataset `88d9ff34` carries
  no quote volume, so that field is validated per bar only by the range
  invariant `volume x low <= quote volume <= volume x high`.

## Acceptance criteria (exact)

- Symbol count and per-year pool sizes recorded; expected order of
  magnitude: tens of symbols eligible by 2022, NOT hundreds.
  Empirical resolution (2026-08-23): the frozen $5M rule produces more than
  100 eligible symbols before the end of 2022. Source hashes, repaired volume
  evidence, continuous 90-calendar-day eligibility, and PIT timing were
  rechecked; the earlier magnitude expectation was wrong. The threshold is
  not changed after observing this result.
- PIT pool membership is a pure function of the raw data: re-running the
  filter must reproduce identical pools (pipeline asserts set equality).
- BTC/ETH full-window per-bar comparison against curated `88d9ff34`
  passes with zero mismatches; any mismatch fails the snapshot.
- Registry replay must be `VERIFIED` with `quarantine = 0` after applying only
  the bounded same-source repair evidence above. Missing,
  duplicate, or out-of-window rows go to quarantine — zero-filling and
  silent gap interpolation are forbidden. If quarantine is nonzero the
  snapshot FAILS: investigate and re-curate; relaxing the check to force
  a pass is forbidden.
- Pre-alpha domain exclusion: `ICPUSDT` is removed from the eligible price
  domain as `EXCLUDED_INCOMPLETE_TRADE_HISTORY`. Official checksummed 1h trade
  archives contain no bars for 2022-09-22..26 while the checksummed mark-price
  archive continues. The whole symbol is excluded; those dates are not
  zero-filled, interpolated, or replaced with mark prices. The original
  209-symbol candidate set and the exclusion evidence remain hash-bound; the
  eligible curated domain is 208 symbols. This rule was frozen before any
  Family A alpha screen used the dataset.
- Price-only curated field scope per symbol file: 1d bars (open_time, OHLC,
  quote volume) plus the monthly PIT universe; anything else fails validation.
  Funding events remain outside this snapshot until `funding_verdict=PASS`.

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
- The archive-extended candidate set was frozen and the price-file acquisition
  audit passed for 209 candidates (138 current, 71 archive-only); the curated
  price dataset therefore uses `pre2024-usdm-archive-extended-1d`. This rename
  does not remove the survivor-bias or exploration-only graduation boundary.
- Per the user's decision (2026-08-22): with this probe recorded, the
  bulk public fetch is GO without a further CLI review round.

## Cost and authorization

- The bulk public fetch was user-authorized and completed. The bounded repair
  pass uses only the same official public archive, no credentials, and never
  mutates the acquired monthly files.
