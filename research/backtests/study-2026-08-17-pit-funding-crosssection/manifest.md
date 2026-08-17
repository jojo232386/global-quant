# Run Manifest: study-2026-08-17-pit-funding-crosssection

> Completed after the run. This manifest does not authorize live trading.

## Identity

- run id: `run-2026-08-17-funding_crosssection-pit-1`
- study id: `study-2026-08-17-pit-funding-crosssection`
- preregistration: `preregistration.md` (locked before fetch, commit `b3a0a5e`)
- data checklist: `data-checklist.md`
- started (UTC): 2026-08-17T07:20:00Z

## Code and environment

- repository sha256 (HEAD at run): `fa3b85dd92925ebcbf711afe7270c132fcfad4e3`
- branch / worktree: `continuation/p0p1-gaps`
- python: 3.12 system + .venv for auth tools; this study is stdlib-only
- random seeds: none

## Data

- pit manifest sha256 `4e2a8a7a3eebfa0011657bce0fe3e77b138db1369126a990b135bde31f8ee48f`; universes `47bdcd6e646ce3c92a6ca870378b4266c88abe42bac600c11af04aa0fb59f785`
- 100 candidates -> 197 daily universes -> 77 symbols

## Configuration

- rule: funding_crosssection; 3 legs x 100 USDT; costs 15 bps/side; stress 30 bps +
  1 bps buffer; split 2026-06-16

## Execution

- command: `./scripts/gmaq-research-pit --rule funding_crosssection`

## Outputs

- `results.json` (commit `fa3b85dd92925ebcbf711afe7270c132fcfad4e3`), `verdict.md`

- metrics: train 312 trades 10.3%;
  OOS 183 trades -34.9%
  (Sharpe -5.897); OOS stress -40.5%;
  PIT benchmark -8.1% gross.

## Conclusion

- verdict: REJECT (predeclared rule: OOS Sharpe -5.897 < 0.5)
- bias comparison: the biased today-top-15 run (study-2026-08-16-multi-funding-crosssection) reported OOS
  +13.1% PASS; under point-in-time reconstruction this rule
  returns -34.9% OOS -> the biased PASS is explained
  by selection bias, as the preregistration predicted.

## Follow-ups

- study closed with REJECT; both cross-sectional hypotheses are falsified
  under point-in-time universes. Recorded as failure evidence.
