# NT-GATE-1A Frozen Protocol

Protocol version: `1.0`

Status at freeze: `READY`

Frozen start: `2026-07-30T06:26:32+08:00`

Wall-clock stop deadline: `2026-07-30T18:26:32+08:00`

Effective work limit: `12 hours`

## 1. Sole objective

Prove, offline and without alpha, that one shared NautilusTrader `Strategy`
source and one shared order-intent/state-machine/event-ledger model can:

1. produce a deterministic BTC/ETH target sequence;
2. survive rejected, partial, duplicated, out-of-order, and unknown events;
3. recover from non-graceful process death using durable state and an
   append-only event ledger;
4. replay to exactly the same orders, positions, wallet balance, equity, and
   protection state across independent processes and hash seeds.

This gate proves local engineering behavior only.

## 2. Explicit exclusions

- No API key, secret, credential, exchange connection, DNS lookup, HTTP,
  WebSocket, or other outbound network access during gate execution.
- No Binance Demo, Testnet, or live endpoint.
- No claim about Binance order, conditional-order, funding, or reconnect
  semantics.
- No market-data download, alpha, indicator, parameter search, return
  optimization, or profitability conclusion.
- No old SMC, ATR, SMA, or terminal-trend code.
- No A-share project access or shared code, data, environment, output, report,
  commit, conclusion, or credential.
- No hidden strategy branch based on backtest, demo, or live environment.
- No test fixture, fault injector, or simulated fill logic inside production
  strategy code.
- No native Binance OCO or Nautilus bracket-order claim. Protective orders are
  independent intents coordinated by local state.

## 3. Meaning of one shared strategy source

Backtest and the later Demo runner must use the same:

- `Strategy` decision logic;
- target-to-order intent logic;
- order state machine;
- append-only ledger event model;
- protection-group coordination rules.

They may use different runtime configuration, data clients, execution clients,
and test fixtures. Production `Strategy` behavior must not inspect an
environment name to choose different trading behavior.

The shared strategy and state-machine source files must be SHA-256 hashed and
recorded in the verdict.

## 4. Fixed no-alpha target schedule

The schedule exists only to exercise mechanics:

| Step | BTC target | ETH target |
|---|---:|---:|
| 0 | flat | flat |
| 1 | +10% notional | -10% notional |
| 2 | flat | flat |
| 3 | -10% notional | +10% notional |
| 4 | flat | flat |

The schedule must exercise entry, exit, shorting, reversal, and two-instrument
coordination. It is not a strategy hypothesis.

## 5. Proof boundary between Gate 1A and Gate 1B

Gate 1A may prove only:

- local order and protection coordination is internally consistent under the
  frozen event model;
- ledger, order, position, wallet, and equity state can be recovered;
- duplicate, out-of-order, rejected, canceled, and partial-fill events do not
  create duplicate orders or unexplained risk.

Gate 1B alone may test actual Binance Demo acknowledgements, conditional-order
trigger/cancel behavior, funding, connection loss, and venue reconciliation.

## 6. Required scenario matrix

Every scenario declares its initial state, input events, expected intents,
expected fills, final position, final wallet/equity, protection state, and exit
code.

1. New order rejected.
2. Order submitted but not acknowledged.
3. Partial fill followed by completion.
4. Partial fill followed by cancel of the remainder.
5. Cancel rejection and cancel/fill race.
6. Reversal while the old position is not fully closed.
7. One protective order fills and cancels its siblings.
8. Main position is closed while protection orders remain active or pending.
9. Duplicate order, fill, account, and position events.
10. Out-of-order events.
11. Unknown external order or unexplained fill.
12. Replayed ledger disagrees with a stored runtime snapshot.

Unknown or unexplained events must fail closed. Ignoring them is forbidden.

## 7. Required non-graceful termination matrix

Each crash test runs in an independent process, uses forced termination, and
recovers only from durable files:

1. decision persisted, order not submitted;
2. order submitted, acknowledgement absent;
3. order acknowledged, fill absent;
4. partial fill persisted;
5. cancel requested, confirmation absent;
6. protection group update in progress;
7. event append/checkpoint boundary.

After restart, duplicate historical events are delivered again to verify
idempotency. A graceful framework reset is not accepted as crash evidence.

## 8. Append-only ledger contract

Each canonical JSONL event includes:

- `decision_id`
- `strategy_id`
- `run_id`
- `instrument_id`
- `client_order_id`
- `venue_order_id`
- `position_id`
- `correlation_id`
- `causation_id`
- `event_id`
- `event_sequence`
- `event_timestamp`
- `receive_timestamp`
- `event_type`
- `order_intent`
- `order_transition`
- `fill`
- `fee`
- `position_transition`
- `balance_transition`
- `protection_group_id`
- `persistence_checkpoint`
- `process_start_id`
- `source_hash`
- `config_hash`

Optional fields remain present with JSON `null`. Events are immutable once
appended. Derived orders, positions, balances, and protection state must be
rebuildable from the ledger.

For the Gate 1A USDT-perpetual accounting model, funding is zero:

`wallet_balance = initial_balance + realized_pnl - cumulative_fees`

`equity = wallet_balance + unrealized_pnl`

Replay state must match the final runtime snapshot field by field after
excluding only declared volatile metadata.

## 9. Mandatory invariants

1. One `decision_id` may not create duplicate effective orders for the same
   instrument and target state.
2. One fill event affects position, realized PnL, fees, wallet, and equity once.
3. `client_order_id` is unique within the strategy account scope.
4. Duplicate entry orders inconsistent with target intent cannot coexist.
5. A flat main position cannot retain protection capable of increasing risk.
6. Replay before and after restart yields identical business state.
7. Wallet, realized PnL, unrealized PnL, fees, and equity satisfy the frozen
   accounting identities.
8. Unexplained events cannot be discarded to obtain a passing result.

## 10. Offline network enforcement

Gate execution runs under a process-level macOS sandbox denying network access
and a Python guard that immediately raises, records a stack trace, and exits
non-zero for:

- `socket.connect`;
- `socket.connect_ex`;
- `socket.create_connection`;
- DNS resolution;
- HTTP and HTTPS clients;
- WebSocket clients;
- inherited child-process access;
- IPv4 and IPv6.

The claim is limited to processes launched by the frozen gate harness. The gate
does not claim a machine-wide firewall.

## 11. Determinism matrix

The complete suite runs:

- with at least two distinct `PYTHONHASHSEED` values;
- in independent processes;
- from cold start;
- from each frozen crash checkpoint;
- at least three complete repetitions.

Process IDs, process start IDs, and wall-clock receive timestamps are declared
volatile. Business events, canonical ordering, intent identifiers, final
position, wallet/equity, and replay hash must be identical.

## 12. Timebox and verdict semantics

The effective work limit is 12 hours.

`PASS` requires every frozen condition below.

`STOP` means this Gate 1A design stops and Gate 1B is forbidden. It does not
permanently stop the global-quant project. The stop report must include root
cause evidence, attempted bounded remedy, and the smallest redesign proposal.
Only a human may authorize a newly versioned Gate 1A.

## 13. PASS conditions

All must be true:

1. Shared production strategy source is used without environment behavior
   branches.
2. No network access and no credentials.
3. All core scenarios and crash points pass.
4. No duplicate effective order or duplicate fill accounting.
5. Ledger replay is complete and equals the runtime snapshot.
6. Restart state is identical and idempotent.
7. Business results match across processes, repetitions, and hash seeds.
8. Strategy and state-machine hashes are recorded.
9. Worktree status, commands, timestamps, numeric exit codes, and evidence paths
   are traceable.
10. `P0=0` and `P1=0`.
11. Effective work is within 12 hours.

## 14. STOP conditions

Stop on any of:

- duplicated effective order or double-counted fill;
- unexplained order, fill, position, wallet, equity, or protection state;
- restart recovery depends on old process memory;
- ledger replay differs from final state;
- network or credential access occurs;
- production strategy requires an environment-specific behavior branch;
- any unresolved P0 or P1;
- the 12-hour timebox is exceeded.

One bounded diagnosis and one minimal remedy are allowed for an implementation
defect. Feature expansion, market data, alpha, or parameter changes are not
remedies.

## 15. Required machine verdict

`gate_1a_verdict.json` must contain:

- `verdict`
- `started_at`
- `completed_at`
- `effective_work_duration`
- `repository`
- `branch`
- `commit`
- `dirty_worktree`
- `strategy_hash`
- `state_machine_hash`
- `config_hash`
- `test_commands`
- `exit_codes`
- `network_block_status`
- `scenario_results`
- `restart_results`
- `ledger_replay_hash`
- `unresolved_P0`
- `unresolved_P1`
- `unresolved_P2`
- `evidence_paths`

Only `PASS` or `STOP` is valid for this offline gate.

## 16. Required evidence

- frozen protocol commit;
- dependency lock;
- source and configuration checksums;
- red/green test evidence;
- scenario and crash-test logs;
- network-denial logs with stack traces;
- deterministic replay outputs;
- command, timestamp, and numeric-exit-code log;
- human-readable decision;
- `gate_1a_verdict.json`;
- WorkBuddy read-only review appended to the legacy coordination inbox.

## 17. WorkBuddy boundary

WorkBuddy may only read and independently verify the frozen commit and evidence.
It must not edit implementation, write an alternative engine, alter the gate,
connect to an exchange, use credentials, or start another research route.
WorkBuddy must verify raw evidence item by item and report P0/P1/P2 plus a
four-state conclusion. A summary-only endorsement is invalid.

## 18. Sole next action

- `PASS`: freeze Gate 1A and separately preregister Gate 1B for Binance Futures
  Demo.
- `STOP`: do not enter Gate 1B; present the recorded redesign decision to the
  user.
