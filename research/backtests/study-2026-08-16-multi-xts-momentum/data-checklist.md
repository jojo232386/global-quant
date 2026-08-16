# Data Availability Checklist: study-2026-08-16-multi-xts-momentum

> Filled per `research/data/DATA_AVAILABILITY_CHECKLIST.md`. This checklist
> does not authorize live trading.

## A. Provenance

- source and access method: Binance public REST — `/fapi/v1/ticker/24hr`
  (universe), `/fapi/v1/klines` 15m (prices), `/fapi/v1/fundingRate`
  (funding); all credential-free
- retrieval datetime (UTC): 2026-08-16 (universe) and 2026-08-16 ~11:40Z
  (klines + funding)
- coverage window and gaps: 2026-02-01 .. 2026-08-16; per-symbol bars and
  funding counts recorded in `user_data/data/multi/multi-manifest.json`
  (sha256 `bfd15701fb169a55421fc16dab5c7789c5fdb96bb3b79df679d56bfa9d06eec5`); SNDKUSDT starts 2026-04-03 and is excluded on days
  without bars
- field definitions: open/high/low/close strings, volume base asset,
  open_time_utc_ms; fundingTime ms and fundingRate string
- adjustment method: none — NOTE: no per-symbol corporate-action audit;
  AKEUSDT shows an 81x close range over the window with no >25% single-bar
  gaps (scan done 2026-08-16), so the move is continuous, but its
  inclusion dominates results
- symbol mapping: fixed 15-symbol universe, quote USDT, stablecoin bases
  excluded, sha256-pinned

## B. Quality checks

- duplicates removed: verified (fetch writes one row per bar)
- missing sessions: verified per symbol via manifest bar counts
- time misalignment: verified — single venue, same bar grid
- dead/delisted samples: none in window; SNDKUSDT listed later, handled
- survivorship bias: PRESENT BY DESIGN — universe is today's top-15 by
  volume; results are biased upward and must be read accordingly
- lookahead audit: verified — signal at 23:45 close T, execution at next
  00:00 open, exit at following 00:00 open (contract-tested)
- mixed sources: verified — single venue

## C. Timing and availability table

| input | produced at | available at | signal computed at | earliest tradable at |
|---|---|---|---|---|
| 23:45 close | T | T | close of T | open of 00:00 (T+1) |
| funding record | fundingTime | fundingTime | fundingTime <= T | open of 00:00 (T+1) |

## D. Cross-asset alignment

- single venue, daily rebalance at 00:00 UTC; funding carry is NOT charged
  on held legs (24h holds cross ~3 funding intervals); the 2x cost stress
  adds a 1 bps flat buffer per round trip instead.

## E. Sign-off

- UNKNOWN items: taker fee placeholder (authenticated check pending);
  per-symbol corporate actions not individually audited; selection bias
  as above.
- signed (date UTC): 2026-08-16
