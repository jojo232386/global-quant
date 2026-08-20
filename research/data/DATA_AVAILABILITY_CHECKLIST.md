# Data Availability Checklist

> Fill one copy per study into `research/backtests/<study-id>/data-checklist.md`
> before running. Every item must be either verified with evidence (name the
> evidence) or explicitly marked UNKNOWN. UNKNOWN items must not be used to
> claim results. This checklist does not authorize live trading.

## A. Provenance

- source and access method: `<vendor / public endpoint>`
- retrieval datetime (UTC) and frequency: `<when fetched, how often>`
- coverage window and gaps: `<start - end; known gaps>`
- field definitions and units: `<fields>`
- adjustment method (splits, dividends, contract rolls, stablecoin peg):
  `<method>`
- symbol / venue mapping table: `<mapping>`
- data snapshot or checksum: `<snapshot id / sha256>`
- data version pin: `<version>`

## B. Quality checks

- duplicates removed: verified / UNKNOWN — evidence `<...>`
- missing trading sessions enumerated: verified / UNKNOWN — evidence `<...>`
- time misalignment between sources: verified / UNKNOWN — evidence `<...>`
- dead / delisted samples handled: verified / UNKNOWN — evidence `<...>`
- survivorship bias considered: verified / UNKNOWN — evidence `<...>`
- lookahead audit (Section C) completed: verified / UNKNOWN — evidence `<...>`
- incompatible mixed sources separated: verified / UNKNOWN — evidence `<...>`

## C. Timing and availability table

- One row per signal input. "Produced" is when the data exists; "available"
  is when a subscriber could really see it; "tradable" is the earliest time a
  conservative execution could act on it.
  | input | produced at | available at | signal computed at | earliest tradable at |
  |---|---|---|---|---|

## D. Cross-asset alignment (multi-market studies only)

- trading calendars and sessions per market (US equities, gold spot/futures,
  crypto 24/7): `<sessions>`
- holidays, halts, and venue outages: `<events>`
- contract roll schedule for gold futures: `<roll>`
- crypto venue differences (price, funding, depth) and stablecoin basis:
  `<differences>`
- FX / quoting currency and conversion timing: `<currency>`
- macro and earnings publication times vs. real availability: `<releases>`

## E. Sign-off

- all items above are verified with evidence, or listed here as UNKNOWN with
  their impact: `<list of UNKNOWN items and impact>`
- signed (date UTC): `<YYYY-MM-DD>`
