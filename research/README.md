# Research

This directory owns the GMAQ strategy research loop. It does not own the
active runtime, and it does not authorize live trading.

## Loop

The cross-study operating rules are frozen in
`process/GMAQ_RESEARCH_PROTOCOL_V2.md`. EXPL-017's completed negative result
and its process review are documented under `process/`; they do not authorize
a replacement experiment.

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
- Every new formal run must consume a Data Layer V1 `VERIFIED` curated
  snapshot and bind its dataset ID, snapshot-manifest SHA, and input-file
  SHAs. A research runner must not fetch exchange data directly. Missing data
  may be added only through the existing raw -> validated -> curated V1 flow.
- Latest formal study: `study-2026-08-20-btceth-spot-perp-carry` consumed its
  separately bound VERIFIED Data Layer V1 curated dataset and is `REJECT`:
  OOS -0.18%, Sharpe -0.381, only 9 active symbol-weeks, and all cost/funding/
  delay stresses negative. The hypothesis is closed without parameter rescue.
- Next preregistered study: `study-2026-08-21-btceth-relative-value-forward`
  is `WAITING_FOR_PROSPECTIVE_WINDOW`. It cannot fetch or run formally before
  its frozen forward window completes and a new VERIFIED curated V1 dataset ID
  plus manifest/schema/file SHAs are bound. It is not a current PASS.

## Layout

- `preregistration/`: hypotheses locked before data work
- `data/`: provenance and timing-availability checklists
- `manifest/`: per-run manifests and artifact hashes
- `costs/`: conservative execution cost model baseline
- `gate/`: PASS/REJECT evaluation criteria
- `backtests/`: per-study backtest definitions and result artifacts (created
  per study, never edited after a formal run)
- `process/`: cross-study lessons, review records, and the current research
  protocol; these records do not modify historical experiment artifacts
