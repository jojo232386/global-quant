# Research

This directory owns the GMAQ strategy research loop. It does not own the
active runtime, and it does not authorize live trading.

## Loop

1. Preregister the hypothesis before looking at results:
   `preregistration/HYPOTHESIS_TEMPLATE.md`
2. Establish data provenance, timezone, and timing availability before any
   backtest: `data/DATA_AVAILABILITY_CHECKLIST.md`
3. Record every formal run in a manifest:
   `manifest/RUN_MANIFEST_TEMPLATE.md`
4. Apply the conservative execution cost model:
   `costs/COST_MODEL_BASELINE.md`
5. Score the run through the PASS/REJECT gate:
   `gate/EVALUATION_GATE.md`

## Rules

- A strategy promoted from this directory must retain data provenance, timing
  integrity, conservative execution assumptions, out-of-sample evidence, and
  explicit risk limits.
- A research PASS is a research claim. It does not authorize dry-run
  promotion, live configuration, credentials, account changes, or orders.
- Research success is not live profitability. Failure evidence is recorded as
  honestly as passing evidence.
- Any strategy promoted to the active runtime must be separated from
  `LiveExecutionCanaryStrategy`, which remains a runtime canary and is
  explicitly not alpha.
- A run without a preregistration and a manifest is not evidence.

## Layout

- `preregistration/`: hypotheses locked before data work
- `data/`: provenance and timing-availability checklists
- `manifest/`: per-run manifests and artifact hashes
- `costs/`: conservative execution cost model baseline
- `gate/`: PASS/REJECT evaluation criteria
- `backtests/`: per-study backtest definitions and result artifacts (created
  per study, never edited after a formal run)
