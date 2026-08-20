# Acquisition amendment before formal results

Date: 2026-08-20 UTC. Result status at amendment: `NOT_RUN`.

The first public-data acquisition attempt stopped before producing a data
manifest or any strategy result because early `/fapi/v1/fundingRate` records
contain an empty `markPrice`. The funding timestamp and rate were present, but
the preregistered requirement to value funding from published data could not be
met from that response field alone.

Frozen deterministic resolution before retry:

- keep the original funding timestamp and funding rate unchanged;
- fetch complete Binance USD-M `/fapi/v1/markPriceKlines` at 8-hour frequency
  for the same exact `[2020-01-01, 2026-08-20)` window;
- when the funding response has a positive `markPrice`, keep it and label the
  source `fundingRate_response`;
- only when it is empty, map the event timestamp down to its UTC 8-hour bucket
  and use that mark-kline open, labeled
  `fapi_8h_mark_kline_open_fallback`;
- reject any missing 8-hour bucket, non-positive price, duplicate/gapped mark
  series, or incomplete funding pagination;
- pin the 8-hour mark-price files and fallback counts in the data manifest.

No signal, parameter, split, cost, stress, or PASS threshold changed. This is a
source-completeness amendment made before viewing any return or verdict.

## End-of-window valuation boundary

The first formal runner invocation then stopped before writing results because
the last complete 2026-08-19 open-to-next-open return requires the fixed
2026-08-20 00:00 open. The acquisition file correctly excluded the incomplete
2026-08-20 OHLC row, so that valuation boundary was unavailable.

The retry records only the positive 2026-08-20 00:00 `open` as
`end_boundary_open` in the manifest. The incomplete high, low, close, and
volume remain excluded; the boundary cannot enter any signal or parameter and
is used only to mark and liquidate positions at the predeclared exclusive end.
Missing or duplicated boundary values fail acquisition. No return or verdict
was written before this clarification.
