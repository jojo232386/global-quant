# GMAQ Validation Archive v1 acceptance contract

## Frozen objective

Release GMAQ as a complete, reproducible validation product. The release succeeds only if the repository proves that its data, research, runtime, and live-admission controls fail closed against the preserved evidence.

The Git commit containing this contract is the acceptance freeze. Later implementation may add the verifier, its focused test, the machine-readable result, and presentation updates. It must not change the gates below or rewrite any bound research artifact.

## Bound baselines

- Research closeout: `db45f4e9f434d7a391b33ebdda4cc1e1d6672e30`
- Showcase closeout: `bc18b5b`
- Standalone runtime-test repair: `1c44fb2`
- Runtime and research conclusion: `FROZEN_VALIDATION_ARCHIVE`

## Immutable evidence

| Artifact | SHA256 |
| --- | --- |
| `research/process/oss-strategy-replication-001-program-history.json` | `b3d5ffb9b706370bd6175fb65006db21f03d0603657facdf3aec5129a786a731` |
| `research/process/vbt-alpha-program-001-program-history.json` | `b3609443852b9694d3dc244b2a648346dcc709db0d1f7d3b4a7289e860325b98` |
| `research/exploration/expl-017-formal-003-result.json` | `c55a4ddc5e8919f5ddd87b19d810d009851661c0a6828a283a285e87b08e347f` |
| `research/exploration/factor-graveyard.md` | `8cc1bb770b79d42d268d957820836a2a628eead9db82b7f050f4170c6ac07bb0` |
| `configs/LIVE_READINESS.md` | `e0262dcd26baa5d9d8c628533fb1f18be00c1c85015db425c4e5d0a13c7d7c66` |

## Required gates

### 1. Archive integrity

The verifier must reproduce every bound SHA256 and confirm these closeout facts:

- OSS replication result is `NO_ADMISSIBLE_OSS_CANDIDATE`, with 8 repositories, 30 strategy files, 5 shortlisted candidates, no strategy or tiny-live readiness, and 0 real orders.
- VBT Alpha Program result is `VBT_ALPHA_PROGRAM_001_EXHAUSTED`, with no strategy or tiny-live readiness and 0 real orders.
- EXPL-017 Formal 003 ran once and records `HYPOTHESIS_FAIL`.
- The Factor Graveyard retains the EXPL-017 failure and its no-rescue rule.
- Live-readiness blockers remain explicit; the archive must not emit a live-ready claim.

### 2. Critical fail-closed controls

The verifier must execute these exact test files in one isolated pytest invocation:

- `tests/test_data_layer_contract.py`
- `tests/test_control_room_contract.py`
- `tests/test_live_admission.py`
- `tests/test_expl_017_formal_003_freeze.py`
- `tests/test_research_tier.py`
- `tests/test_runtime_contract.py`
- `tests/test_entry_gate_behavior.py`

The frozen collection contains 109 tests. All 109 must pass. The verifier must force localhost traffic to bypass desktop HTTP proxies.

### 3. Full repository contract

The hash-pinned Python 3.12 environment must pass the complete suite of 487 tests. A failing full suite blocks release even if the focused controls pass.

### 4. Deterministic result

Two consecutive verifier runs against the same commit and Python environment must produce byte-identical JSON. The committed result contains no clock time, duration, host name, user name, temporary path, or network-derived value.

### 5. Claim boundary

The result must contain these exact facts:

```json
{
  "release": "GMAQ_VALIDATION_ARCHIVE_V1",
  "status": "ARCHIVE_RELEASED",
  "validation_benchmark": "PASS",
  "promoted_alpha": 0,
  "ready_for_strategy": false,
  "ready_for_tiny_live": false,
  "real_orders": 0,
  "profitability_claim": false
}
```

`ARCHIVE_RELEASED` means the validation product passed its release contract. It does not mean a strategy passed, the system can trade live, or the project earned money.

## Stop rule

Any hash mismatch, artifact mismatch, focused-test failure, full-suite failure, nondeterministic output, or broadened claim sets the release to `NOT_RELEASED`. The team must fix a process defect or preserve the failure. It must not reduce the test set, change an expected artifact value, or weaken the claim boundary after seeing the result.
