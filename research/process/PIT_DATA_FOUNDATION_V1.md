# GMAQ PIT Data Foundation V1

Status: `PARTIAL_PIT_UNIVERSE_UNLOCKED`

Canonical baseline:
`683ff6f8de0dd7495c76be6cb19c200843173826` (GitHub merge of PR #33).

This follow-up preserves PR #33's full-market and Funding/OI vintage blockers.
It closes a narrower question that PR #33 did not test: whether a fixed,
historically captured cohort can remove current-survivor selection from a
future confirmation universe without pretending to reconstruct every Binance
listing.

## Result

```text
RESULT=PARTIAL_PIT_UNIVERSE_UNLOCKED
PIT_INSTRUMENT_MASTER=PARTIAL_PIT_COHORT_CANDIDATE
PIT_CLEAN_SYMBOL_COUNT=0_FORMALLY_ADMITTED
HISTORICALLY_CAPTURED_COHORT_SYMBOL_COUNT=80
TOTAL_SYMBOLS_EVALUATED=81
DATA_FOUNDATION_UNLOCKED=YES_FIXED_COHORT_ONLY
TIER2_DATA_FOUNDATION_READY=NO
PAID_DATA_REQUIRED=NO_FOR_FIXED_COHORT; UNRESOLVED_FOR_NUMERIC_VINTAGE
READY_FOR_TINY_LIVE=FALSE
REAL_ORDER_COUNT=0
```

The cohort candidate is the complete set of 80 `TRADING / PERPETUAL / USDT` instruments
in one archived official Binance USD-M `exchangeInfo` response. The response's
official `serverTime` differs from the Wayback capture by about one second.
All 80 bind to Price V1. Seventy-four retain positive quote-volume activity
through 2023-12-31; `BZRXUSDT` and `YFIIUSDT` retain their existing confirmed
terminal events, while `CVCUSDT`, `HNTUSDT`, and `SRMUSDT` now bind additional
official terminal evidence. `TOMOUSDT` has an unresolved zero-volume tail after
2023-11-14. Because that positive daily aggregate is identified by its bar-open
time and cannot prove continuous intraday status, the deterministic query window
stops at `2023-11-14T00:00:00Z` rather than overstating the supported interval.

Historical queries return 80 symbols in mid-2021, 78 in mid-2022, and 75 in
mid-2023. The five later-delisted contracts remain present before their
effective terminal times. Queries at or after 2023-11-14 fail closed. Symbols
listed after the frozen capture are never admitted. This controls
current-survivor bias for the covered cohort window; it does not claim complete
rolling-market coverage or add later listings.

## Evidence semantics

- Listing/start: Tier B — Wayback capture of the official historical
  `exchangeInfo` response. Its REST `serverTime` is a response/status timestamp,
  not a publication timestamp. It is a conservative confirmed-active start,
  not the true listing time. Binance's reported `onboardDate` is retained but
  explicitly untrusted and is never used for admission.
- Delisting: Tier A — official Binance CMS announcement plus official
  aggregate-trade archive evidence for five cohort members. The two existing
  Lifecycle V1 events are reused; three cohort-only events are hash-bound in a
  supplemental artifact without rewriting the canonical sidecar.
- Activity: positive-quote-volume exchange-event timestamps in the verified
  Price V1 files. First/last or zero-volume bars are never promoted to
  listing/delisting facts. The unresolved `TOMOUSDT` tail limits global coverage.
- AKRO: `QUARANTINED`. It is not a cohort member; the announced 2022-05-26
  settlement conflicts with official aggregate trades through 2022-05-27.
- Vintage: instrument-status capture is historically bound. Current numeric
  Price/Funding/OI archive bytes still lack complete historical publication and
  revision lineage, so all numeric vintage remains `VINTAGE_UNVERIFIED`.

## Linkage and non-goals

- Price V1 is linked by exact snapshot, manifest, PIT, per-role file hashes and
  remains `EXPLORATION_ONLY` for its numeric results.
- Lifecycle V1 is linked by exact sidecar hash and remains unchanged; the
  three-event supplemental terminal artifact is independently hash-bound.
- Funding/OI is identity-compatible by Binance USD-M symbol but remains blocked
  by historical first-availability and revision/vintage evidence.
- No Alpha, EXPL, backtest, Research Tier, Protocol, runtime, database, data
  lake, credential, order, or live-trading path is created or changed.

The 80 records are historically captured cohort candidates; none is formally
Tier 2 admitted by this artifact alone. The implementation reuses the existing Data Layer V1 verification and
Lifecycle V1 evidence, plus the official `binance-public-data` checksum/revision
model. A standard-library thin builder/query is lower complexity than importing
CCXT/Freqtrade/Nautilus or a second data platform.
