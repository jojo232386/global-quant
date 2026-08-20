# Hypothesis Preregistration: BTC/ETH volatility-scaled long/short TSMOM

> Locked before this study fetches market data, implements the runner, or
> computes a result. One formal run is allowed. A failed gate closes the study;
> no result-driven parameter rescue is permitted.

## Identity, trial context, and claim

- study id: `study-2026-08-20-btceth-volscaled-ls-tsmom`
- preregistration date: 2026-08-20 UTC
- project-wide trial context: 11 earlier formal GMAQ result directories exist.
  Two early cross-sectional PASS artifacts were later invalidated by
  point-in-time reconstruction; all valid promotion candidates are REJECT.
  This is therefore study 12, not an independent first test, and any PASS must
  use the stricter gates below and then enter prospective shadow validation.
- claim: a symmetric medium-horizon trend rule on fixed BTCUSDT and ETHUSDT
  USD-M perpetuals, with ex-ante volatility scaling and no leverage above 1x,
  has positive net out-of-sample performance after taker costs, published
  funding, adverse-funding stress, and delayed execution.
- mechanism: medium-horizon trend persistence is directionally symmetric;
  scaling notional down when realized volatility rises should reduce crash
  concentration without using a forecast of the next return.
- distinction from the closed 2026-08-20 study: that rule was long-or-cash and
  equal-weight. This rule is always directional when a non-zero signal exists,
  permits shorts, and sizes each leg from information available before entry.
  The old result is not reused, overwritten, or rescored.

## External basis, not result evidence

- Moskowitz, Ooi, and Pedersen (2012), *Time Series Momentum*, motivates a
  symmetric own-return trend sign over one-to-twelve-month horizons:
  https://doi.org/10.1016/j.jfineco.2011.11.003
- Moreira and Muir (2017), *Volatility-Managed Portfolios*, motivates reducing
  exposure when ex-ante volatility is high:
  https://doi.org/10.1111/jofi.12513
- Binance public USD-M klines, funding history, and mark-price klines are the
  only market inputs. No account or private endpoint is permitted.

These sources motivate a falsifiable test only. They do not transfer a return
claim to GMAQ.

## Frozen data and sample

- instruments: `BTCUSDT`, `ETHUSDT`, Binance USD-M perpetuals only; the pair is
  fixed before acquisition, so no current top-N universe selection is used.
- public endpoints only; no API key, account field, order, or private stream.
- daily UTC trade klines, published funding events, and 8-hour mark-price
  klines for funding completeness.
- acquisition window: 2020-01-01 00:00 UTC through 2026-08-20 00:00 UTC,
  excluding the incomplete end-date candle except its already-fixed open used
  as the final valuation boundary.
- train: 2021-01-01 through 2023-12-31 UTC.
- OOS: 2024-01-01 through 2026-08-19 UTC.
- frozen OOS halves: 2024-01-01 through 2025-04-25 and 2025-04-26 through
  2026-08-19.
- bars must be unique, strictly increasing, gap-free daily UTC OHLC with
  positive finite prices. Funding pagination and mark-price fallback coverage
  must span the exact request window. Missing, duplicated, non-finite, or
  out-of-window data makes the run `INVALID`; no forward fill is allowed.
- funding events are included in a holding interval exactly when
  `interval_open < fundingTime <= interval_close`. If the historical funding
  row has no positive mark price, use the open of the public 8-hour mark-price
  candle whose timestamp equals the funding bucket. The fallback source and
  count must be pinned; any missing bucket is `INVALID`.
- sorted funding timestamps must start no later than eight hours after the
  acquisition start, end no earlier than eight hours before its boundary, and
  have no gap greater than eight hours. Request pagination must report the
  complete exact window; duplicate or non-finite rates are `INVALID`.

## Frozen signal, sizing, and execution

- decision schedule: every Monday 00:00 UTC; execution schedule: Tuesday
  00:00 UTC. This one-day primary lag prevents using a daily candle that closes
  at the same instant as its execution price.
- information cutoff: the Sunday daily candle that closes at Monday 00:00 UTC
  and all earlier candles. No Monday candle field is available to the target.
- direction: sign of the trailing 180-calendar-day close-to-close return for
  each symbol. Positive is long, negative is short, exactly zero is flat.
- realized volatility: annualized sample standard deviation of the latest 63
  completed daily close-to-close log returns, using `sqrt(365)`. For a Monday
  decision at `t`, the 63 returns are formed from completed daily-bar closes
  whose bar-open timestamps are `t-64d` through `t-1d`; the trend return uses
  closes from bars opened at `t-181d` and `t-1d`. The target executes at
  `t+1d` without reading that intervening bar.
- per-symbol target weight:
  `direction * min(0.50, 0.10 / max(realized_volatility, 0.20))`.
  Thus each symbol targets at most 10% ex-ante annualized volatility, absolute
  weight never exceeds 0.50, and portfolio gross exposure never exceeds 1.0.
- no cross-symbol normalization, borrowing return, cash yield, pyramiding, or
  leverage above the declared gross cap.
- targets execute at Tuesday open with taker assumptions and are held until
  the next scheduled Tuesday execution. Daily PnL is open-to-next-open.
- positive funding is paid by longs and received by shorts; negative funding
  is received by longs and paid by shorts. Baseline uses every published event
  crossed by the signed position.
- turnover is the sum of absolute signed-weight changes. Long-to-short or
  short-to-long therefore incurs the full close-plus-open turnover.
- the final boundary liquidates every signed position and charges taker cost.
- accounting starts each train/OOS simulation with 1,000 USDT cash and zero
  units. At a rebalance, cost is deducted first; signed units are set to target
  weight times post-cost equity divided by that symbol's open. Residual cash is
  post-cost equity minus signed marked value. Between rebalances units are
  fixed; equity is `cash + sum(signed_units * current_open)`. Short proceeds in
  cash are therefore offset by the negative marked liability rather than
  credited as return. Funding changes cash at the event mark notional.
- pretrade weight is signed marked value divided by pretrade equity. Turnover
  is `sum(abs(target_weight - pretrade_weight))`; transaction cost is pretrade
  equity times turnover times the configured per-side rate. Target units then
  use post-cost equity. This ordering is fixed and has no circular solver.
- baseline funding cash change is
  `-signed_units * event_mark_price * funding_rate`; stress funding cash change
  is always `-abs(signed_units * event_mark_price) * 5 * abs(funding_rate)`.
- daily net return is next-open equity divided by current-open pre-trade equity
  minus one, including any rebalance cost and funding crossed that day. Annual
  return uses 365-day compounding; annual volatility and zero-rate Sharpe use
  sample standard deviation times `sqrt(365)`. Maximum drawdown uses the full
  daily equity path including its initial boundary.

## Frozen costs, stress, and short-risk checks

- baseline fee plus slippage comes only from
  `configs/execution-costs.json`; the current declared value is 15 bps per
  side. No maker fill, rebate, VIP discount, or spread improvement is credited.
- cost stress doubles configured fee/slippage.
- funding stress is always adverse: every crossed event charges
  `5 * abs(funding_rate) * abs(position_notional)` regardless of direction.
  A favorable historical funding credit can never improve the stress result.
- delayed stress applies each target one additional complete daily bar later
  and combines stressed cost with adverse funding stress. The target is still
  computed from the original Monday information set, executes Wednesday open,
  and remains in force until the next precomputed target executes the following
  Wednesday; no Tuesday or Wednesday candle information enters that target.
- for every short holding day, report the worst trade-kline high relative to
  the position's current signed weighted-average short entry price. Increasing
  a short recomputes average entry by unit-weighted entry notionals; reducing a
  short preserves it; closing or crossing through zero clears it and a new
  short starts at that rebalance open. If any observed high is 50% or more
  above that entry, or finite OHLC evidence is unavailable, the short-risk gate
  fails. This is a conservative screen, not an account-specific liquidation
  model.

## Frozen robustness tests

- trend lookback neighbors: 126 and 252 calendar days, with volatility window
  fixed at 63 days.
- volatility-window neighbors: 42 and 84 completed daily returns, with trend
  lookback fixed at 180 days.
- the frozen OOS halves are contiguous slices of the one primary OOS daily
  return/equity path. The second half inherits the actual position and equity
  at the split; neither half is restarted or allowed a new warm-up.
- one-day delayed execution uses stressed cost and adverse funding.
- benchmarks: cash and 50/50 BTC/ETH long-only buy-and-hold, with baseline
  entry/final costs and actual funding.
- block bootstrap: 2,000 circular-block resamples of OOS daily net returns,
  deterministic seed 20260820, 28-day blocks.
- report signed per-symbol net PnL contribution and largest absolute
  contribution share.
- train, primary OOS, each parameter neighbor, delayed stress, and benchmarks
  are separate simulations initialized only at their declared window start.
  Neighbor and delayed results cannot reuse a primary-run position path.
- zero daily-return variance gives Sharpe `0`; non-positive equity or any daily
  return `<= -1` is `INVALID`. A zero absolute contribution denominator sets
  largest contribution share to `1.0`, so it cannot pass concentration.

No other parameter, filter, stop, threshold, rebalance day, or asset may be
tested under this study id.

## Predeclared PASS, REJECT, and INVALID rules

PASS requires every condition below:

- OOS annualized Sharpe >= 0.70, OOS net return > 0, and OOS adverse-funding
  stress net return > 0;
- OOS maximum drawdown <= 20% and train net return > 0;
- both frozen OOS halves have positive baseline net return;
- every 126/252-day trend neighbor and 42/84-day volatility neighbor has
  positive OOS adverse-funding stress return;
- delayed stressed OOS return is positive;
- at least 100 OOS scheduled weekly decisions are scoreable;
- block-bootstrap probability of positive compounded OOS return >= 80%;
- no symbol contributes more than 75% of absolute OOS PnL;
- the short-risk gate passes, complete data/provenance hashes are pinned, and
  both benchmarks are reported.

REJECT applies when the run is scoreable and any statistical, robustness, or
short-risk condition fails. One failed condition closes the study.

INVALID applies only to broken implementation, timing, data, provenance, or a
deviation from this preregistration. INVALID is not evidence of profitability
and closes this study id without a result. A defect correction may run only
under a new study id that copies these economic rules unchanged and discloses
the invalid predecessor; this study id itself always permits at most one formal
execution.

## Promotion boundary

- A historical PASS would authorize only implementation of the identical
  signal in a no-money prospective shadow/dry-run candidate.
- It would still require at least eight consecutive weekly shadow decisions,
  exact signal/order-intent replay, zero unknown outcomes, completed 48-hour
  runtime reliability evidence, current account/risk evidence, and a new
  explicit one-time authorization before any funded canary.
- A historical REJECT ends this hypothesis. No parameter rescue or immediate
  replacement study is allowed in this phase.

## Frozen parameter record

`direction=long_short`, `trend_lookback_days=180`,
`trend_neighbors=[126,252]`, `volatility_window_days=63`,
`volatility_neighbors=[42,84]`, `per_symbol_vol_target=0.10`,
`volatility_floor=0.20`, `per_symbol_abs_cap=0.50`, `gross_cap=1.0`,
`decision=Monday 00:00 UTC`, `execution=Tuesday 00:00 UTC`,
`delayed_execution=Wednesday 00:00 UTC`, `baseline_cost=config`,
`stress_cost=config_x2`, `stress_funding=5x_absolute_adverse`,
`delay_days=1`, `bootstrap_seed=20260820`, `bootstrap_samples=2000`,
`bootstrap_block_days=28`.

Frozen execution-cost config SHA-256:
`effa8ab33aa35f94788e77049adf13a4a0a3f226b68771797eded866d2689dc6`.
Any cost-config byte change before the formal run makes this preregistration
`INVALID`; it cannot silently adopt new costs.
