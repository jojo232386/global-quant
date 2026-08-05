# NT-GATE-1A Final Review

Date: `2026-08-06`

Reviewed commit: `b4485b2ec6e482420d23926cfdbd454539834abb`

Formal evidence root:
`evidence/runtime/gate1a-b4485b2ec6e482420d23926cfdbd454539834abb`

## Verdict

`STOP`

The within-window machine candidate was `PASS`, but it was not the final Gate
verdict. Final review found `P0=0`, `P1=3`, and the frozen wall-clock deadline
had already expired. Gate 1B is forbidden.

The final machine verdict records two independently sufficient failures:

- the machine-enforced 12-hour limit was exceeded;
- the required WorkBuddy review was missing.

No timestamp was backfilled and no protocol condition was relaxed.

## P1 Findings

### 1. Real Strategy fills bypass the durable execution inbox

`tests/helpers/crash_worker.py:77` manually writes a fill to `DurableInbox`
for the `execution_confirm_unpersisted` crash boundary. The production
`FixedTargetStrategy.on_order_filled` path at
`src/global_quant/gate1a/strategy.py:312` instead calls
`coordinator.apply_fill()` directly and never first persists the raw execution
event to that inbox.

The test therefore proves recovery for the helper model, not for the real
Strategy callback at the critical "fill received, ledger not yet durable"
boundary.

Acceptance for a newly authorized Gate: deliver an `OrderFilled` through the
real `FixedTargetStrategy` in an independent process, force `SIGKILL` at the
persistence boundary, restart from disk, and prove the fill is applied exactly
once.

### 2. Unknown fills bypass durable fail-closed handling

`src/global_quant/gate1a/strategy.py:314` indexes
`coordinator.orders[client_order_id]` before calling the coordinator. An unknown
order raises `KeyError` before `EventSourcedCoordinator.apply_fill()` can write
an `ANOMALY` event and set `fail_closed=true` at
`src/global_quant/gate1a/coordinator.py:492`.

The existing unknown-event scenario at
`src/global_quant/gate1a/scenarios.py:470` calls the coordinator directly and
does not cover the real Strategy callback.

Acceptance for a newly authorized Gate: inject an unknown `OrderFilled` into
the real Strategy, require a durable `ANOMALY`, require `fail_closed=true`, and
prove restart refuses further trading.

### 3. Frozen wall-clock deadline expired before final independent review

The protocol fixed the deadline at `2026-07-30T18:26:32+08:00`. The candidate
evidence completed at `2026-07-30T07:57:18+08:00`, but the required independent
review was not completed before the deadline. Rebuilding the manifest on
`2026-08-06` correctly produced a duration greater than 12 hours and a machine
`STOP`.

This cannot be repaired by late signing or timestamp substitution. Only the
user may authorize a newly versioned Gate 1A with a fresh start and deadline.

## P2 Findings

- The scenario oracle and implementation can be changed in the same commit;
  the oracle has no earlier independent preregistration anchor.
- Recovery preserves the old `process_start_id`, weakening restart provenance.
- Tool versions in `build_gate_manifest.py` are hard-coded rather than sampled
  from the tested environment.
- Nautilus emits the preserved pandas compatibility warnings.
- The installed Nautilus wheel bytes are not retained in the Gate evidence.

## Evidence Preserved

- six independent full-suite runs: `145 passed`, zero failures each;
- 17 crash-recovery tests, zero failures;
- 12 scenario tests, zero failures;
- 11 network-isolation tests, zero failures;
- 3 real Nautilus backtest tests, zero failures;
- two hash seeds and three repetitions with one business replay hash;
- 140 candidate evidence hashes verified;
- candidate and final manifest/verdict detached checksums verified.

These are useful negative engineering evidence. They do not override the P1
findings and do not prove Binance behavior, alpha, profitability, or live
readiness.

## Failed Auxiliary Reviews

The Claude Code/Qwen Code comparison was preserved in the legacy coordination
inbox. Claude was denied by the read-only permission policy and Qwen timed out;
neither is counted as a completed review. WorkBuddy was not submitted into an
active A-share task, preserving project separation.

## Only Permitted Next Step

Wait for explicit human authorization of `NT-GATE-1A v1.2`. If authorized, the
new Gate may only repair the two real-Strategy P1 paths, preregister the oracle,
sample tool versions, and rerun the same no-network/no-alpha evidence within a
new fixed timebox. It may not enter Gate 1B or research alpha.
