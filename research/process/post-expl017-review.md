# Post-EXPL-017 Research Process Review

Status: `PROCESS_REVIEW_COMPLETE`
Scope: workflow learning only. No EXPL-017 formula, split, cost, lifecycle,
formal artifact, or result is changed here.

## Earlier process gaps

- **Pre-formal defects:** early implementation attempts exposed lookahead in
  terminal handling, incorrect incumbent-turnover/NAV semantics, and
  insufficiently independent validation. They were correctly reclassified as
  invalid implementation attempts, not hypothesis or market failures.
- **Lifecycle gap:** immutable Price V1 had prices and PIT membership but no
  auditable lifecycle role. A consumer could not infer a terminal event from
  a future missing bar, so the missing role caused a pre-formal data stop.
- **Horizon contract gap:** FORMAL-001 required a forward IC endpoint outside
  its own permitted data boundary. FORMAL-002 repaired the static horizon
  schedule but still could not execute because its bound implementation was
  formal-locked and lacked the complete consumer.
- **Missing formal consumer:** a frozen contract without a fully bound,
  tested consumer cannot prove that IC, portfolio, cost, lifecycle, and gate
  semantics are actually consumed by the formal run. Contract text and a
  green preflight are necessary but not sufficient.

## Improvements now retained

- **Identity separation:** `HYPOTHESIS_ID`, implementation attempt, and
  `FORMAL_RUN_ID` are separate identities. Pre-formal defects close an attempt
  rather than silently rewriting a hypothesis result.
- **Lifecycle sidecar:** lifecycle data is a versioned, hashed sidecar bound
  to the formal data identity and consumed through an explicit interface.
- **Horizon preflight:** all decision, execution, and forward-endpoint rows
  are checked for split containment and exact endpoint availability before a
  formal run can be admitted.
- **Independent oracle:** a separately defined oracle checks the frozen
  portfolio and diagnostic logic rather than relying only on the runner's own
  calculations.
- **One-shot formal run:** an approved immutable freeze, canonical claim, and
  atomic result/failure record make a formal result permanently consumptive.

## GMAQ Research Protocol v2

Future work follows `GMAQ_RESEARCH_PROTOCOL_V2.md`: one hypothesis at a time,
pre-result review, PIT/data contracts, independent verification, one formal
run, and honest archival of either positive or negative evidence. The protocol
is designed to prevent repeated process defects, not to accelerate discovery
of a favorable curve.
