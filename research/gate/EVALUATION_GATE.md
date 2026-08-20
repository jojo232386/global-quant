# Evaluation Gate

> This gate scores research runs only. This gate does not authorize live trading. A research PASS does not authorize dry-run promotion, live configuration, credentials, account changes, or orders. Promotion and live deployment are separate, explicitly authorized steps (see `configs/LIVE_READINESS.md`).

## Mandatory reported metrics

Every formal run must report, with definitions, sample range, and benchmark:

- total return
- annualized return
- annualized volatility
- maximum drawdown (and its duration)
- Sharpe and/or Sortino
- Calmar
- win rate
- profit factor
- turnover
- number of trades
- holding period
- maximum consecutive losses
- benchmark comparison (state the benchmark and its source)

Curve screenshots are supplementary; they cannot replace the metric table.
Definitions must state return frequency, compounding, and cost treatment.

## Hard gates

The following conditions force REJECT (or INVALID where noted), regardless of
any metric:

- no preregistration or no run manifest -> not evidence, cannot be scored
- lookahead or data-integrity violation found -> INVALID (fix and rerun)
- out-of-sample failure (predeclared rule not met) -> REJECT
- edge dies under the cost stress (fees x2, slippage x2, latency x2,
  funding x5) -> REJECT
- parameter count inconsistent with sample size, test set used for tuning, or
  unreported trial counts -> REJECT (data-mining risk)
- benchmark comparison missing -> INCONCLUSIVE at best

## Robustness evidence required for PASS

- train / test separation as preregistered
- walk-forward or rolling-window results
- parameter sensitivity around the chosen set
- cost and latency stress results
- randomization or Monte Carlo result where feasible

## Evidence separation

Every conclusion must separate:

1. verified results (metrics with exact scope),
2. unverified assumptions (data, cost, regime),
3. limitations (survivorship, liquidity, capacity),
4. next verification steps.

## Promotion rules

- PASS means the strategy clears the research gate for the stated
  environment only.
- Promotion to the active runtime requires a new, separate review, a
  strategy separated from `LiveExecutionCanaryStrategy`, dry-run evidence on
  the promoted layout, and explicit authorization.
- Live deployment additionally requires every item in
  `configs/LIVE_READINESS.md` with a verified, dated read-only check.
