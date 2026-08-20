# Run Manifest: study-2026-08-16-eth15m-momentum

> Completed after the run. This manifest does not authorize live trading.

## Identity

- run id: `run-2026-08-16-eth15m-momentum-1`
- study id: `study-2026-08-16-eth15m-momentum`
- preregistration reference and sha256: `preregistration.md` (locked before
  data fetch, commit `9b7c730`)
- data checklist reference and sha256: `data-checklist.md`
- started (UTC): 2026-08-16T09:50:00Z

## Code and environment

- repository sha256 (HEAD at run): `257a345eff9017fb308c9fb51ab9ef25a385e249`
- branch / worktree: `continuation/p0p1-gaps` at
  `/Users/ASUS/Desktop/global-quant-continuation`
- python version: 3.12 (system python3)
- runtime version: not used — offline backtest, no Freqtrade dependency
- dependency lock: stdlib only
- random seeds: none (deterministic rule)

## Data

- data version pin and snapshot id:
  `user_data/data/ethusdt-15m-2026-02-01-2026-08-16.jsonl`
- checksum of inputs:
  sha256 `af6d8f009dab481103d804aa7599b6e323b9e55e63daa6e26054c6542f1f6d2c`
- fetched at (UTC): 2026-08-16T09:53:31Z
- coverage window used: 2026-02-01 .. 2026-08-16, 18817 bars, 0 gaps,
  0 duplicates

## Configuration

- strategy class: long-only 15m momentum (preregistered rule)
- timeframe and pairs: 15m, ETHUSDT (Binance USD-M klines)
- stake, leverage, margin mode: 100 USDT notional per trade, 1x, no margin
- cost model applied: `costs/COST_MODEL_BASELINE.md` crypto defaults —
  15 bps per side baseline (taker 5 bps PLACEHOLDER_UNVERIFIED + slippage
  10 bps); stress 30 bps per side + 1 bps funding buffer
- parameters: lookback 4 bars, hold 4 bars, stoploss 1%, one position

## Execution

- command line: `./scripts/gmaq-research-backtest`
- started / finished (UTC): 2026-08-16 ~10:05Z / ~10:06Z
- host and runtime duration: local macOS, seconds

## Outputs

| artifact | sha256 |
|---|---|
| `results.json` | computed at commit `257a345` |
| `verdict.md` | this study |

- result metrics summary: train 1611 trades -45.0% (Sharpe -27.3); OOS 736
  trades -21.6% (Sharpe -48.3); OOS 2x-cost stress -44.3%; benchmark
  buy-and-hold OOS +5.0% gross / +4.7% net.

## Conclusion

- verdict: REJECT
- exact gate rule that fired: OOS Sharpe -48.3 < 0.5 AND OOS stress net
  return -44.3% not > 0
- verified results vs. unverified assumptions vs. limitations:
  - verified: deterministic rule, no lookahead (contract-tested), data
    integrity (0 gaps/duplicates), cost math
  - unverified: taker fee is the VIP0 placeholder until authenticated
    account check; funding not modeled (1 bps stress buffer only)
  - limitations: single instrument, single rule, no regime filter, 2-month
    OOS window

## Follow-ups

- study closed with REJECT: the gate rejected honestly; the edge is absent
  after conservative costs. Recorded as failure evidence per
  `research/gate/EVALUATION_GATE.md`.
- next research: only with a new preregistration (different rule or
  different cost-verified environment).
