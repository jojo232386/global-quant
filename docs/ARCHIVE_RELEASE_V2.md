# GMAQ Validation Archive v2 portable acceptance contract

## V1 preserved outcome

Validation Archive v1 passed twice on the local machine with all 487 tests and produced byte-identical result SHA256 `fad57ee210aca8fe8ff11eeb811be43c4e2046784d11e17c2b7cb1c71875d1ba`. GitHub CI then produced 482 passed tests and 5 skipped tests because the repository does not contain the local immutable Data Layer warehouse.

The v1 contract required 487 passed tests on every verifier host. Its release status is `NOT_RELEASED / PROCESS_DEFECT_PORTABILITY`. V2 preserves that result and fixes the contract through a new release ID. It does not modify v1 or reinterpret its failed gate.

## Frozen objective

Release a repository-complete validation product whose output stays identical on a machine with the external warehouse and on a clean GitHub CI runner without it.

The Git commit containing this contract is the v2 acceptance freeze. Later implementation may update the verifier, CI, README, case study, banner, and v2 machine result. It must not change the rules below or any bound research artifact.

## Bound lineage and evidence

- Research closeout: `db45f4e9f434d7a391b33ebdda4cc1e1d6672e30`
- V1 contract freeze: `8e1fc13deda460b90526dd071960a646ec832f56`
- V1 failed CI run: `33253093383`
- Test-isolation repair: `1c44fb2`

V2 retains every immutable evidence path and SHA256 from `docs/ARCHIVE_RELEASE.md`.

## Required gates

### 1. Archive integrity and critical controls

- The five v1 evidence hashes and their closeout fields must match.
- The seven focused fail-closed test files remain unchanged as the critical suite.
- All 109 critical controls must pass with no skip, failure, error, or xfail.

### 2. Portable full-suite contract

Pytest must collect exactly 487 tests. The verifier accepts one of two complete modes:

1. `WAREHOUSE_PRESENT`: 487 passed, 0 skipped.
2. `PORTABLE_REPOSITORY`: 482 passed and exactly these 5 data-bound tests skipped:
   - `tests/test_spot_perp_carry_contract.py::test_real_bound_dataset_replays_verified`
   - `tests/test_vbt_alpha_program_001.py::test_range_overlay_matches_the_unchanged_frozen_price_loader`
   - `tests/test_expl_017_formal_consumer.py::test_real_snapshot_can_bind_to_sidecar_without_reading_performance`
   - `tests/test_ls_tsmom_contract.py::test_verified_curated_snapshot_binding_loads_real_v1_dataset`
   - `tests/test_expl_017_lifecycle_v1.py::test_real_price_v1_structure_scope_is_read_only_when_local_snapshot_is_available`

The portable mode must report the documented missing external warehouse reason for each skip. Any other skip, any partial subset of the five, or any failure/error/xfail blocks release.

### 3. Cross-mode deterministic result

The verifier must emit the same canonical JSON in both modes. The result records:

- 487 collected tests;
- 482 repository-portable tests;
- 5 bound external-data checks;
- 109 critical controls;
- no host-specific mode, time, duration, path, or user value.

Two consecutive local runs must be byte-identical. GitHub CI must match the committed v2 result byte for byte.

### 4. Claim boundary

The canonical v2 result must contain:

```json
{
  "release": "GMAQ_VALIDATION_ARCHIVE_V2",
  "status": "ARCHIVE_RELEASED",
  "validation_benchmark": "PASS",
  "collected_tests": 487,
  "portable_tests": 482,
  "external_data_checks": 5,
  "promoted_alpha": 0,
  "ready_for_strategy": false,
  "ready_for_tiny_live": false,
  "real_orders": 0,
  "profitability_claim": false
}
```

The 5 data-bound tests are not declared passed by portable CI. They remain named, externally bound checks. `ARCHIVE_RELEASED` covers the validation product and its portable evidence boundary; it does not cover Alpha, profitability, live readiness, or real trading.

## Stop rule

Any artifact mismatch, critical-test skip/failure, unexpected full-suite skip, collection-count drift, mode-dependent JSON, local double-run mismatch, or GitHub CI mismatch sets v2 to `NOT_RELEASED`. The team must preserve the failure and create a new contract for any changed rule.
