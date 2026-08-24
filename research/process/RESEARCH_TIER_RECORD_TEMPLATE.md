# GMAQ Research Tier Record Template

Status: `TEMPLATE_FAIL_CLOSED_NO_LIVE`
Scope: local Tier v1 proposal until merged.

Create one JSON record using exactly one schema below, then validate it with
`python research/process/research_tier.py RECORD.json`. Placeholders are
deliberately invalid or no-live; the checker validates declared structure, not
the underlying evidence.

## Tier 1 Exploration

```json
{
  "RESEARCH_TIER": "TIER_1_EXPLORATION",
  "RECORD_ID": "UNASSIGNED",
  "EXPLORATION_ONLY": true,
  "OUTPUT_CLASS": "EXPLORATION_RESULT",
  "DATA_LIMITATIONS": "UNVERIFIED",
  "PIT_LIMITATIONS": "UNVERIFIED",
  "KNOWN_BIAS": "UNVERIFIED",
  "RESULT_STATUS": "MECHANISM_VALID",
  "NEXT_STAGE": "CANDIDATE_REVIEW",
  "READY_FOR_TINY_LIVE": false
}
```

## Tier 2 Confirmation

All `*_IDS` fields are non-empty, exact identifier lists. They expose prior
discovery information and current confirmation information; a renamed
hypothesis, run, or file never establishes independence. `GMAQ_2023` cannot be
used with a temporal basis because it is already consumed project-level holdout.

```json
{
  "RESEARCH_TIER": "TIER_2_CONFIRMATION",
  "RECORD_ID": "UNASSIGNED",
  "HYPOTHESIS_ID": "UNASSIGNED",
  "PROGRAM_ID": "UNASSIGNED",
  "PROGRAM_FORMAL_HYPOTHESIS_COUNTED": false,
  "TIER_1_RECORD_ID": "UNASSIGNED",
  "CANDIDATE_REVIEW_RECORD_ID": "UNASSIGNED",
  "DATA_ADMISSION_RECORD_ID": "UNASSIGNED",
  "PIT_CREDIBLE": false,
  "LIFECYCLE_CREDIBLE": false,
  "GOLD_SAMPLE": false,
  "INDEPENDENT_REVIEW": false,
  "HOLDOUT": false,
  "PRIOR_TIER_1_DATA_SOURCE_IDS": ["UNASSIGNED"],
  "PRIOR_TIER_1_TIME_WINDOW_IDS": ["GMAQ_2023"],
  "PRIOR_TIER_1_UNIVERSE_IDS": ["UNASSIGNED"],
  "PRIOR_TIER_1_DATASET_FAMILY_IDS": ["UNASSIGNED"],
  "PRIOR_TIER_1_SAMPLE_IDS": ["UNASSIGNED"],
  "CONFIRMATION_DATA_SOURCE_IDS": ["UNASSIGNED"],
  "CONFIRMATION_TIME_WINDOW_IDS": ["GMAQ_2023"],
  "CONFIRMATION_UNIVERSE_IDS": ["UNASSIGNED"],
  "CONFIRMATION_DATASET_FAMILY_IDS": ["UNASSIGNED"],
  "CONFIRMATION_SAMPLE_IDS": ["UNASSIGNED"],
  "INDEPENDENCE_BASIS": "INSUFFICIENT",
  "INDEPENDENCE_EVIDENCE": "Actual independence evidence is not yet available.",
  "UNIVERSE_DEFINITION": "UNVERIFIED",
  "COVERAGE_LIMITATIONS": "UNVERIFIED",
  "RESULT_STATUS": "CONFIRMATION_BLOCKED",
  "BLOCK_REASON": "ADDITIONAL_INDEPENDENT_EVIDENCE_REQUIRED",
  "NEXT_STAGE": "STOP",
  "READY_FOR_TINY_LIVE": false
}
```

Permitted `INDEPENDENCE_BASIS`: `TEMPORAL_NEW_WINDOW`, `INDEPENDENT_SOURCE`,
`INDEPENDENT_SAMPLE`, `TEMPORAL_AND_INDEPENDENT_SOURCE`,
`TEMPORAL_AND_INDEPENDENT_SAMPLE`, `INSUFFICIENT`. `CONFIRMED_CANDIDATE` and
completed `FAIL` require an actually supported non-insufficient basis and
`BLOCK_REASON = NONE`; insufficient evidence must remain blocked as shown.
`PROGRAM_FORMAL_HYPOTHESIS_COUNTED` remains false before a Formal Run; a
completed formal `FAIL` must set it true after updating program history.

## Tier 3 Production Candidate

```json
{
  "RESEARCH_TIER": "TIER_3_PRODUCTION_CANDIDATE",
  "RECORD_ID": "UNASSIGNED",
  "HYPOTHESIS_ID": "UNASSIGNED",
  "PROGRAM_ID": "UNASSIGNED",
  "PROGRAM_FORMAL_HYPOTHESIS_COUNTED": false,
  "TIER_2_RECORD_ID": "UNASSIGNED",
  "TIER_2_RESULT_STATUS": "UNVERIFIED",
  "FREEZE_RECORD_ID": "UNASSIGNED",
  "FORMAL_RUN_ID": "UNASSIGNED",
  "FORMAL_RUN_RESULT_STATUS": "UNVERIFIED",
  "PIT_COMPLETE": false,
  "LIFECYCLE_COMPLETE": false,
  "UNIVERSE_COMPLETE": false,
  "COSTS_COMPLETE": false,
  "EXECUTION_ASSUMPTIONS_COMPLETE": false,
  "SLIPPAGE_COMPLETE": false,
  "CONCENTRATION_COMPLETE": false,
  "ROBUSTNESS_COMPLETE": false,
  "FREEZE_COMPLETE": false,
  "FORMAL_RUN_COMPLETE": false,
  "STRATEGY_REVIEW_COMPLETE": false,
  "DRY_RUN_COMPLETE": false,
  "RELIABILITY_REVIEW_COMPLETE": false,
  "RISK_REVIEW_COMPLETE": false,
  "RESULT_STATUS": "PRODUCTION_CANDIDATE",
  "NEXT_STAGE": "STRATEGY",
  "READY_FOR_TINY_LIVE": false
}
```

Only Tier 3 can set `READY_FOR_TINY_LIVE = true`; it remains no order
authority. Tier 3 requires a prior Tier 2 `CONFIRMED_CANDIDATE`, Freeze, and
completed formal PASS already counted in its program history. Every Tier 3
`FAIL` requires `NEXT_STAGE = STOP`.
