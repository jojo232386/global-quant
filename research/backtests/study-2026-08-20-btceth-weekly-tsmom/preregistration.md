# Hypothesis Preregistration: BTC/ETH weekly time-series momentum

> Locked before market data is fetched for this study and before any result is
> generated. Any result-driven parameter change requires a new study id. This
> research does not authorize an order, Demo promotion, or live trading.

## Identity and hypothesis

- study id: `study-2026-08-20-btceth-weekly-tsmom`
- preregistration date: 2026-08-20 UTC
- claim: a low-turnover, long-or-cash time-series momentum rule applied to
  BTCUSDT and ETHUSDT USD-M perpetuals has positive net out-of-sample returns
  after conservative execution costs and published funding.
- mechanism: medium-horizon under-reaction can create return persistence;
  moving to cash when an asset's own trailing return is non-positive limits
  exposure during prolonged downtrends.
- reason for this study: the previous 15-minute rules overtraded and the prior
  cross-sectional rules failed under point-in-time reconstruction. This is a
  slower own-history rule on two continuously liquid contracts, not a tuned
  rescue of those failures.

## External basis, not evidence of this result

- Moskowitz, Ooi, and Pedersen (2012), *Time Series Momentum*, documents
  own-return persistence over one- to twelve-month horizons across futures:
  https://doi.org/10.1016/j.jfineco.2011.11.003
- Han, Kang, and Ryu (working paper, version available in 2026) reports stronger
  crypto time-series than cross-sectional momentum but emphasizes liquidation,
  skew, fat tails, and realistic trading assumptions:
  https://doi.org/10.2139/ssrn.4675565
- Binance's official public-data repository states that USD-M kline archives
  derive from `/fapi/v1/klines`, funding is public, and checksum files are
  available: https://github.com/binance/binance-public-data

These sources motivate a falsifiable test only. Their reported returns are not
transferred to GMAQ and are not a PASS.

## Frozen data and sample

- instruments: `BTCUSDT`, `ETHUSDT`, Binance USD-M perpetual contracts only.
- public endpoints only; no API key or private endpoint.
- daily UTC klines and historical funding events.
- acquisition window: 2020-01-01 00:00 UTC through 2026-08-20 00:00 UTC,
  excluding any incomplete bar at or after the end timestamp.
- formal performance window: 2021-01-01 through 2026-08-19 UTC after warm-up.
- train: 2021-01-01 through 2023-12-31 UTC.
- OOS: 2024-01-01 through 2026-08-19 UTC.
- frozen OOS halves: 2024-01-01 through 2025-04-25; 2025-04-26 through
  2026-08-19.
- every bar must have a unique, strictly increasing open timestamp and a
  complete OHLC record. Missing required bars, duplicated timestamps,
  incomplete funding pagination, or data outside the declared window makes
  the run `INVALID` or `INCONCLUSIVE`; missing values are never forward-filled.

## Frozen signal, execution, and portfolio

- decision schedule: every Monday 00:00 UTC.
- information cutoff: the final fully completed daily candle before the Monday
  open. The signal never uses the Monday candle close.
- primary signal: trailing 180-calendar-day close-to-close return for each
  instrument, computed only from completed candles.
- active set: an instrument is active only when its trailing return is strictly
  positive; otherwise its weight is zero.
- portfolio: equal weight across active instruments, long only, cash for unused
  weight, maximum gross exposure 1.0, no shorting, no leverage above 1x.
- rebalance: target weights are applied at the Monday open following the signal.
  Position weights remain fixed until the next scheduled rebalance.
- mark-to-market: daily open-to-next-open returns. Published funding with
  `fundingTime` inside each holding interval is subtracted from long returns.
- turnover: sum of absolute weight changes at each rebalance. No artificial
  round trip is inserted when a weight is unchanged.
- liquidation is impossible in the model because gross exposure is capped at
  1x and no borrowing is modeled; this does not imply live liquidation safety.

## Frozen costs and stress

- baseline per-side fee plus slippage is read only from
  `configs/execution-costs.json`; current declared research value is 15 bps.
- costs are charged on absolute weight turnover at each rebalance and on final
  liquidation for a closed-window metric.
- baseline funding uses every published BTCUSDT/ETHUSDT funding event crossed
  by the modeled position.
- cost/funding stress doubles configured fee and slippage and multiplies actual
  funding by 5.
- delayed-execution stress applies every scheduled target one complete daily
  bar later, with stressed costs and 5x funding; it is not used for tuning.
- no account-specific fee discount, maker fill, rebate, spread improvement, or
  interest on cash is credited.

## Frozen robustness tests

- parameter neighbors: trailing-return lookbacks of 126 and 252 calendar days,
  with every other rule unchanged.
- walk-forward: both frozen OOS halves reported separately.
- benchmark: 50/50 BTCUSDT/ETHUSDT buy-and-hold, rebalanced once at the formal
  window start, with the same gross cap, baseline entry/final costs, and actual
  funding. A cash benchmark is also reported.
- block bootstrap: 2,000 resamples of OOS daily net portfolio returns using
  deterministic seed 20260820 and 28-day circular blocks. Report the
  probability that compounded OOS return is positive.
- contribution: report per-symbol net PnL contribution and the largest share
  of absolute symbol contribution.

## Predeclared PASS, REJECT, and INCONCLUSIVE rules

PASS requires every condition below:

- OOS annualized Sharpe >= 0.5, OOS net return > 0, and OOS stress return > 0;
- OOS maximum drawdown <= 25%; train net return > 0;
- both frozen OOS halves have positive baseline net return;
- both 126-day and 252-day parameter neighbors have positive OOS stress return;
- delayed-execution OOS stress return is positive;
- at least 100 OOS scheduled rebalance observations are scoreable;
- block-bootstrap probability of positive compounded OOS return >= 70%;
- no symbol contributes more than 75% of absolute OOS symbol PnL;
- all kline and funding integrity/coverage checks pass, the run manifest pins
  every input hash, and the benchmark is reported.

REJECT applies when the study is scoreable and any statistical or robustness
condition fails. A strong benchmark does not rescue a failed rule.

INCONCLUSIVE applies only when the statistical gates pass but a required
external data, funding, cost, or provenance input is incomplete. Missing
evidence never softens an observed statistical failure.

## Frozen parameter record

`lookback_days=180`, `neighbors=[126,252]`, `rebalance=Monday 00:00 UTC`,
`gross_cap=1.0`, `weights=equal_active`, `direction=long_or_cash`,
`train_end=2023-12-31`, `oos_start=2024-01-01`,
`oos_midpoint=2025-04-26`, `bootstrap_seed=20260820`,
`bootstrap_samples=2000`, `bootstrap_block_days=28`.

No threshold, split, signal, cost, neighbor, delay, or PASS rule may be changed
after `results.json` is observed.
