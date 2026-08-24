# Next Alpha Candidate Review v3

Status: `HYPOTHESIS_REVIEW_COMPLETE_DATA_BLOCKED_NO_EXPL_CREATED`

Scope: pre-experiment comparison of three candidate mechanisms. None has an
`EXPL-*` identity, admitted dataset, gold sample, implementation, backtest,
formal run, or approval. Price V1's existing fields are useful only where
specified below: it is `VERIFIED` but survivor-biased and exploration-only, so
the evidence does not establish formal readiness.

## Candidate 1 — `CAND-QUOTE-VOLUME-SHARE-MIGRATION-001`

- **HYPOTHESIS_ID:** `CAND-QUOTE-VOLUME-SHARE-MIGRATION-001` (provisional).
- **MECHANISM:** attention/liquidity migration. A PIT-observed rise or fall in
  an asset's share of eligible-universe quote volume may precede relative
  future returns as attention and executable liquidity migrate across assets.
  This is explicitly not price confirmation.
- **WHY_EXISTS:** capital, information attention, and venue activity can shift
  across otherwise comparable eligible contracts before price leadership is
  fully incorporated. The claim fails if volume-share changes contain no
  incremental relation after contemporaneous market information is controlled.
- **DIFFERENCE_FROM_FAILED_WORK:** EXPL-017 tested multi-horizon price ranking
  with a volatility-state mapping. This card tests cross-sectional share
  migration measured from quote volume, not a transformed price trend, a
  volume eligibility ranking, or a new price/volatility filter.
- **REQUIRED_DATA:** daily `quote_volume`, open/close, an unbiased historical
  contract master, and PIT lifecycle/listing identities sufficient to establish
  the eligible-universe denominator. Price V1 has the first two fields and a
  survivor-biased exploration-only universe, but it does not supply the
  required unbiased historical contract master.
- **PIT_REQUIREMENTS:** calculate each volume share using only the eligible
  PIT universe and completed bars through decision close `t`; trade no earlier
  than the declared next executable time. Preserve the as-of denominator and
  membership, do not use later listings/delistings, keep endpoints inside their
  split, and treat missing required data as `DATA_UNAVAILABLE`.
- **EXPECTED_FAILURE:** share migration may be merely momentum plus volume
  confirmation, volume may react rather than lead, the result may disappear in
  the survivor-biased universe, or turnover/costs may erase a diagnostic
  spread.

**Required diagnostic design if data feasibility is separately proven:** test
whether volume-share change leads future return while controlling for
contemporaneous own price return, contemporaneous volatility, and
contemporaneous market beta. Use a predeclared out-of-sample/rolling evaluation
plan and report IC, Rank IC, quantile spread, two-leg turnover, and cost
sensitivity only as diagnostics. Without these controls it can be
indistinguishable from momentum plus volume confirmation. This is neither a
price-confirmation signal nor a formal gate.

**Independent decision:** `DATA_BLOCKED`. The mechanism is distinct, but Price
V1's survivor bias and lack of an unbiased historical contract master
contaminate the volume-share denominator; Lifecycle V1 is termination-only and
does not establish complete PIT listing/onboard membership. No diagnostic,
data admission, Gold Sample, implementation, backtest, formal run, or EXPL is
permitted from these fields. **Review matrix:** mechanism independence `YES`;
existing-data availability `NO`; PIT risk `HIGH`; research cost `UNVERIFIED`
pending a bounded data-feasibility proof. **Recommended next action:** `STOP`.

## Candidate 2 — `CAND-PERPETUAL-LISTING-MATURATION-001`

- **HYPOTHESIS_ID:** `CAND-PERPETUAL-LISTING-MATURATION-001` (provisional).
- **MECHANISM:** a perpetual contract's liquidity, participation, and pricing
  behavior may change systematically as it matures after listing, producing a
  time-since-onboard effect distinct from cross-sectional price momentum.
- **WHY_EXISTS:** early contract lifecycle can have fragmented liquidity,
  market-maker onboarding, and changing access; a valid mechanism requires
  historical listing/onboard evidence rather than inferring age from a later
  data snapshot.
- **DIFFERENCE_FROM_FAILED_WORK:** it is a contract-lifecycle/onboarding
  mechanism, not EXPL-017's price-only rank/state mechanism.
- **REQUIRED_DATA:** PIT historical listing/onboard timestamps, contract
  specification and identity changes, lifecycle statuses, and prices/volume.
  Current Lifecycle V1 is exception-only termination evidence, not a complete
  PIT listing/onboard history.
- **PIT_REQUIREMENTS:** preserve first-public listing/onboard vintages,
  symbol migrations, re-listings, contract changes, availability time, and
  decision/execution clocks; later-known listing dates may not be backfilled.
- **EXPECTED_FAILURE:** apparent maturity can be survivorship, selection, or
  concurrent price/liquidity effects; required historical onboarding evidence
  may be unavailable.

**Review matrix:** mechanism independence `YES`; existing-data availability
`NO`; PIT risk `HIGH`; research cost `MEDIUM_TO_HIGH` because a new historical
lifecycle/onboard contract would be needed. **Recommended next pre-experiment
stage:** `STOP_DATA_UNAVAILABLE` until a separate data-feasibility task admits
the needed historical contract.

## Candidate 3 — `CAND-ORDER-BOOK-RESILIENCE-001`

- **HYPOTHESIS_ID:** `CAND-ORDER-BOOK-RESILIENCE-001` (provisional).
- **MECHANISM:** cross-sectional resilience of displayed depth after shocks may
  encode temporary liquidity replenishment or withdrawal and precede returns
  or executable costs.
- **WHY_EXISTS:** order-book replenishment is a microstructure mechanism, not
  a price-only transformation.
- **DIFFERENCE_FROM_FAILED_WORK:** it does not reuse EXPL-017's price ranking,
  volatility state, or quote-volume eligibility rule.
- **REQUIRED_DATA:** timestamped historical order-book snapshots/deltas,
  venue/contract identity, sequence integrity, trade clock, and executable
  spread/depth fields. No such admitted historical dataset is present; current
  depth tooling is runtime/preflight material and outside research scope.
- **PIT_REQUIREMENTS:** exchange timestamps, capture latency, sequence gaps,
  revision policy, listing/lifecycle state, and decision-to-execution alignment
  must be preserved.
- **EXPECTED_FAILURE:** sparse/degraded books, venue changes, snapshot latency,
  and costs can dominate; a data reconstruction could itself create lookahead.

**Review matrix:** mechanism independence `YES`; existing-data availability
`NO`; PIT risk `HIGH`; research cost `HIGH` and needs infrastructure outside
this task. **Recommended next pre-experiment stage:** `STOP_DATA_UNAVAILABLE`.

## Selection and stop condition

`SOURCE_CANDIDATE_ID=CAND-QUOTE-VOLUME-SHARE-MIGRATION-001`.

`REVIEWED_HYPOTHESIS_ID=HYP-QUOTE-VOLUME-SHARE-MIGRATION-001`.

`HYPOTHESIS_REVIEW_DECISION=DATA_BLOCKED`.

`SELECTED_NEXT_HYPOTHESIS=NONE`.

`EXPL_CREATED=NO`.

`NEXT_ACTION=STOP_SEPARATE_AUTHORIZATION_REQUIRED_FOR_BOUNDED_PIT_DENOMINATOR_AND_LIFECYCLE_DATA_FEASIBILITY_PROOF`.
The reviewed identity records the independent decision, not an approved
experiment. Only a separately authorized bounded PIT-denominator and lifecycle
data-feasibility proof may occur before Data Admission. The future order, if
that proof succeeds, remains independent Hypothesis Review → Data Admission →
Gold Sample → Implementation → Formal. Stop now; no experiment is started.
