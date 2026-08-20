# Data checklist: study-2026-08-17-pit-funding-shock-neutral

- source contract: Binance public USD-M 15m klines, 1d volume bars, and funding
  history; no API credentials.
- expected coverage: 2026-02-01 through 2026-08-16 UTC.
- time availability: D-2 volume defines D's universe; funding cutoff is D-1
  23:45; execution uses D 00:00 open.
- no fill policy: missing bars or fewer than four visible funding records skip
  the symbol; an under-populated day is skipped.
- supplied data limitation: PIT reconstruction starts from today's top-100 and
  is not a complete historical listing/delisting master.
- funding coverage limitation: the existing PIT manifest does not record
  complete request-window evidence.
- execution limitation: historical order-book spread, depth, impact and partial
  fill evidence is unavailable; configured slippage is a research assumption.
- account limitation: fee tier and maintenance margin are placeholders until a
  separately authorized, read-only, version-bound verification.

## Run-time evidence

- PIT manifest file SHA-256:
  `2dc62f0897e4c43bcc62ebc80b8285370fa1c7c919956a53325fed155c581c8e`
- dataset SHA-256 recorded inside that manifest:
  `4e2a8a7a3eebfa0011657bce0fe3e77b138db1369126a990b135bde31f8ee48f`
- PIT universes SHA-256:
  `47bdcd6e646ce3c92a6ca870378b4266c88abe42bac600c11af04aa0fb59f785`
- 100 candidates, 77 daily-universe symbols, 61 scoreable OOS rebalance days.
- the supplied manifest has no `funding_coverage` request-window section;
  `funding_request_window_complete=false` is retained in `results.json`.

The data supports falsification research but cannot support strategy promotion.
