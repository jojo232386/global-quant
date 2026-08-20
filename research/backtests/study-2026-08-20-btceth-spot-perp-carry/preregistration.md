# Hypothesis Preregistration: BTC/ETH spot-perpetual carry

> Locked before acquiring the missing spot data, implementing a runner, or
> computing any result. This document authorizes neither exchange access nor
> live trading.

## Identity and current state

- study id: `study-2026-08-20-btceth-spot-perp-carry`
- preregistration date: 2026-08-20 UTC
- project-wide trial context: twelve earlier formal GMAQ studies exist and all
  valid promotion candidates are `REJECT`; this is study 13.
- state: `WAITING_FOR_VERIFIED_DATASET`
- formal-run allowance: zero until the dataset binding below is complete;
  `one formal run` after binding.
- claim: a weekly, unlevered, delta-neutral long-spot/short-perpetual portfolio
  on fixed BTCUSDT and ETHUSDT has positive net OOS carry after actual published
  funding, spot/perpetual basis movement, conservative taker costs, and stress.
- distinction: this tests absolute cash-and-carry, not price direction,
  cross-sectional funding rank, or a funding-shock reversal. It may hold neither,
  one, or both symbols and never takes a net short crypto position.

## External basis, not result evidence

- BIS Working Paper 1087, *Crypto carry*, motivates testing whether leveraged
  futures demand and limited arbitrage capital can sustain crypto basis/carry:
  https://www.bis.org/publ/work1087.htm
- Binance public spot klines and USD-M published funding/mark data are the only
  permitted inputs. No API key, account endpoint, order, or private stream is
  permitted.

These sources motivate a falsifiable test only; they are not evidence that this
implementation will be profitable.

## Mandatory Data Layer V1 binding

- Formal input must be a new Data Layer V1 dataset created only through the
  existing `raw -> validated -> curated` flow and verified with
  `verify_snapshot(..., minimum_stage="curated")` immediately before use.
- Required contents: BTCUSDT and ETHUSDT spot 8-hour UTC OHLCV, USD-M 8-hour
  mark OHLC, and published funding events, all from Binance public endpoints.
- No runner may fetch Binance or consume loose source files. No Data Layer V2,
  DuckDB, Parquet, second source, or new governance framework is allowed.
- Before runner implementation or formal execution, append an immutable binding
  amendment containing all of: curated dataset name, curated dataset ID,
  snapshot-manifest SHA-256, every input-file SHA-256, schema ID, exact coverage,
  and `VERIFIED` replay evidence. That amendment may fill identifiers only; it
  may not change this hypothesis, sample, signal, costs, or gates.
- current dataset name / ID / manifest SHA: `UNASSIGNED` / `UNASSIGNED` /
  `UNASSIGNED`. This is a hard stop, not a value a runner may accept.
- The existing `btceth-weekly-tsmom` curated snapshot is insufficient because
  it contains no spot 8-hour series. Its identifiers must not be relabeled or
  treated as this study's binding.

## Frozen sample and availability

- fixed instruments: BTCUSDT and ETHUSDT only.
- acquisition window: 2020-01-01 00:00 UTC through the last complete 8-hour
  boundary available when the V1 source capture begins.
- train: 2021-01-01 through 2023-12-31 UTC.
- OOS: 2024-01-01 through the last complete bound day; OOS must contain at
  least 104 weekly decisions or the study is `INVALID`.
- decision time: each Monday 00:00 UTC. Only funding settlements and complete
  8-hour bars timestamped strictly before that boundary may form the signal.
- execution time: Monday 08:00 UTC open. This full-bar lag prevents use of a
  boundary close as its simultaneous fill.
- exact contiguous 8-hour coverage, unique timestamps, finite positive OHLC,
  valid envelopes, nonnegative finite volume, and complete published funding
  are mandatory. No forward fill, interpolation, top-N selection, or missing
  event substitution is allowed.

## Frozen strategy and accounting

- For each symbol, sum the 21 funding rates settled in the seven complete days
  ending before Monday 00:00 UTC.
- Baseline all-in roundtrip cost per active symbol is four transaction sides:
  spot entry, perpetual entry, spot exit, perpetual exit. Each side uses
  `taker_fee_rate + baseline_slippage_bps_per_side / 10,000` from the frozen
  cost file.
- A symbol is active for the coming week only when its trailing seven-day
  funding sum is strictly greater than its baseline all-in roundtrip cost.
  Otherwise both legs are flat. There is no ranking or tuned threshold.
- Active symbols share gross capital equally. Each receives at most 50% of
  portfolio capital long spot and the same USDT notional short perpetual; with
  both active, each receives 25% per leg. Total gross notional is at most 1.0x
  equity while net crypto delta at entry is zero. No borrowed spot, pyramiding,
  compounding inside the week, or liquidation-price assumption is allowed.
- Positions execute at Monday 08:00 spot and mark opens and liquidate at the
  next Monday 08:00 opens. Entry and exit both pay taker fee plus slippage.
- Net PnL separately records spot price PnL, perpetual mark-price PnL, every
  funding settlement with `entry_time < fundingTime < exit_time`, and transaction
  cost. Positive funding is received by the short perpetual leg.
- Portfolio equity starts at 1,000 USDT. No cash yield, collateral yield,
  maker rebate, VIP discount, spread improvement, or unstated borrowing return
  is credited.

## Frozen costs and stress

- cost file: `configs/execution-costs.json`
- frozen cost-file SHA-256:
  `effa8ab33aa35f94788e77049adf13a4a0a3f226b68771797eded866d2689dc6`
- baseline uses the file's 5 bps taker fee plus 10 bps slippage per side. These
  are conservative research assumptions, not verified account costs.
- cost stress doubles both fee and slippage on all four transaction sides.
- funding stress retains settlement timestamps and signs but credits only 50%
  of positive funding while charging 100% of negative funding.
- delayed stress executes both legs at Monday 16:00 UTC using the original
  Monday 00:00 information set, with doubled costs and stressed funding.
- A statistical PASS remains `INCONCLUSIVE_FOR_LIVE` until current account fee,
  collateral, margin, liquidation/ADL, transfer, minimum-notional, and
  spot/perpetual execution constraints are independently verified.

## Frozen evaluation and decision rule

Report train and OOS total return, annualized return/volatility, Sharpe,
maximum drawdown, weekly win rate, turnover, active weeks, per-symbol PnL,
funding PnL, basis PnL, costs, each OOS half, cost stress, funding stress,
combined delayed stress, and 2,000 deterministic 4-week circular-block
bootstrap samples with seed 20260820.

`PASS` requires every condition:

- OOS net return > 0, Sharpe >= 0.70, and maximum drawdown <= 15%;
- both contiguous OOS halves and both symbols have positive net contribution;
- cost stress, funding stress, and combined delayed stress each remain positive;
- at least 52 OOS active symbol-weeks and 104 scoreable weekly decisions;
- bootstrap probability of positive compounded OOS return >= 80%;
- no symbol contributes more than 75% of absolute OOS PnL;
- complete V1 provenance binding and exact replay both pass.

`REJECT` applies when the run is scoreable and any condition fails. `INVALID`
applies only to broken data, timing, provenance, implementation, or deviation
from this document. One failed formal result closes the study; no parameter
rescue or immediate replacement is allowed.

## Promotion boundary

A historical PASS would authorize only a no-money, dual-leg shadow ledger of
the identical weekly decisions. It does not authorize building exchange-bound
multi-leg execution, changing the current Freqtrade runtime, using credentials,
or placing orders. Those actions remain out of scope until prospective evidence
passes and the user supplies fresh one-time authorization and capital.
