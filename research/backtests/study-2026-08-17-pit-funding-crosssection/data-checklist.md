# Data Availability Checklist: study-2026-08-17-pit-funding-crosssection

> Filled per `research/data/DATA_AVAILABILITY_CHECKLIST.md`. This checklist
> does not authorize live trading.

## A. Provenance

- source and access method: Binance public REST — ticker/24hr (candidates),
  1d and 15m klines, fundingRate; credential-free
- retrieval datetime (UTC): 2026-08-17 ~07:00Z
- coverage: 2026-02-01 .. 2026-08-16; 197 daily universes; point-in-time
  universe spans 77 symbols out of a 100-symbol candidate
  pool (today's top-100 by 24h quote volume)
- version pin: pit manifest sha256 `4e2a8a7a3eebfa0011657bce0fe3e77b138db1369126a990b135bde31f8ee48f`, universes sha256
  `47bdcd6e646ce3c92a6ca870378b4266c88abe42bac600c11af04aa0fb59f785`
- partial data: {"INTCUSDT": "18664/18817", "SOXLUSDT": "8873/18817", "SPCXUSDT": "8334/18817"}

## B. Quality checks

- duplicates/gaps: fetch-time verified; partial klines recorded above and
  excluded per-day where bars are missing
- survivorship bias: REDUCED — universe rebuilt per day from day D-2
  volumes; residual bias from the candidate pool being today's top-100
  (coins dead before today are absent) is predeclared
- lookahead audit: verified — universe(D) uses only the D-2 daily bar;
  signal at 23:45 of D-1; execution at 00:00 open of D (contract-tested)

## C. Timing and availability table

| input | produced | available | used at |
|---|---|---|---|
| 1d bar D-2 | 00:00 D-1 | 00:00 D-1 | universe for D |
| 23:45 close | T | T | signal |
| 00:00 open | D | D | execution |

## D. Cross-asset alignment

- single venue; daily rebalance; funding carry not charged on 24h holds
  (stress adds 1 bps flat buffer)

## E. Sign-off

- UNKNOWN: candidate-pool survivorship residue; per-symbol corporate
  actions not audited.
- signed (date UTC): 2026-08-17
