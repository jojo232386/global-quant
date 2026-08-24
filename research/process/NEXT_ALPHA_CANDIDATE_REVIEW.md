# Next Alpha Candidate Review

Status: `HYPOTHESIS_REVIEW_COMPLETE_NO_SELECTION`
Scope: candidate mechanisms only. No candidate below has an `EXPL-*` identity,
preregistration, dataset, implementation, backtest, formal run, or approval.

## Candidate A — `CAND-COMPRESSION-RELEASE-001`

- **HYPOTHESIS_ID:** `CAND-COMPRESSION-RELEASE-001` (provisional only).
- **MECHANISM:** after a contract-specific volatility compression completes,
  the first directional price expansion may continue cross-sectionally when
  broad-market volatility is calm; high broad volatility maps to no trade,
  never to EXPL-017's reversal rule. Compression-to-release is the proposed
  causal event; momentum direction and broad volatility are measurements and
  controls around that event.
- **WHY_EXIST:** a falsifiable inventory/information-release story can produce
  delayed continuation after dormant price discovery. The claim is about a
  transition from compression to expansion, not about choosing a better
  momentum lookback or broad-volatility threshold.
- **REQUIRED_DATA:** PIT universe and Lifecycle V1 plus timestamped daily open,
  close, and quote volume sufficient to define compression and the completed
  directional expansion. A fresh admission must verify exact field coverage;
  EXPL-017's binding is evidence context, not automatic approval.
- **PIT_REQUIREMENTS:** compression, expansion, and broad state use only bars
  completed through decision close; the earliest trade is the next available
  open. State boundaries are learned from train history only, and every
  forward endpoint must stay inside its declared split.
- **EXPECTED_FAILURE_MODE:** compression events may be sparse, the release may
  reverse instead of continue, broad-state conditioning may add no marginal
  value, or turnover/cost/concentration may erase the spread.
- **DIFFERENCE_FROM_FAILED_WORK:** it does not reuse EXPL-017's multi-horizon
  rank or calm-continuation/high-volatility-reversal mapping. It also differs
  from the rejected single-symbol intraday low-vol momentum gate: the proposed
  event was framed as a PIT cross-sectional compression-to-release transition.

**Independent decision:** `REJECTED_AS_VARIANT`. The observable claim is still
calm-state price continuation after a low-volatility/breakout condition. That
question is already covered by the rejected intraday volatility-filtered
momentum and breakout studies plus EXPL-017's calm-state continuation. The
inventory/information-release narrative has no separately measured variable,
and “compression completes”/“first expansion” lack a frozen ex-ante event
definition. Frequency, ranking, or event-label changes do not establish a new
mechanism and can introduce retrospective event selection.

## Candidate B — `CAND-DERIVATIVES-POSITIONING-001`

- **HYPOTHESIS_ID:** `CAND-DERIVATIVES-POSITIONING-001` (provisional only).
- **MECHANISM:** a build-up in open-interest stock combined with adverse
  funding flow and weak price confirmation identifies leveraged crowding;
  subsequent deleveraging may create a cross-sectional reversal.
- **WHY_EXIST:** funding and open interest represent derivative positioning,
  not a transformed price momentum or volatility-regime input.
- **REQUIRED_DATA:** exchange- and contract-specific funding schedules and
  rates, open interest with publication timestamps, contract specifications,
  mark/index price definitions, liquidations if used, and spot/perpetual
  execution mapping.
- **PIT_REQUIREMENTS:** freeze vendor/exchange source, historical revision
  policy, event availability time, sampling calendar, contract roll/listing
  treatment, and derivative-versus-spot clock alignment.
- **EXPECTED_FAILURE_MODE:** field revisions, exchange coverage mismatch,
  unmodeled funding settlement, or unavailable historical timestamps can make
  the contract invalid or `DATA_UNAVAILABLE`.
- **DIFFERENCE_FROM_FAILED_WORK:** failed studies ranked funding level or
  funding change without an admitted OI stock. This proposal tests the joint
  stock-flow-price state; it cannot reuse EXPL-017 Price V1 or treat OI as an
  optional extra feature.

**Independent decision:** `DATA_BLOCKED`. The mechanism is distinct, but no
implementation or EXPL
identity is permitted until the funding/open-interest contract is independently
reviewed and admitted. The 2026-08-24
[data-capability review](CAND_DERIVATIVES_POSITIONING_001_DATA_CAPABILITY_REVIEW.md)
confirmed that daily OI endpoints are feasible without exact 288-slot
completeness, but historical availability/revision and PIT-universe evidence
remain insufficient for Tier 2.

## Candidate C — `CAND-FLOAT-DILUTION-001`

- **HYPOTHESIS_ID:** `CAND-FLOAT-DILUTION-001` (provisional only).
- **MECHANISM:** a pre-announced increase in freely tradable token supply
  creates temporary selling pressure, so assets with larger PIT-known float
  dilution underperform the cross-section after the unlock.
- **WHY_EXIST:** the proposed driver is a supply shock to investable float,
  not a transformed price, volatility, funding, or momentum signal.
- **REQUIRED_DATA:** first-public unlock schedules, realized circulating and
  freely tradable supply, revision history, token/contract identity mapping,
  PIT universe, prices, and lifecycle records.
- **PIT_REQUIREMENTS:** preserve every schedule vintage and first-public time;
  later corrections or realized supply must not enter earlier decisions.
  Listing, redenomination, migration, and stale schedule handling require an
  auditable as-of mapping.
- **EXPECTED_FAILURE_MODE:** schedules may not be historically vintaged,
  announced unlocks may already be priced, realized float may differ from the
  schedule, or sparse coverage and costs may erase the cross-sectional effect.
- **DIFFERENCE_FROM_FAILED_WORK:** it tests an external supply mechanism, not
  EXPL-017's price-only momentum plus volatility-regime mechanism or the failed
  low-volatility/liquidity-weighting cards.

**Independent decision:** `DATA_BLOCKED`. No current V1 dataset proves
historical unlock vintages or investable-float availability. First-public
schedule vintages and an as-of asset/contract identity map are mandatory.

## Lead recommendation and stop condition

The Lead proposed Candidate A for review because its Price/Lifecycle fields
could be audited without new data. The independent Reviewer rejected it as a
mechanism variant and classified B/C as data-blocked.

`SELECTED_NEXT_HYPOTHESIS=NONE`. Hypothesis Review is complete, but no candidate
is approved for an EXPL identity. No EXPL-018 may be created from this review.

The next action is `STOP`. A future, separately authorized task may evaluate a
new data contract for B or C; that would be data feasibility work, not an
experiment, implementation, or formal run.
