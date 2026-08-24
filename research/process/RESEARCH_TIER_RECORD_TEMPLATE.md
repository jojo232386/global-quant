# GMAQ Research Tier Record Template

Status: `TEMPLATE_FAIL_CLOSED_NO_LIVE`

Create one JSON record using exactly one tier schema below, then validate it
with `python research/process/research_tier.py RECORD.json`. All placeholder
values are deliberately invalid or no-live. The checker verifies record shape
and declared gates only; the cited evidence remains subject to independent
review.

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

Permitted `RESULT_STATUS`: `MECHANISM_VALID`, `FAIL`.
Permitted `NEXT_STAGE`: `CANDIDATE_REVIEW`, `STOP`.

## Tier 2 Confirmation

```json
{
  "RESEARCH_TIER": "TIER_2_CONFIRMATION",
  "RECORD_ID": "UNASSIGNED",
  "HYPOTHESIS_ID": "UNASSIGNED",
  "TIER_1_RECORD_ID": "UNASSIGNED",
  "CANDIDATE_REVIEW_RECORD_ID": "UNASSIGNED",
  "DATA_ADMISSION_RECORD_ID": "UNASSIGNED",
  "PIT_CREDIBLE": false,
  "LIFECYCLE_CREDIBLE": false,
  "UNIVERSE_DEFINITION": "UNVERIFIED",
  "COVERAGE_LIMITATIONS": "UNVERIFIED",
  "GOLD_SAMPLE": false,
  "INDEPENDENT_REVIEW": false,
  "HOLDOUT": false,
  "RESULT_STATUS": "FAIL",
  "NEXT_STAGE": "STOP",
  "READY_FOR_TINY_LIVE": false
}
```

Permitted `RESULT_STATUS`: `CONFIRMED_CANDIDATE`, `FAIL`.
`CONFIRMED_CANDIDATE` requires every Tier 2 gate true and `NEXT_STAGE = FREEZE`.
`FAIL` requires `NEXT_STAGE = STOP`.

## Tier 3 Production Candidate

```json
{
  "RESEARCH_TIER": "TIER_3_PRODUCTION_CANDIDATE",
  "RECORD_ID": "UNASSIGNED",
  "HYPOTHESIS_ID": "UNASSIGNED",
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

Permitted `RESULT_STATUS`: `PRODUCTION_CANDIDATE`, `FAIL`.
Permitted `NEXT_STAGE`: `STRATEGY`, `DRY_RUN`, `RELIABILITY_REVIEW`,
`RISK_REVIEW`, `LIVE`, `STOP`. `NEXT_STAGE = LIVE` requires every Tier 3 gate
and `READY_FOR_TINY_LIVE = true`; the latter remains no order authority. A
Tier 3 record must reference the prior Tier 2 `CONFIRMED_CANDIDATE`, Freeze,
and completed Formal Run records. `FORMAL_RUN_RESULT_STATUS = PASS` means the
frozen formal evaluation itself passed; admission, process completion, or
review acceptance alone is not a pass. Every `FAIL` requires `NEXT_STAGE = STOP`.
