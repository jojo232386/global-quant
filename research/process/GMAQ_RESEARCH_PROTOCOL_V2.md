# GMAQ Research Protocol v2

Status: `ACTIVE_PROCESS_STANDARD`
Purpose: produce interpretable, reproducible market-mechanism evidence without
building a factor zoo. This protocol does not authorize runtime, Freqtrade,
Binance, credential, or order changes.

## 1. Hypothesis Review before an experiment ID

The Lead writes one economic mechanism and the reviewer independently checks:

- it is not a parameter, window, cost, split, or formula variant of a failed
  experiment;
- prior failures do not already answer the same mechanism question;
- required data is actually available under an auditable contract; and
- PIT timing, execution, and forward-return definitions contain no future
  information.

Only an approved review may create a new `EXPL-*` identity. A rejected or
data-blocked proposal remains a candidate document, not an experiment.

The review decision is one of `APPROVED_FOR_EXPL_ID`, `DATA_BLOCKED`,
`REJECTED_AS_VARIANT`, or `REJECTED_AS_ALREADY_COVERED`. It records the Lead's
mechanism claim and the independent Reviewer's checks; silence is not approval.

## 2. Freeze the research contract

Separate and record `HYPOTHESIS_ID`, implementation attempt IDs, and a single
`FORMAL_RUN_ID`. Before observing formal metrics, freeze the mechanism,
universe, data identities, feature definitions, splits, horizons, costs,
portfolio mapping, risk limits, success gates, and expected failure modes.
Implementation repairs before formal execution may create a new attempt, but
may not silently alter the frozen hypothesis.

The identity sequence is strict: candidate review, then hypothesis/EXPL id,
then reviewed implementation attempt, then formal-run id and committed freeze.
A formal-run id is never minted merely because a hypothesis card exists.

## 3. Admit data and timing

Bind immutable snapshot, manifest, file, and sidecar hashes. Document source,
field definitions, time zone, publication/availability time, missing-data
handling, survivorship limitations, and exchange/contract distinctions. A
lifecycle sidecar is required whenever event handling can affect membership,
valuation, or execution. Missing funding, open-interest, or other required
fields is `DATA_UNAVAILABLE`, never an invitation to proxy them silently.

## 4. Validate the consumer before formal execution

Run a horizon preflight that proves every signal time, execution time, and
forward endpoint belongs to its declared split and uses the exact permitted
endpoint. Test that exclusions do not substitute terminal values or alter
unrelated NAV, holdings, cost, or split semantics. An independent oracle must
recompute the essential frozen logic from the bound inputs. A Reviewer grants
or denies formal admission before metrics are exposed.

## 5. Execute exactly once

After approval, create a canonical run claim before metrics are computed and
write exactly one canonical result or failure record. No rerun, parameter
rescue, threshold change, split change, cost change, or result replacement is
permitted. A formal failure is evidence, not an operational defect to hide.

## 6. Evaluate and archive

Evaluate all frozen gates together: predictive diagnostics, portfolio return
and risk, holdout, cost stress, concentration, regime coverage, lifecycle
effects, and declared robustness checks. A partial pass cannot override a
failed required gate. Archive failed hypotheses in the Factor Graveyard with
the data identity, run identity, failed gates, counter-evidence, and
non-repeat rule.

## 7. Anti-factor-zoo constraints

Do not conduct broad scans over indicators, parameters, time windows, or
portfolio variants to find a favorable curve. Study one mechanism at a time;
record negative evidence; and require a new, mechanism-level rationale before
the next proposal. Research PASS never proves live profitability or authorizes
tiny-live trading.

Only one hypothesis may be active. Adding indicators or filters to delete the
failed observations of a prior formal run is rescue unless the Lead states a
separate causal mechanism and the Reviewer confirms it is not already covered.
No broad indicator, parameter, lookback, or holding-window scan is admissible.

## 8. Role and stop contract

- **Lead:** freezes the mechanism claim, data need, expected failure, and
  acceptance contract.
- **Executor:** may implement only the approved contract and must stop before
  changing its scientific meaning.
- **Reviewer:** independently checks variant risk, prior coverage, data/PIT
  readiness, lookahead, implementation correctness, and final classification.

Stop on unavailable required data, unresolved lifecycle identity, an invalid
horizon, missing independent oracle, failed correctness review, or a consumed
formal id. Historical formal artifacts are immutable. Runtime, credentials,
orders, and live promotion remain outside this protocol.
