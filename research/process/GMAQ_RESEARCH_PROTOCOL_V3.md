# GMAQ Research Protocol v3

Status: `ACTIVE_PROCESS_STANDARD`
Supersedes v2 for future work only. [Protocol v2](GMAQ_RESEARCH_PROTOCOL_V2.md),
EXPL-017 artifacts, and the Factor Graveyard remain historical records and are
not rewritten by this protocol. This document does not authorize runtime,
Strategy, Freqtrade, credentials, orders, tiny-live, or any live trading.

Future work is additionally classified by
[GMAQ Research Tier System v1](GMAQ_RESEARCH_TIER_V1.md). The tier system is
additive: it separates exploration evidence from confirmation and production
candidate evidence; it does not relax this protocol's PIT, lookahead,
lifecycle, accounting, independent-review, or formal-run requirements.

## Audit of the current flow

**CURRENT_FLOW:** Candidate Review → independent Hypothesis Review → Data
Admission → Gold Sample → reviewed Implementation attempt(s) → pre-metric
Freeze → independent consumer/horizon review → one Formal Run →
evaluate/archive. A candidate review never creates an EXPL; a hypothesis
failure remains an honest final research outcome.

**IDENTIFIED_BOTTLENECKS:** EXPL-017 showed that lifecycle semantics, horizon
containment, consumer binding, and accounting definitions can be discovered
late, after substantial implementation work. An experiment also needs a short,
uniform identity record and a factor-level diagnostic before a costly formal
consumer is frozen.

**DUPLICATED_WORK:** repeatedly reconstructing code/data/config/result
identity, re-explaining diagnostic definitions, and discovering the same
readiness omissions in separate implementation attempts creates engineering
ceremony without strengthening the mechanism claim.

**MISSING_CHECKS:** there was no reusable exact-key metadata emitter, no
standalone diagnostic contract for IC/rank-IC/quantile/turnover/cost, and no
single fail-closed readiness map covering data, PIT, lifecycle, gold sample,
consumer, horizon, accounting, and reporting before a new formal-run ID.

## Minimal v3 additions

### 1. Identity and recorder record

Use [`research_run_metadata.py`](research_run_metadata.py) only after the
explicit dataset, config, and result files exist. It emits exactly
`HYPOTHESIS_ID`, `IMPLEMENTATION_ID`, `FORMAL_RUN_ID`, `DATASET_SHA`,
`CODE_SHA`, `CONFIG_SHA`, and `RESULT_SHA`; file inputs are SHA-256 hashed and
`CODE_SHA` is the resolved Git `HEAD` only when the target worktree, including
untracked files, is clean. Missing, linked, malformed, dirty, or unresolvable
inputs fail closed. The record is provenance, not a formal admission, verdict,
or run creator.

### 2. Independent diagnostic before formalisation

Use [`factor_diagnostic.py`](factor_diagnostic.py) with already PIT-safe input
observations only. It reports cross-sectional IC, Rank IC, quantile spread,
two-leg target turnover, and transparent cost sensitivity. It neither checks
PIT provenance nor supplies a formal verdict, consumer, backtest, or live
decision. Its input/turnover/cost definitions are in the module docstring so
diagnostic results cannot silently use incompatible accounting conventions.

### 3. Formal readiness before a formal-run ID

Before a new `FORMAL_RUN_ID` is minted, a reviewer must make every exact
boolean in [`FORMAL_READINESS_TEMPLATE.json`](FORMAL_READINESS_TEMPLATE.json)
true and pass it through [`formal_readiness.py`](formal_readiness.py):
`DATASET_READY`, `PIT_READY`, `LIFECYCLE_READY`, `GOLD_SAMPLE_READY`,
`CONSUMER_READY`, `HORIZON_READY`, `ACCOUNTING_READY`, `REPORT_READY`.
Missing, non-boolean, false, or extra keys block. The checker is intentionally
not an approval engine and creates no identity or formal run.

### 4. Candidate review before data work

Every new economic mechanism begins from
[Candidate Review Template v3](CANDIDATE_REVIEW_TEMPLATE_V3.md), with exact
fields `HYPOTHESIS_ID`, `MECHANISM`, `WHY_EXISTS`,
`DIFFERENCE_FROM_FAILED_WORK`, `REQUIRED_DATA`, `PIT_REQUIREMENTS`, and
`EXPECTED_FAILURE`. Parameter, window, cost, split, and formula variants are
not new Alphas.

## Adapted ideas, not adopted frameworks

These are small protocol concepts only: no framework, dependency, copied code,
database, workflow engine, or new research platform is introduced.

- Qlib: separate experiment identity, recorder-style provenance, and a
  predeclared rolling evaluation plan where relevant ([Recorder docs](https://qlib.readthedocs.io/en/stable/component/recorder.html)).
- Alphalens: independent factor IC, Rank IC, quantile, and turnover diagnostics
  ([project documentation](https://github.com/quantopian/alphalens)).
- Freqtrade: explicit lookahead and recursive-stability checks before trusting
  a signal ([lookahead analysis](https://docs.freqtrade.io/en/stable/lookahead-analysis/),
  [recursive analysis](https://docs.freqtrade.io/en/stable/recursive-analysis/)).
- VectorBT: fast, bounded exploration before expensive formal work, without
  replacing the frozen formal consumer ([project site](https://vectorbt.dev/)).

## Standard remains high; ceremony is reduced

Data provenance, PIT timing, lifecycle handling, gold samples, independent
consumer/oracle, horizon containment, accounting, one-shot formal execution,
and negative-result archival remain mandatory. The four reusable additions
move routine identity/readiness/diagnostic checks earlier without weakening
independent review or creating a new platform.

## Next Alpha preparation and stop

See [Next Alpha Candidate Review v3](NEXT_ALPHA_CANDIDATE_REVIEW_V3.md). Its
independent review classified `HYP-QUOTE-VOLUME-SHARE-MIGRATION-001` as
`DATA_BLOCKED`: Price V1's survivor bias plus the missing unbiased historical
contract master contaminates its volume-share denominator, and Lifecycle V1 is
termination-only. `SELECTED_NEXT_HYPOTHESIS=NONE`; no EXPL is created. The next
action is STOP pending separate authorization for a bounded PIT-denominator and
lifecycle data-feasibility proof before any Data Admission. The future sequence
remains independent Hypothesis Review → Data Admission → Gold Sample →
Implementation → Formal. No result here authorizes live trading.
