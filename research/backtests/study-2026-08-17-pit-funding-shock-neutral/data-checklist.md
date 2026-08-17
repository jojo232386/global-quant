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

The data supports falsification research but cannot support strategy promotion.
