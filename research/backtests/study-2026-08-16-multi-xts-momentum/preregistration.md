# Hypothesis Preregistration: USD-M cross-sectional momentum (long top-3 24h)

> Locked before any data fetch or backtest for this study. Amendments after
> results are observed invalidate the study.
> This study does not authorize live trading.

## 1. Study id and title (required)

- study id: `study-2026-08-16-multi-xts-momentum`
- title: USD-M cross-sectional momentum (long top-3 24h)
- author / session: gmaq continuation session
- date (UTC): 2026-08-16

## 2. Hypothesis (required)

- Investible claim: among the top-15 USD-M perpetuals, a daily-rebalanced long of the 3 symbols with the highest trailing 24h return earns a positive return after conservative costs.
- Mechanism: cross-sectional return continuation at the daily horizon within the liquid perp complex.

## 3. Applicable environment (required)

- asset class / venue: crypto perpetuals, Binance USD-M futures
- instruments: a fixed universe of the top-15 USD-M perpetuals by 24h quote
  volume as of 2026-08-16 (quote asset USDT, stablecoin bases excluded)
- expected regime: any
- holding period: 24 hours per rebalance (daily 00:00 UTC)
- expected capacity: 3 legs x 100 USDT notional at 1x; capacity beyond book
  depth is not claimed

## 4. Predeclared failure conditions (required)

- The edge is considered dead if it survives only at optimistic costs, or
  only in one split, or with fewer than 30 OOS rebalance events.
- The universe is chosen from today's top-15 (survivorship bias upward);
  any PASS must be read with that limitation stated.

## 5. Data plan (required)

- sources: Binance public `/fapi/v1/ticker/24hr` (universe),
  `/fapi/v1/klines` 15m (prices), `/fapi/v1/fundingRate` (funding history)
- fields: open_time_utc_ms, open, high, low, close, volume; fundingTime,
  fundingRate
- coverage: 2026-02-01 to 2026-08-16 (UTC)
- adjustments: none (perpetual futures)
- version pin: per-symbol sha256 recorded in
  `user_data/data/multi/multi-manifest.json`

## 6. Timing and availability (required)

| input | produced at | available at | signal computed at | earliest tradable at |
|---|---|---|---|---|
| 23:45 candle close | T | T | close of T | open of the 00:00 candle (T+1) |
| funding record | fundingTime | fundingTime | fundingTime <= T | open of the 00:00 candle (T+1) |

No future function: rankings use only data timestamped <= T; execution is at
the open of the next 00:00 candle; exits at the following 00:00 open.

## 7. Minimal strategy (required)

- signal (at 23:45 close T): 24h return = close[T] / close[T-96] - 1
- rank symbols descending; long the top 3
- enter at the open of the 00:00 candle; exit at the open of the next day's 00:00 candle

- long-only, equal weight, 3 legs of 100 USDT each, 1x, no stoploss, no
  re-entry between rebalances

## 8. Cost and risk model (required)

- cost model: `costs/COST_MODEL_BASELINE.md` crypto defaults — 15 bps per
  side per leg; stress 30 bps per side + 1 bps flat funding buffer
- limits: 3 legs max, no leverage, daily rebalance only

## 9. Evaluation (required)

- metrics per `gate/EVALUATION_GATE.md`; benchmark = equal-weight buy-and-hold
  of the same 15-symbol universe over the OOS window (gross and net)
- split: 2026-02-01..2026-06-16 train, 2026-06-16..2026-08-16 out-of-sample

## 10. Robustness plan (required)

- train/test split as above
- sensitivity: cost x2 stress; universe size 15 vs 10 (top-10 subset)

## 11. Predeclared PASS/REJECT rule (required)

PASS only if ALL of: out-of-sample annualized Sharpe >= 0.5; out-of-sample
net total return > 0 under the 2x cost stress; at least 30 out-of-sample
rebalance events; and train/OOS net returns have the same sign. Otherwise
REJECT.

## 12. Change log (required)

| date (UTC) | section | change | justification |
|---|---|---|---|
| 2026-08-16 | - | - | none yet |
