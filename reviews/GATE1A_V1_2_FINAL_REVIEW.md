# NT-GATE-1A v1.2 Final Review

Date: `2026-08-06`

Protocol anchor: `d05651e28222b65e78a906aa5e2be62c36c76a4a`

Tested commit: `51becd3c4ff28239a2524bbf814b8c5668acffb8`

Formal evidence root:
`evidence/runtime/gate1a-v1.2-51becd3c4ff28239a2524bbf814b8c5668acffb8`

Curated evidence:
`evidence/gate1a/51becd3c4ff28239a2524bbf814b8c5668acffb8`

## Verdict

`PASS`

The final machine verdict completed at
`2026-08-06T07:29:00.478740+08:00`, `1740.47874` seconds after the frozen
start and before the 12-hour deadline. Its failure list is empty.

WorkBuddy independently reviewed the exact tested commit and fresh evidence,
then recorded `PASS`, `P0=0`, `P1=0`, and `P2=4` before the deadline.

## Closed Findings

### 1. Real Strategy fills are durable before accounting

`FixedTargetStrategy.on_order_filled` now canonicalizes the fill and appends it
to `DurableInbox` before calling `EventSourcedCoordinator.apply_fill`. The
append includes `fsync`.

The integration test delivers a real Nautilus `OrderFilled` through the real
Strategy callback in an independent process, sends `SIGKILL` immediately after
the durable append, and proves one accounting effect after recovery and zero
new effects after a second recovery.

### 2. Unknown fills durably fail closed

The Strategy now calls the coordinator before reading the order intent. An
unknown fill therefore records `ANOMALY`, sets `fail_closed=true`, raises
`UnexplainedEventError`, and leaves restart blocked instead of raising an early
`KeyError` without durable state.

## Formal Evidence

- protocol and callback oracle were committed before the frozen start;
- callback oracle SHA-256 remained
  `6bb21fc49e604bf300ed676b90c2b4322fa7e04ef7f3d0c25172e983987e1a21`;
- six independent full-suite runs passed `150/150` with zero failures, errors,
  or skips;
- network matrix: `11/11`;
- crash matrix: `17/17`;
- real Strategy callback matrix: `2/2`;
- real Nautilus backtest matrix: `3/3`;
- scenario matrix: `12/12`;
- determinism matrix: `1/1`, with six independent process runs;
- tool-version sampling: `1/1` for Python, NautilusTrader, pytest, uv,
  platform, and architecture;
- all thirteen required restart groups passed;
- all required command exit codes were zero;
- the candidate manifest covered 149 evidence paths;
- the final manifest covered 156 evidence paths;
- source objects, evidence files, manifests, verdicts, and detached checksums
  were independently rehashed successfully;
- the tested worktree was clean and all formal commands were network isolated.

## P2 Observations

- Nautilus `BarDataWrangler` emits a pandas chained-assignment warning.
- The Nautilus backtest path emits a `Timestamp.utcnow` deprecation warning.
- The Gate records the installed package version but does not preserve the
  149 MiB Nautilus wheel bytes.
- `preflight_status.txt` is empty. This is non-blocking because all fifteen
  command records independently bind the clean worktree and tested commit.

## Proof Boundary

This PASS proves the frozen offline Strategy callback, event accounting,
crash recovery, fail-closed behavior, determinism, and evidence contract. It
does not prove Binance Demo or live order semantics, funding behavior, market
data quality, strategy alpha, profitability, or real-money readiness.

No Gate 1B, exchange connection, market data, credential, alpha research, Demo,
or live-money work was performed.

## Next Action

Wait for explicit human authorization of a separately versioned and frozen
Gate 1B. This completed Gate does not authorize that work automatically.
