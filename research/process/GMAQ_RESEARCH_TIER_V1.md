# GMAQ Research Tier System v1

Status: `ACTIVE_ADDITIVE_PROCESS_STANDARD`
Version: `GMAQ_RESEARCH_TIER_V1`

This standard is additive to [GMAQ Research Protocol v3](GMAQ_RESEARCH_PROTOCOL_V3.md).
It distinguishes the evidentiary strength of an exploration dataset from the
requirements for a production candidate. It does **not** lower or replace PIT,
lookahead, lifecycle, accounting, gold-sample, independent-review, freeze, or
one-shot formal-run requirements. It authorizes neither runtime, Strategy,
Dry-run, credentials, orders, tiny-live, nor live trading.

## Required sequence

Every prospective mechanism follows this sequence:

`Idea → Tier 1 Exploration → Candidate Review → Data Admission → Tier 2 Confirmation → Freeze → Formal Run → Tier 3 Production Candidate → Live`

Each arrow is a separate decision. A failed or data-blocked stage stops the
sequence; it is not a reason to alter parameters, data contracts, or standards.

## Tier 1 — Exploration Research

**Purpose:** discover, screen, and directionally test a mechanism.

Tier 1 may use public data, limited coverage, survivor-biased data, or an
exploration-only dataset. Its record must state `EXPLORATION_ONLY = TRUE` and
non-empty `DATA_LIMITATIONS`, `PIT_LIMITATIONS`, and `KNOWN_BIAS` fields.
It may support candidate screening, mechanism validation, or a directional
judgment only.

Its only output class is `EXPLORATION_RESULT`; it must never be called an
Alpha. Tier 1 must not enter Strategy, Dry-run, or Live. A positive-looking
Tier 1 result cannot jump to Tier 3 or directly to Strategy.

## Tier 2 — Confirmation Research

**Purpose:** decide whether a mechanism is worth becoming a production
candidate.

Tier 2 requires credible PIT and lifecycle evidence, an explicit universe,
a Gold Sample, Independent Review, and Holdout. Limited coverage can remain,
but the record must specify coverage and its limitations. The only results are
`CONFIRMED_CANDIDATE` and `FAIL`.

Tier 2 never authorizes direct live trading. A `CONFIRMED_CANDIDATE` proceeds
to Freeze; `FAIL` stops. Tier 2 cannot waive missing data or turn an
exploration result into production evidence.

## Tier 3 — Production Candidate

**Purpose:** prepare a confirmed mechanism for real-trading consideration.

Tier 3 requires complete PIT, lifecycle, universe, costs, execution
assumptions, slippage, concentration, and robustness evidence. Before any
real-trading consideration it additionally requires separate Strategy,
Dry-run, Reliability, and Risk reviews.

Only Tier 3 may set `READY_FOR_TINY_LIVE = TRUE`, and only after every listed
requirement and review is complete. That state is still not an order authority;
the repository's separate human and runtime admission gates remain mandatory.

## Promotion and data rules

- Tier 1 evidence must never be promoted directly to Tier 3.
- An attractive Tier 1 result must never enter Strategy, Dry-run, or Live.
- Missing or insufficient data must never lower the confirmation or production
  standard.
- **Route A — Existing Data Exploration** is always tried first and remains
  Tier 1 only until the relevant admission is proven.
- **Route B — External Data Acquisition feasibility** may begin only when a
  mechanism has value and a defined data blocker. It is a proof-of-fit review,
  never an authorization to purchase data.

Every paid-data proposal requires a completed
[Vendor Proof-of-Fit template](VENDOR_PROOF_OF_FIT_TEMPLATE.md) before any
purchase decision. It must identify fields, coverage, timestamps,
PIT/availability semantics, revisions, cost, the exact blocker expected to be
removed, decision, and supporting evidence. "Buy first, research later" is
prohibited.

## Records and static check

Use [Research Tier Record Template](RESEARCH_TIER_RECORD_TEMPLATE.md) to
record a tier decision. `research_tier.py` validates the exact required fields,
forbidden Tier 1 labels/jumps/readiness, declared Tier 1 → review → admission
→ Tier 2 → Freeze → Formal Run lineage, Tier 2 confirmation prerequisites,
and Tier 3 readiness prerequisites. Tier 3 additionally requires an explicit
`FORMAL_RUN_RESULT_STATUS = PASS`, meaning the frozen evaluation passed rather
than merely ran or received process approval. Referenced records remain evidence
to open and review: this is a static consistency check, not a substitute for
independent review or evidence verification:

```text
python research/process/research_tier.py path/to/tier-record.json
```

Templates default to fail-closed, no-live values. See the additive
[Tier v1 Classification Register](RESEARCH_TIER_CLASSIFICATION_REGISTER_V1.md)
for the historical-context classifications; it does not modify historical
experiment artifacts.
