# Hypothesis Preregistration: ETHUSDT 15m momentum

> Locked before any data fetch or backtest for this study. Amendments after
> results are observed invalidate the study.
> This study does not authorize live trading.

## 1. Study id and title (required)

- study id: `study-2026-08-16-eth15m-momentum`
- title: ETHUSDT perpetual 15m time-series momentum (long-only)
- author / session: gmaq continuation session
- date (UTC): 2026-08-16

## 2. Hypothesis (required)

- Investible claim: after a positive 1-hour return (4 x 15m candles) on
  ETHUSDT perpetual, the following 1-hour return is positive on average,
  after conservative costs.
- Mechanism: short-horizon return continuation driven by persistent order
  flow; expected to be weak or absent after costs — the study tests whether
  any edge survives conservative cost and stress assumptions.

## 3. Applicable environment (required)

- asset class / venue: crypto perpetuals, Binance USD-M futures
- instruments: `ETHUSDT` only
- expected regime: any; no regime filter preregistered
- holding period: 4 candles (1 hour); rebalance at candle close
- expected capacity: 100 USDT notional per trade at 1x; capacity beyond
  book depth is not claimed

## 4. Predeclared failure conditions (required)

- The edge is considered dead if it survives only at optimistic costs, or
  only in one of the two time splits, or only because of a handful of trades
  (fewer than 30 OOS trades).

## 5. Data plan (required)

- source: Binance public `/fapi/v1/klines`, symbol ETHUSDT, interval 15m
- fields: open_time_utc_ms, open, high, low, close, volume
- coverage: 2026-02-01 to 2026-08-16 (UTC)
- adjustments: none (perpetual futures, no splits/dividends)
- version pin: sha256 of the fetched JSONL recorded in the run manifest

## 6. Timing and availability (required)

| input | produced at | available at | signal computed at | earliest tradable at |
|---|---|---|---|---|
| 15m candle close | T | T (after exchange finalizes the candle) | close of T | open of T+1 candle |

No future function: the signal uses only candles up to T; execution is at
the open of T+1; exits use the exit candle's close or the stoploss price
whichever is worse.

## 7. Minimal strategy (required)

- entry: long 100 USDT notional at the open of candle T+1 when the return
  over candles T-3..T is > 0
- exit: at close of candle T+1+4, or at the stoploss price when the exit
  candle's low breaches entry * (1 - 1%), whichever is worse
- no shorting, no leverage beyond 1x, one position at a time, no
  re-entries while a position is open

## 8. Cost and risk model (required)

- cost model: `costs/COST_MODEL_BASELINE.md` crypto defaults — taker fee
  5 bps (PLACEHOLDER_UNVERIFIED) + conservative slippage 10 bps per side;
  15 bps per side total, 30 bps round trip
- stress: 2x per-side costs (30 bps/side) plus the funding simplification
  documented in the data checklist
- limits: stoploss 1%, max one open position, no leverage

## 9. Evaluation (required)

- metrics per `gate/EVALUATION_GATE.md`: total return, annualized return,
  annualized volatility, max drawdown, Sharpe, Sortino, Calmar, win rate,
  profit factor, turnover, trade count, holding period, max consecutive
  losses, benchmark comparison (ETHUSDT buy-and-hold over the same window)
- split: 2026-02-01..2026-06-16 train, 2026-06-16..2026-08-16 out-of-sample
- benchmark: buy-and-hold 100 USDT over the out-of-sample window

## 10. Robustness plan (required)

- train/test split as above
- sensitivity: cost x2 stress; holding period 2 vs 4 vs 8 candles on train
- randomization: none preregistered (single rule, fixed parameters)

## 11. Predeclared PASS/REJECT rule (required)

PASS only if ALL of: out-of-sample annualized Sharpe >= 0.5; out-of-sample
net total return > 0 under the 2x cost stress; at least 30 out-of-sample
trades; and train/OOS net returns have the same sign. Otherwise REJECT.

## 12. Change log (required)

| date (UTC) | section | change | justification |
|---|---|---|---|
| 2026-08-16 | - | - | none yet |
