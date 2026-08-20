# Hypothesis Preregistration: prospective BTC/ETH relative-value mean reversion

> Locked before prospective data exists, before a runner is implemented, and
> before any result is computed. This document does not authorize exchange
> credentials, orders, Demo entry, or live trading.

## Identity and state

- study id: `study-2026-08-21-btceth-relative-value-forward`
- project-wide trial context: this is study 14 after thirteen recorded GMAQ
  studies and no current VERIFIED curated V1 PASS.
- state: `WAITING_FOR_PROSPECTIVE_WINDOW`
- formal-run allowance: zero before the frozen window ends and a complete V1
  binding exists; `one formal run` afterward.
- contamination boundary: the 2024–2026 history has already been observed by
  other studies and cannot establish a new promotion PASS. Every scored return
  in this study must begin at or after 2026-08-21 00:00 UTC.

## Hypothesis and distinction

- Falsifiable claim: temporary 8-hour deviations of the fixed ETH/BTC USD-M
  mark-price ratio from its trailing 90-day mean revert fast enough that a
  symmetric long/short perpetual pair earns positive prospective net returns
  after taker costs, slippage, and published funding.
- Mechanism: BTC and ETH share a broad crypto market factor, while temporary
  relative demand and positioning shocks can dislocate their ratio. The claim
  is statistical, not risk-free arbitrage; structural repricing can prevent
  convergence.
- This is not directional TSMOM, funding rank/shock, spot-perpetual carry, or
  a threshold rescue of a rejected study. The pair, sample, signal, holding
  limit, costs, and gates below are frozen before the prospective window.
- Mechanism reference only: Tadi and Kortchmeski, *Evaluation of Dynamic
  Cointegration-Based Pairs Trading Strategy in the Cryptocurrency Market*,
  https://arxiv.org/abs/2109.10662. The paper motivates testing mean reversion
  with explicit execution constraints; it is not evidence for this rule.

## Applicable environment and failure conditions

- venue/instruments: Binance USD-M `BTCUSDT` and `ETHUSDT` perpetuals only.
- frequency/holding period: 8-hour decisions; maximum 18 buckets (6 days).
- capacity claim: none. Formal evidence is normalized at 1,000 USDT and cannot
  establish executable capacity, account eligibility, or live profitability.
- The hypothesis is dead if net prospective performance fails any frozen gate,
  if either half is negative, if costs/funding dominate the gross edge, if the
  minimum trade count is absent, or if the relative ratio does not converge
  under delayed execution. One failure closes the study with no parameter rescue.

## Mandatory Data Layer V1 binding

- Formal input must be produced only by the existing Data Layer V1
  `raw -> validated -> curated` flow and replay as `VERIFIED / curated / PASS`
  through `verify_snapshot(..., minimum_stage="curated")` immediately before
  the run.
- Dataset family: the existing `btceth-weekly-tsmom` V1 schema, because it
  already contains fixed-symbol BTC/ETH 8-hour mark OHLC and published funding.
  A new snapshot extends the dates; this does not create Data Layer V2, a
  second source, DuckDB, Parquet, or a new governance framework.
- Required acquisition identity: `gmaq-fetch-tsmom --study-id
  study-2026-08-21-btceth-relative-value-forward`; its manifest must bind this
  preregistration SHA before migration.
- Current dataset ID / snapshot-manifest SHA / schema ID / input-file SHAs:
  `UNASSIGNED` / `UNASSIGNED` / `UNASSIGNED` / `UNASSIGNED`. These values are
  a hard stop, not placeholders a runner may accept.
- Before runner implementation or formal execution, add one immutable binding
  amendment containing the curated dataset ID, manifest/schema/file SHAs,
  exact coverage, parent snapshots, and successful registry replay.
- No formal runner may fetch Binance, read loose source files, or accept a path
  in place of the bound dataset ID.

## Frozen data window and timing

- warm-up only: 2026-05-23 00:00 UTC through 2026-08-21 00:00 UTC; no return,
  trade, threshold choice, or gate may be scored in this interval.
- prospective OOS: 2026-08-21 00:00 UTC through 2027-02-17 00:00 UTC,
  end-exclusive (180 complete days / 540 expected 8-hour buckets).
- fields: BTCUSDT and ETHUSDT mark-price 8-hour OHLC plus every published
  funding event. Daily klines may remain in the V1 snapshot but are not signal
  inputs.
- quality: exact contiguous 8-hour opens, unique timestamps, finite positive
  OHLC with valid envelopes, complete funding request-window evidence, and no
  forward fill or interpolation.
- At boundary T, only the close and funding events from buckets ending at or
  before T are available. The signal is computed after T and may execute only
  at the next 8-hour mark open T+8h. All legs use the same delayed boundary.

## Frozen minimal strategy

- Define `r = log(ETHUSDT mark close / BTCUSDT mark close)` over the last 270
  complete 8-hour buckets (90 days), including the just-completed bucket.
- Compute the population mean and sample standard deviation over those 270
  values. A zero/non-finite standard deviation invalidates that decision.
- When flat, if `z=(r-mean)/stdev >= 2.0`, execute 0.5x long BTC and 0.5x short
  ETH gross notional at the next 8-hour opens. If `z <= -2.0`, reverse both
  legs. Otherwise remain in cash.
- When invested, exit both legs at the next eligible opens after `abs(z) <=
  0.5`, after 18 held buckets, or after `abs(z) >= 4.0`, whichever signal is
  observed first. No same-boundary fill, pyramiding, overlap, partial pair, or
  retry is allowed.
- Initial equity is 1,000 USDT; gross notional is at most 1.0x, leverage is 1x,
  and at most one two-leg pair is open. Every funding settlement strictly
  inside a holding interval is charged/credited to its corresponding leg.

## Frozen costs, risk, and evaluation

- Cost source: committed `configs/execution-costs.json`, pinned by SHA in the
  later binding. Baseline applies taker fee plus baseline slippage to each of
  four entry/exit leg sides. No maker rebate, VIP discount, cash yield, or
  favorable spread assumption is credited.
- Stress A doubles fees and slippage; stress B delays both entry and exit by
  one additional 8-hour bucket while preserving the original information set;
  stress C combines both and charges adverse funding as published.
- Report net/gross return, annualized return/volatility, Sharpe, Calmar,
  maximum drawdown and duration, win rate, profit factor, turnover, completed
  trades, holding buckets, consecutive losses, leg/funding/cost attribution,
  both contiguous 90-day halves, and 2,000 deterministic 6-bucket circular
  block-bootstrap samples with seed 20260821.
- Benchmarks: zero-return cash and a continuously held 0.5x long ETH / 0.5x
  short BTC relative-value pair, both with consistent funding/cost treatment.
- Frozen robustness diagnostics only: lookbacks 216 and 324 buckets and entry
  z thresholds 1.75 and 2.25, one variable at a time. They cannot replace the
  primary rule or rescue its verdict.

`PASS` requires all of the following:

- prospective net return > 0, annualized Sharpe >= 0.80, and maximum drawdown
  <= 15%;
- both contiguous 90-day halves positive and at least 60% of completed trades
  profitable after baseline costs;
- minimum 30 completed trades, with no single trade contributing more than
  25% of positive gross PnL;
- stress A, stress B, and stress C each have positive net return;
- bootstrap probability of positive compounded return >= 80%;
- the primary rule beats both frozen benchmarks and no integrity/timing/V1
  binding check fails.

`REJECT` applies when the window is complete and any scoreable gate fails.
`INCONCLUSIVE` applies only when the prospective window yields fewer than the
minimum 30 completed trades despite complete valid data. `INVALID` is reserved
for broken provenance, timing, implementation, or data. Neither outcome may be
converted to PASS by changing a parameter or reusing the same window.

## Promotion boundary and change log

- A PASS would authorize only a no-money shadow ledger using the identical
  frozen rule. It would not authorize credentials, account changes, an
  execution adapter, orders, Demo entry, or live trading.
- Any amendment to the hypothesis, dates, signal, costs, or gates after this
  commit creates a new study ID. The future binding amendment may fill only
  immutable V1 identifiers and hashes.
- change log: 2026-08-21 UTC — initial preregistration before prospective data.
