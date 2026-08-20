# Data Availability Checklist: study-2026-08-16-eth15m-volfiltered-momentum

> Filled per `research/data/DATA_AVAILABILITY_CHECKLIST.md`. This checklist
> does not authorize live trading.

## A. Provenance

- source and access method: Binance public `GET /fapi/v1/klines` (credential-free)
- retrieval datetime (UTC): 2026-08-16T09:53:31Z
- coverage window and gaps: 2026-02-01T00:00:00Z .. 2026-08-16T00:00:00Z
  (last bar open); bars=18817, expected=18817, duplicates=0, gaps=0
- field definitions and units: open/high/low/close as strings, volume in
  base asset; open_time_utc_ms in milliseconds
- adjustment method: none (perpetual futures; no splits/dividends; USD-M
  linear contract)
- symbol / venue mapping: `ETHUSDT` on Binance USD-M only
- data snapshot or checksum:
  sha256 `af6d8f009dab481103d804aa7599b6e323b9e55e63daa6e26054c6542f1f6d2c`
  for
  `user_data/data/ethusdt-15m-2026-02-01-2026-08-16.jsonl`
- data version pin: the sha256 above plus
  `user_data/data/ethusdt-15m-2026-02-01-2026-08-16.meta.json`

## B. Quality checks

- duplicates removed: verified — duplicates=0 (evidence: fetch-time scan)
- missing trading sessions enumerated: verified — gaps=0 against the
  expected bar count (evidence: fetch-time scan)
- time misalignment between sources: verified — single source used
- dead / delisted samples handled: n/a — ETHUSDT perpetual traded the whole
  window (status TRADING on 2026-08-16 preflight)
- survivorship bias considered: n/a — single instrument, no cross-section
- lookahead audit (Section C) completed: verified — see table below; the
  backtester is contract-tested for the entry delay
- incompatible mixed sources separated: verified — single source

## C. Timing and availability table

| input | produced at | available at | signal computed at | earliest tradable at |
|---|---|---|---|---|
| 15m candle close | close of candle T | close of candle T | close of candle T | open of candle T+1 |

The backtester executes entries at the open of T+1 and exits at the exit
candle close or the stoploss price, whichever is worse.

## D. Cross-asset alignment (multi-market studies only)

- n/a — single crypto perpetual venue. 24/7 market; no sessions, no
  contract rolls. Funding is not modeled (see sign-off).

## E. Sign-off

- UNKNOWN items and impact: funding carry is not modeled (holding is 1h vs
  8h funding interval; exposure at funding boundary possible but rare).
  Mitigation: the 2x cost stress includes a 1 bps flat funding buffer per
  round trip, listed in the results. Taker fee remains
  PLACEHOLDER_UNVERIFIED until an authenticated account check.
- signed (date UTC): 2026-08-16
