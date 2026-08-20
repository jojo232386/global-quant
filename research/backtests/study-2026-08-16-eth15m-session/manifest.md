# Run Manifest: study-2026-08-16-eth15m-session

> Completed after the run. This manifest does not authorize live trading.

## Identity

- run id: `run-2026-08-16-eth15m-session-1`
- study id: `study-2026-08-16-eth15m-session`
- preregistration reference: `preregistration.md` (locked before running,
  commit `9e1efb7`)
- data checklist reference: `data-checklist.md`
- started (UTC): 2026-08-16T11:30:00Z

## Code and environment

- repository sha256 (HEAD at run): `afa61597c53eb3d327aa8b83096f98512e01b4a2`
- branch / worktree: `continuation/p0p1-gaps` at
  `/Users/ASUS/Desktop/global-quant-continuation`
- python version: 3.12 (system python3)
- runtime version: not used — offline backtest, stdlib only
- random seeds: none (deterministic rule)

## Data

- data version pin: `user_data/data/ethusdt-15m-2026-02-01-2026-08-16.jsonl`
- checksum of inputs: sha256 `af6d8f009dab481103d804aa7599b6e323b9e55e63daa6e26054c6542f1f6d2c`
- fetched at (UTC): 2026-08-16T09:53:31Z
- coverage: 2026-02-01 .. 2026-08-16, 18817 bars, 0 gaps, 0 duplicates

## Configuration

- strategy: the preregistered rule in `preregistration.md`
- timeframe and pairs: 15m, ETHUSDT (Binance USD-M klines)
- stake, leverage, margin mode: 100 USDT notional per trade, 1x, no margin
- cost model applied: `costs/COST_MODEL_BASELINE.md` crypto defaults —
  15 bps per side baseline; stress 30 bps per side + 1 bps funding buffer
- parameters: preregistered defaults (lookback/hold/stoploss 1%)

## Execution

- command line: `./scripts/gmaq-research-backtest --rule session`
- host and runtime duration: local macOS, seconds

## Outputs

| artifact | sha256 |
|---|---|
| `results.json` | computed at commit `afa61597c53eb3d327aa8b83096f98512e01b4a2` |
| `verdict.md` | this study |

- result metrics summary: train 809 trades
  -22.0% (Sharpe -23.3502); OOS
  366 trades -11.3%
  (Sharpe -34.5069); OOS stress -22.5%;
  benchmark 4.6% gross.

## Conclusion

- verdict: REJECT
- exact gate rule that fired: OOS Sharpe -34.5069 < 0.5 AND OOS
  stress net return -22.5% not > 0
- verified vs unverified vs limitations: verified deterministic no-lookahead
  execution and data integrity; unverified taker fee placeholder and funding
  simplification; single instrument, 2-month OOS window.

## Follow-ups

- study closed with REJECT; recorded as failure evidence. Next studies need
  their own preregistration.
