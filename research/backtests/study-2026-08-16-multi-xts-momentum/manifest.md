# Run Manifest: study-2026-08-16-multi-xts-momentum

> Completed after the run. This manifest does not authorize live trading.

## Identity

- run id: `run-2026-08-16-xts_momentum-1`
- study id: `study-2026-08-16-multi-xts-momentum`
- preregistration reference: `preregistration.md` (locked before running,
  commit `b4623da`)
- data checklist reference: `data-checklist.md`
- started (UTC): 2026-08-16T12:00:00Z

## Code and environment

- repository sha256 (HEAD at run): `ce6429d091b98a5cf56a75f74fcc49ec77cb4c17`
- branch / worktree: `continuation/p0p1-gaps` at
  `/Users/ASUS/Desktop/global-quant-continuation`
- python version: 3.12 (system python3)
- runtime version: offline backtest, stdlib only
- random seeds: none (deterministic rule)

## Data

- data version pin: `user_data/data/multi/` (universe + per-symbol klines +
  funding), manifest sha256 `bfd15701fb169a55421fc16dab5c7789c5fdb96bb3b79df679d56bfa9d06eec5`
- coverage: 2026-02-01 .. 2026-08-16; 15 symbols; SNDKUSDT listed later
- fetched at (UTC): 2026-08-16

## Configuration

- strategy: the preregistered daily-rebalance rule in `preregistration.md`
- timeframe: 15m bars, daily 00:00 UTC rebalance
- stake/leverage: 3 legs x 100 USDT, 1x, no margin, no stoploss
- cost model: `costs/COST_MODEL_BASELINE.md` crypto defaults — 15 bps per
  side; stress 30 bps per side + 1 bps funding buffer

## Execution

- command line: `./scripts/gmaq-research-crosssection --rule xts_momentum`
- host and runtime duration: local macOS, seconds

## Outputs

| artifact | sha256 |
|---|---|
| `results.json` | computed at commit `ce6429d091b98a5cf56a75f74fcc49ec77cb4c17` |
| `verdict.md` | this study |

- result metrics summary: train 399 trades
  9.6%; OOS 183 trades
  28.0% (Sharpe 2.2935);
  OOS stress 22.2%;
  benchmark basket 217.1% gross.
  Single-symbol dominance: AKEUSDT (81x range, +3151% OOS bench leg) drives most of the OOS P&L.

## Conclusion

- verdict: PASS (by the predeclared rule)
- evidence strength: WEAK — universe is today's top-15 by volume
  (survivorship/selection bias upward) and OOS P&L is dominated by one
  parabolic symbol. Not an investable claim; see `verdict.md`.
- verified vs unverified: verified deterministic no-lookahead execution and
  data pins; unverified taker fee placeholder, funding carry simplification,
  per-symbol corporate actions.

## Follow-ups

- PASS by rule; promotion requires a point-in-time universe study
  (universe reconstructed as of each rebalance date) before any stronger
  claim.
