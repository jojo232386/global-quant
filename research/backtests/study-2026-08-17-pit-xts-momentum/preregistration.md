# Hypothesis Preregistration: PIT cross-sectional momentum (long top-3 24h)

> Locked before any data fetch or backtest for this study. Amendments after
> results are observed invalidate the study.
> This study does not authorize live trading.

## 1. Study id and title (required)

- study id: `study-2026-08-17-pit-xts-momentum`
- title: PIT cross-sectional momentum (long top-3 24h)
- author / session: gmaq continuation session
- date (UTC): 2026-08-17

## 2. Hypothesis (required)

- Investible claim: with a point-in-time top-15 universe, a daily-rebalanced long of the 3 highest trailing-24h return symbols earns a positive return after conservative costs.
- Mechanism: daily return continuation within the liquid perp complex.

## 3. Applicable environment (required)

- asset class / venue: crypto perpetuals, Binance USD-M futures
- universe: POINT-IN-TIME — for each trading day D, the top-15 symbols by
  24h quote volume computed from 1d klines of day D-2 (the last complete
  daily bar known at the 23:45 signal time of D-1). Candidate pool: today's
  top-100 USD-M perpetuals by 24h quote volume (quote USDT, stablecoin bases
  excluded). Survivorship bias is reduced but not eliminated: coins that
  died before today are missing from the candidate pool (predeclared).
- holding period: 24 hours per rebalance (daily 00:00 UTC)
- capacity: 3 legs x 100 USDT at 1x

## 4. Predeclared failure conditions (required)

- The edge is considered dead if it survives only at optimistic costs, only
  in one split, with fewer than 30 OOS rebalance events, or only because of
  a handful of extreme symbols.
- Comparison target: the same rule on the biased today-top-15 universe
  (study-2026-08-16-multi-*). If PIT results collapse relative to the
  biased run, the biased PASSes are explained by selection bias.

## 5. Data plan (required)

- sources: Binance public `/fapi/v1/ticker/24hr` (candidate pool),
  `/fapi/v1/klines` interval 1d and 15m, `/fapi/v1/fundingRate`
- coverage: 2026-02-01 .. 2026-08-16 (UTC)
- version pin: sha256 recorded in `user_data/data/pit/pit-manifest.json`

## 6. Timing and availability (required)

| input | produced at | available at | used at |
|---|---|---|---|
| 1d bar of D-2 | 00:00 UTC D-1 | 00:00 UTC D-1 | universe for D |
| 23:45 close T | T | T | signal for D |
| 00:00 open D | D | D | execution |
| funding record | fundingTime | fundingTime | signal (fundingTime <= T) |

No future function: the universe for day D uses only data complete before
the signal time; execution at the 00:00 open; exits at the next 00:00 open.

## 7. Minimal strategy (required)

- universe(D) = top-15 by 1d quote volume of day D-2 among candidates
- rule: PIT cross-sectional momentum (long top-3 24h)
- long-only, equal weight 3 legs x 100 USDT, no stoploss, no intraday
  re-entry

## 8. Cost and risk model (required)

- costs: 15 bps per side per leg; stress 30 bps per side + 1 bps funding
  buffer (taker fee verified on account 2026-08-16: 5 bps)

## 9. Evaluation (required)

- metrics per `gate/EVALUATION_GATE.md`; benchmark = equal-weight buy-hold
  of the PIT top-15 of the first OOS day over the OOS window
- split: train 2026-02-01..06-16, OOS 2026-06-16..08-16

## 10. Robustness plan (required)

- cost x2 stress; comparison vs. the biased today-top-15 run

## 11. Predeclared PASS/REJECT rule (required)

PASS only if ALL of: OOS Sharpe >= 0.5; OOS stress net return > 0; at least
30 OOS rebalance events; train/OOS same sign. Otherwise REJECT.

## 12. Change log (required)

| date (UTC) | section | change | justification |
|---|---|---|---|
| 2026-08-17 | - | - | none yet |
