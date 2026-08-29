# GMAQ engineering case study

## Executive summary

GMAQ started as a global and crypto quantitative trading project. The work produced a strong validation stack and no promotable Alpha. The team closed the strategy-production track, preserved the negative evidence, and retained the repository as an auditable validation archive.

The validator caught results that ordinary backtest review would have accepted. GMAQ traced each claim to data, code, cost assumptions, and a frozen decision rule. When a result failed, the team kept the failure and stopped the lane.

## The problem

Researchers can introduce errors long before an order reaches an exchange. A current-symbol universe can leak survivors into old history. An engine correction can change a result after the team has seen it. Thin edges can disappear under realistic costs. Engineers use a green test suite to prove software behavior; it says nothing about market edge.

GMAQ encoded those distinctions as machine-checked contracts.

## The system

### Data and time integrity

Data Layer V1 records immutable snapshots, schemas, source-file hashes, stage, and quality verdict. PIT instrument records and lifecycle evidence prevent a current market list from posing as a historical tradable universe.

### Research governance

Each formal study binds a preregistration, dataset identity, implementation commit, cost model, run manifest, and PASS/REJECT rule. The project separates exploration from formal evidence and records program-level trial history. Failed hypotheses enter the Factor Graveyard; the team does not tune them back to life.

### Runtime and admission

The Freqtrade runtime defaults to credential-free dry-run. GMAQ adds entry gating, reconciliation, audit continuity, reliability evidence, and a read-only Control Room. The live-admission module evaluates evidence but cannot arm or submit orders. Missing broker truth, identity drift, incomplete soak evidence, or a dirty worktree returns `BLOCKED`.

## Findings

| Finding | Decision impact |
| --- | --- |
| Present-day cross-sectional universe produced weak historical passes | PIT reconstruction revoked the promotion case |
| Candidate data contract could not prove historical first-availability and revision lineage | Research stopped at `DATA_BLOCKED` |
| EXPL-017 failed final holdout and robustness gates | Recorded `HYPOTHESIS_FAIL`; no rerun or parameter rescue |
| OSS admission changed native Freqtrade strategy semantics before performance testing | Closed the program and moved future screening to native frameworks |
| Runtime safety evidence did not supply strategy edge | Kept live readiness and order submission blocked |

## Closeout scorecard

| Objective | Result |
| --- | --- |
| Validation Archive v1 release contract | `ARCHIVE_RELEASED` |
| Critical fail-closed controls | `109 passed` |
| Reproducible validation infrastructure | Delivered |
| Data lineage and PIT controls | Delivered |
| Fail-closed dry-run and evidence dashboard | Delivered |
| Low-cost Alpha production | Missed |
| Promoted Alpha | `0` |
| Tiny-live readiness | `NO` |
| Real orders | `0` |

The engineering stack succeeded at rejecting weak evidence. The frozen archive release reproduces that result through its [acceptance contract](ARCHIVE_RELEASE.md) and [machine-readable PASS artifact](../results/gmaq-validation-archive-v1.json). The original strategy-production goal missed its target. GMAQ records both facts.

## Lessons applied to the successor

The successor architecture starts with native-framework screening. A Freqtrade strategy runs first in Freqtrade with its original timeframe, state machine, callbacks, ROI, and stop logic. Cheap failures stop there. Candidates that survive cost and lookahead checks can enter stricter PIT, holdout, and independent confirmation.

This sequence protects research integrity without spending confirmation-level effort on every public strategy.

## Evidence index

- [Evaluation gate](../research/gate/EVALUATION_GATE.md)
- [Research Protocol V2](../research/process/GMAQ_RESEARCH_PROTOCOL_V2.md)
- [Research tiers](../research/process/GMAQ_RESEARCH_TIER_V1.md)
- [PIT data foundation](../research/process/PIT_DATA_FOUNDATION_V1.md)
- [Factor Graveyard](../research/exploration/factor-graveyard.md)
- [EXPL-017 frozen result](../research/exploration/expl-017-formal-003-result.json)
- [OSS replication result](../research/exploration/oss-strategy-replication-001-result.json)
- [Live-readiness blockers](../configs/LIVE_READINESS.md)
- [Contract suite](../tests/)

## Archive boundary

The repository remains frozen. Documentation may clarify the evidence; it must not alter historical results, reopen failed hypotheses, or imply profitability. Any future strategy work belongs in a separate project with a new contract.
