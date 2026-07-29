# Gate 1A Red Evidence

These logs preserve expected test-first failures and implementation defects
found before the implementation snapshot was frozen.

## Missing-feature red tests

- `red/core_red.log`: shared package did not exist.
- `red/scenario_matrix_red.log`: scenario runner did not exist.
- `red/determinism_red.log`: deterministic runner did not exist.
- `red/arbiter_red.log`: machine arbiter did not exist.
- `red/command_logger_red.log`: command logger did not exist.
- `red/ledger_immutability_red.log`: callers could mutate an appended event.
- `red/protection_race_red.log`: a late sibling protection fill could reopen
  risk.
- `red/reversal_recovery_red.log`: a pending reversal target was lost after
  restart.
- `red/nautilus_backtest_red*.log`: the real Nautilus integration was absent,
  then exposed fixture and configuration failures.

## Defects found after the first implementation

- `red/isolation_recovery_red_after_impl.log`: replaying already-applied events
  mutated recovered state in five crash scenarios.
- `red/scenario_matrix_red_after_impl.log`: repeated legitimate transitions
  reused one event ID.
- `red/arbiter_red_after_impl.log`: JUnit suite totals were parsed incorrectly.
- `red/nested_offline_guard_red.log`: nested raw-socket probes inherited the
  Python guard and did not independently prove the OS sandbox.
- `red/nautilus_strategy_red_after_impl.log`: the first shared Strategy config
  layout was incompatible with Nautilus `StrategyConfig`.

These failures are evidence that the tests detected the intended defect. They
are not PASS evidence. Final PASS or STOP is decided only from the separately
generated clean evidence manifest.

## Preserved setup issue

The first offline development-environment sync could not install the frozen
`ruff==0.12.7` wheel because it was absent from the local uv cache. A second
offline attempt also showed the local project build backend was not cached.
No network exception was granted. The bounded remedy removed unused lint and
timeout packages, aligned the lock to the already cached `pytest==9.1.1`, and
kept `nautilus-trader==1.230.0` unchanged. Final evidence records the actual
versions.

