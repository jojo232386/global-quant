# GMAQ PIT Instrument Master V1

Status: `PARTIAL_PIT_COHORT_CANDIDATE`

This is a fixed historical cohort, not a complete dynamic Binance universe.
It contains every one of the 80 `TRADING`, USDT-quoted `PERPETUAL` instruments
in a Wayback capture of the official Binance USD-M `exchangeInfo` response at
`2021-01-04T19:51:01Z`. The official response's `serverTime` is
`2021-01-04T19:51:02.039Z`.

The cohort is independent of today's symbol list, later survival, Price V1
returns, liquidity, or an Alpha result. Price V1 supplies hash-bound daily
rows, but a row with zero quote volume is not promoted to trading activity.
`BZRXUSDT` and `YFIIUSDT` reuse the existing official terminal evidence;
`CVCUSDT`, `HNTUSDT`, and `SRMUSDT` bind the same class of official announcement
plus aggregate-trade evidence in a cohort-only supplement. `TOMOUSDT` has an
unresolved zero-volume tail after 2023-11-14, so the whole query window stops
before that tail. `AKROUSDT` is outside the cohort and remains explicitly
quarantined because its official notice conflicts with later official trades.

## Evidence and lineage

- `raw/wayback-fapi-exchange-info-20210104195101.json` is the historical
  response body from
  `https://web.archive.org/web/20210104195101id_/https://fapi.binance.com/fapi/v1/exchangeInfo`.
- `raw/wayback-cdx-exchange-info.json` records the Wayback capture identity,
  original official URL, response status, MIME type, and archive digest.
- `price-v1-cohort-activity.json` is a deterministic, per-symbol summary replayed
  from the existing `VERIFIED / curated / PASS` Price V1 snapshot. Each record
  binds the exact curated kline file SHA-256 and retains first bar as proxy only.
- `../expl-017-lifecycle-v1.json` remains the canonical terminal sidecar. It is
  linked, not copied or rewritten.
- `pit-cohort-terminal-evidence-v1.json` binds three additional official
  terminal events and records the unresolved `TOMOUSDT` coverage stop. It does
  not change Lifecycle V1 or infer any terminal from missing/zero-volume bars.
- `pit-instrument-master-v1.json` is rebuilt from those inputs and checked
  byte-for-byte as canonical sorted JSON.

The archived official response proves historical instrument status at that
capture. Its REST `serverTime` is an official response/status timestamp, not a
publication timestamp and not proof that the same metadata was public earlier.
Positive-volume price event timestamps support historical activity, but current
archive bytes do not prove their historical publication or revision vintage.
Numeric Price, Funding, and OI vintage therefore remain `VINTAGE_UNVERIFIED`.

The official [binance-public-data](https://github.com/binance/binance-public-data)
project is used as the format/checksum/revision-policy reference. Its current
symbol helper is not reused as a historical universe source. CCXT, Freqtrade,
Nautilus, and newer archive projects were evaluated as current adapters or
heavier overlapping pipelines; none supplies the missing historical master and
vintage lineage, so no new dependency is introduced.

## Deterministic commands

```text
python3 -m research.data.pit_instrument_master_v1 capture-activity
python3 -m research.data.pit_instrument_master_v1 rebuild
python3 -m research.data.pit_instrument_master_v1 check --verify-price-v1
python3 -m research.data.pit_instrument_master_v1 universe-at 2022-06-30T23:59:59Z
```

`universe-at` is fail-closed outside
`[2021-01-04T19:51:02.039Z, 2023-11-15T00:00:00Z)`. It returns only this fixed
cohort's confirmed active intervals. It must not be labeled the full Binance
market universe or used to claim Tier 2, Strategy, Dry-run, or Live readiness.
