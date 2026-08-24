# GMAQ Research Tier System v1

Status: `LOCAL_PROPOSAL_UNTIL_MERGED`
Version: `GMAQ_RESEARCH_TIER_V1`

## Canonical reconciliation

At this proposal's reconciliation point, GitHub `main` at
`83095b3a0ae575a29fde4bb538f5e346804e91a9` is canonical truth:

- Research Protocol: `GMAQ_RESEARCH_PROTOCOL_V2`, active.
- Research Pipeline: no separately versioned canonical Pipeline; the active
  process is the flow in Protocol v2. Pipeline v3 is a local proposal, not
  canonical.
- Research Roadmap: no versioned Roadmap is canonical. Roadmap v1 is a local
  proposal, not canonical.
- Research Tier: no Tier standard exists on canonical main. Tier v1 is this
  local proposal until merged.

Obsidian is context and memory; this branch is a proposal until merged. This
document is additive and compatible with canonical Protocol v2. It does not
claim that Protocol v3 or another local proposal is active, and it authorizes
neither runtime, Strategy, Dry-run, credentials, orders, tiny-live, nor live
trading. It does not lower or replace PIT, lookahead, lifecycle, accounting,
gold-sample, independent-review, freeze, or one-shot formal-run requirements.

## Required sequence

`Idea → Tier 1 Exploration → Candidate Review → Data Admission → Tier 2 Confirmation → Freeze → Formal Run → Tier 3 Production Candidate → Live`

Each arrow is a separate decision. A failed or data-blocked stage stops the
sequence; it never justifies changing parameters, data contracts, or standards.

## Tier 1 — Exploration Research

**Purpose:** discover, screen, and directionally test a mechanism. Tier 1 may
use public data, limited coverage, survivor-biased data, or an
exploration-only dataset. Its record must state `EXPLORATION_ONLY = TRUE` and
document `DATA_LIMITATIONS`, `PIT_LIMITATIONS`, and `KNOWN_BIAS`.

Its only output class is `EXPLORATION_RESULT`; it must never be called an
Alpha. Tier 1 may use already seen 2021–2023 data only for low-cost exploration.
It must not enter Strategy, Dry-run, or Live, and a positive-looking result
cannot jump to Tier 3 or directly to Strategy.

## Tier 2 — Confirmation Research

**Purpose:** decide whether a mechanism is worth becoming a production
candidate. Tier 2 requires credible PIT and lifecycle evidence, an explicit
universe, a Gold Sample, Independent Review, and Holdout. Limited coverage can
remain only if its coverage and limits are recorded.

Tier 2 must declare prior Tier 1 sources, windows, universe, dataset family,
and sample IDs, then declare the corresponding confirmation identities and its
evidence basis. A separate dataset family is not mechanically required.
Information independence is sufficient only through a genuinely new time
window or actually distinct source/sample IDs. A renamed hypothesis, run, or
file, a re-split seen history, an overlapping window, or a relabeled holdout is
not independent confirmation.

`GMAQ_2023` was consumed by EXPL-017 FORMAL-003. It is not an untouched,
final, or independent project-level holdout and cannot support a temporal
independence claim. If independence relative to discovery cannot be proven,
`CONFIRMED_CANDIDATE` is forbidden; the only fail-closed outcome is
`CONFIRMATION_BLOCKED`, `NEXT_STAGE = STOP`, and
`BLOCK_REASON = ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED`. Both a completed
formal `FAIL` and `CONFIRMED_CANDIDATE` require independent confirmation.

Tier 2 never authorizes direct live trading. A `CONFIRMED_CANDIDATE` proceeds
to Freeze; a completed `FAIL` or blocked confirmation stops.

## Tier 3 — Production Candidate

Tier 3 requires complete PIT, lifecycle, universe, costs, execution
assumptions, slippage, concentration, and robustness evidence. Before any
real-trading consideration it additionally requires separate Strategy,
Dry-run, Reliability, and Risk reviews. Only Tier 3 may set
`READY_FOR_TINY_LIVE = TRUE`, after every listed requirement and review is
complete. That is still not order authority.

## Program-level history and data routes

One-shot formal evaluation is not program-level statistical independence. Each
formal confirmation must be counted in the thin
[program history](GMAQ_PROGRAM_HISTORY_V1.json): failures remain cumulative,
consumed holdout windows remain visible, and a later PASS after more tests needs
new independent confirmation evidence. Tier records declare their `PROGRAM_ID`;
a formal `FAIL` and every Tier 3 record must declare that the formal hypothesis
has been counted. This is a record and promotion stop, not a Bonferroni/FDR
platform or statistical-governance service.

- **Route A — Existing Data Exploration** is tried first and remains Tier 1
  only until the relevant admission and independent confirmation are proven.
- **Route B — External Data Acquisition feasibility** may begin only where a
  valuable mechanism has a defined data blocker. It is proof-of-fit review,
  never authorization to purchase data.

Every paid-data proposal requires the completed
[Vendor Proof-of-Fit template](VENDOR_PROOF_OF_FIT_TEMPLATE.md) before any
purchase decision: fields, coverage, timestamps, PIT/availability semantics,
revisions, cost, expected blocker removal, decision, and evidence. "Buy first,
research later" is prohibited.

## Static checks

Use [Research Tier Record Template](RESEARCH_TIER_RECORD_TEMPLATE.md) for a
tier decision and validate it with:

```text
python research/process/research_tier.py path/to/tier-record.json
python research/process/research_tier.py --program-history research/process/GMAQ_PROGRAM_HISTORY_V1.json
```

The checker verifies declared structure and fail-closed consistency only; the
evidence must still be opened and independently reviewed. See the additive
[Tier v1 Classification Register](RESEARCH_TIER_CLASSIFICATION_REGISTER_V1.md).
