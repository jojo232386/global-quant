# Hypothesis Preregistration Template

> Copy this file into `research/backtests/<study-id>/preregistration.md`,
> fill every `(required)` section, and commit it BEFORE any backtest or data
> exploration for the study. Amendments after results are observed invalidate
> the study. This template does not authorize live trading.

## 1. Study id and title (required)

- study id: `<study-id>`
- title: `<title>`
- author / session: `<author>`
- date (UTC): `<YYYY-MM-DD>`

## 2. Hypothesis (required)

- The investible claim, stated so that a backtest could falsify it:
  `<hypothesis>`
- Mechanism (why it should work, not just that it backtests well):
  `<mechanism>`

## 3. Applicable environment (required)

- asset class / market / venue: `<e.g. crypto perpetuals on Binance USD-M>`
- instruments and pair universe: `<pairs>`
- expected regime or market state the edge requires: `<regime>`
- intended holding period and rebalance frequency: `<period>`
- expected capacity (before impact degrades the edge): `<capacity>`

## 4. Predeclared failure conditions (required)

- Conditions under which the edge is considered dead even if backtests look
  good: `<e.g. edge disappears in a liquidity-stressed regime, or depends on
  a single exchange's data quality>`

## 5. Data plan (required)

- sources and retrieval method: `<source, endpoint/vendor>`
- fields, frequency, timezone: `<fields> @ <frequency> <timezone>`
- coverage window: `<start> - <end>`
- adjustment / mapping rules (splits, rolls, symbol mapping, delistings):
  `<rules>`
- data version pin and checksum policy: `<version + sha256>`
- fill `data/DATA_AVAILABILITY_CHECKLIST.md` for the study before running.

## 6. Timing and availability (required)

- For each signal input, state: when the data is produced (T), when it is
  actually available (T+d), when the signal is computed, and the earliest
  tradable time (T+d''). No future function is allowed.
  | input | produced at | available at | signal at | earliest tradable at |
  |---|---|---|---|---|
  | `<field>` | `T` | `T+d` | `T+d'` | `T+d''` |

## 7. Minimal strategy (required)

- entry rule, exit rule, sizing, cash handling, rebalancing:
  `<rules>`
- explicit stoploss / risk limits: `<limits>`

## 8. Cost and risk model (required)

- cost model applied: `costs/COST_MODEL_BASELINE.md` (state the exact commit
  and the verified values used)
- position limits, leverage, volatility target, drawdown control:
  `<limits>`

## 9. Evaluation (required)

- metrics, benchmark, and acceptance thresholds:
  `<metrics>`, benchmark `<benchmark>`, thresholds `<thresholds>`
- the run is scored by `gate/EVALUATION_GATE.md`.

## 10. Robustness plan (required)

- train/test separation: `<split>`
- walk-forward / rolling windows: `<windows>`
- parameter sensitivity: `<grid>`
- cost and latency stress: `<stress>`
- randomization / Monte Carlo: `<design>`

## 11. Predeclared PASS/REJECT rule (required)

- One sentence that decides the study regardless of narrative:
  `<e.g. PASS only if OOS Sharpe >= X AND 2x-cost stress remains positive AND
  no data-integrity violation; otherwise REJECT.>`

## 12. Change log (required)

- Any amendment after data is observed must be dated and justified here, and
  the amended section re-evaluated as a new study where required.
  | date (UTC) | section | change | justification |
  |---|---|---|---|
