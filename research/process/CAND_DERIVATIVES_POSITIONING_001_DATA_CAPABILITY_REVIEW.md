# CAND-DERIVATIVES-POSITIONING-001 data capability review

Status: `BEST_ZERO_COST_CANDIDATE_B_PATH_EXHAUSTED`

Review date: `2026-08-24 UTC`

Canonical baseline: `3ed5b68b4610dbea6181f33f534514a919392095`

Research identity: candidate only; no `EXPL-*`, preregistration, run, result, or
Alpha identity is created by this review.

## Decision

```text
CANDIDATE_B_TIER1=MECHANISM_VALID
CANDIDATE_B_TIER2=DATA_ADMISSION_BLOCKED
MINIMAL_ADAPTER=DO_NOT_BUILD
DATA_CAPABILITY_UNLOCKED=NO
PIVOT_TO_PIT_UNIVERSE=YES
```

The official public archives are useful for exploration and establish that the
old exact-288-observations rule was too strict for this daily mechanism. They do
not establish the historical first-availability and revision evidence required
for Tier 2. A new adapter would repeat already completed byte/schema/coverage
work without removing that blocker, so total implementation, maintenance,
dependency, and semantic risk is lower with no new code.

## Mechanism-derived minimum data contract

The candidate remains the joint state recorded in
[Next Alpha Candidate Review](NEXT_ALPHA_CANDIDATE_REVIEW.md): OI stock, adverse
funding flow and weak price confirmation, followed by a proposed three-day
cross-sectional reversal. Removing OI or funding changes the mechanism. The
public source provides realized rates, not literal cash flow; that accounting
term remains unresolved rather than being silently redefined.

| Input | Minimum evidence | Timing and failure rule |
| --- | --- | --- |
| Open interest | Venue, perpetual contract identity, contract-unit `sum_open_interest`, observation time, units/specification lineage | One positive `23:55 UTC` endpoint for every day `t-30..t` inclusive. Exact 288 intraday slots are diagnostic only. A missing/non-positive scheduled endpoint fails closed; no interpolation, carry-forward, symbol drop, or replacement. |
| Funding | Actual settled rate, settlement event time, interval/schedule, venue and contract identity | Use only completed settlement events available by the decision cutoff. A rate series is not literal cash flow without position/notional, payer direction, multiplier, and settlement accounting. |
| Price | Contract-matched completed UTC daily bars | Weak confirmation uses only completed bars; entry and forward returns are separate and later. |
| Universe/lifecycle | Historical contract membership, listing/termination effective time, publication/availability time, contract specifications | Must be known as of each decision. Today's symbol list or later missing bars cannot reconstruct the historical domain. |
| Provenance/PIT | Source identity, retrieval time, immutable checksum, historical first-availability, revision/vintage lineage | Event time and current checksum are necessary but not sufficient for Tier 2. |

The formation window needs 31 daily endpoints, not the highest available
frequency. This corrects an over-strict data requirement; it does not relax PIT,
lookahead, lifecycle, lineage, or accounting.

## Public proof-of-fit evidence

A read-only scan of the local official Binance USD-M archive at
`/Users/ASUS/Desktop/gmaq-data/acquisition/pre2024-usdm-funding-oi-v1`
established:

- 21,939/21,939 manifested OI daily ZIPs parse with the official metrics schema.
- 21,748 have exactly 288 rows; 191 do not.
- 21,908 have a positive exact `23:55` contract-unit OI endpoint.
- One endpoint is missing (`ETCUSDT`, `2023-02-07`) and 30 endpoints are
  non-positive (all `2022-03-07`); these 31 dates remain fail-closed defects.
- 819/819 required funding symbol-month archives exist with the expected schema,
  ordered event time, actual rate, and positive source interval.
- The source-manifest SHA-256 is
  `0a7f9d73cb7619c8660940a8c0d388d3cbb6be4a438dc74d580f7fea70349d5f`;
  the coverage-summary SHA-256 is
  `ec0fdf4ed3927dce93bb3dfb374dee16e237e36bbc7099064fff7039e8ad9d9d`.

These facts prove present-day schema, byte lineage, and endpoint coverage. They
do not prove which bytes were visible before a historical trade. The OI manifest
records symbol, date, path, URL, and current SHA-256, but no historical
publication, receipt, vintage, or revision fields. Local file timestamps are
2026 receipt timestamps. Therefore `observation_time + 5 minutes`, a current
official checksum, filename date, ZIP metadata, or today's `Last-Modified` must
not be labeled historical availability or `pit-safe`.

## Reuse decision

| Capability | Source | Decision | Reason |
| --- | --- | --- | --- |
| Historical OI/funding bytes | [Binance Data Vision](https://data.binance.vision/) | `THIN_ADAPTER_IF_REOPENED` | Official public source and current checksums; does not prove historical publication/revision lineage. |
| Archive conventions/checksums | [binance-public-data](https://github.com/binance/binance-public-data) | `REFERENCE_ONLY` | Useful official format/update documentation; no project code is vendored and no dependency is added. |
| Current OI API/official connector | Binance USD-M REST | `REJECT_FOR_HISTORY` | The historical OI endpoint has a rolling recent window and cannot recover the required historical availability record. |
| Prior local Funding/OI implementation | noncanonical `660764d` worktree | `REJECT_UNCHANGED` | Its file/ZIP/checksum safety ideas are reusable, but exact-288 and inferred `pit-safe` claims are invalid. |
| Tardis | vendor free sample/metadata | `PROOF_OF_FIT_ONLY` | Local timestamps are promising; the free first-day samples cannot prove the required multi-year symbol coverage and revision/vintage contract. |

If a qualifying source later removes the PIT blocker, reuse may be limited to
regular-file/root-containment checks, bounded one-member ZIP parsing, UTF-8 and
exact-header checks, finite-decimal validation, identity checks, conflicting
duplicate rejection, official-current-checksum validation, atomic replacement,
deterministic manifests, and fail-closed missing objects. Do not reuse exact-288
as an endpoint invariant or any `pit-safe`/historical-availability inference.

## Vendor proof-of-fit — Tardis

This section instantiates the existing
[Vendor Proof-of-Fit template](VENDOR_PROOF_OF_FIT_TEMPLATE.md). It is not a
purchase authorization.

- **CANDIDATE_ID:** `CAND-DERIVATIVES-POSITIONING-001`
- **MECHANISM_VALUE_EVIDENCE:** `TIER_1_MECHANISM_VALID`; no Alpha or confirmed
  market result.
- **EXACT_DATA_BLOCKER:** contract-specific historical arrival/first-availability
  timestamps, revision/vintage lineage, and a historical instrument master.
- **VENDOR / PRODUCT / VERSION:** `Tardis.dev Binance USDT Futures historical
  derivative_ticker / metadata; product version UNVERIFIED`.
- **REQUIRED_FIELDS:** symbol, exchange timestamp, local arrival timestamp, OI,
  actual funding rate and interval, contract specifications, listing/termination
  history, and correction/revision identity.
- **COVERAGE:** vendor documentation says Binance USDT Futures data is available
  from 2019-11-17 and `derivative_ticker` OI from 2020-05-13. Exact required
  symbol/date/gap coverage is `UNVERIFIED`.
- **TIMESTAMPS:** documented exchange/native timestamp plus local collection
  timestamp. Historical publication of downloadable normalized files and the
  relationship between local time and later corrections remain `UNVERIFIED`.
- **PIT_AND_AVAILABILITY_SEMANTICS:** promising for collected messages, but not
  admitted without a sample-bound audit of local timestamps and replay behavior.
- **REVISIONS_AND_BACKFILL_POLICY:** `UNVERIFIED` for the required records.
- **COST:** public pricing observed on 2026-08-24 starts at USD 350/month for an
  Academic perpetuals plan, billed quarterly or yearly; Academic/Solo/Pro yearly
  access is limited to four historical years. Business is listed at USD
  3,000/month and all available history with yearly billing. Exact quote,
  eligibility, taxes, license, and required plan are `UNVERIFIED`.
- **EXPECTED_BLOCKER_REMOVED:** historical per-message collection time could
  remove the arrival-time blocker; only verified instrument metadata and
  revision handling could remove the remaining universe/vintage blockers.
- **EVIDENCE:** [Binance USDT Futures coverage](https://docs.tardis.dev/historical-data-details/binance-futures),
  [data type timestamp semantics](https://docs.tardis.dev/tardis-machine/data-types),
  [billing and historical access](https://docs.tardis.dev/faq/billing-and-subscriptions),
  and [current pricing](https://tardis.dev/), accessed 2026-08-24.
- **DECISION:** `NOT_APPROVED`; free samples prove schema shape only, and the
  lowest published tiers do not establish the full required 2021-2023 window as
  of the review date. Do not buy before a contract-specific quote and proof-of-fit.

```text
VENDOR_PROOF_OF_FIT=PARTIAL_SCHEMA_ONLY
PAID_QUOTE_REQUIRED=YES
PURCHASE_AUTHORIZED=NO
PURCHASE_RECOMMENDATION=DONT_BUY_NOW
```

## Stop and reopen condition

Candidate B remains `Tier 1 Mechanism Valid / Tier 2 Data Admission Blocked`.
Do not create an adapter, dataset, `EXPL-*`, backtest, or formal run from these
archives. Reopen Data Admission only when one source supplies all of:

1. contract-specific historical first-availability/arrival evidence;
2. documented revision/vintage lineage for the tested bytes;
3. an auditable historical instrument master and contract specifications; and
4. independently reviewed coverage for the required symbols and dates.
