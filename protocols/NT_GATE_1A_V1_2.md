# NT-GATE-1A v1.2 Protocol

Status at freeze: `READY`

Protocol commit must precede the frozen start.

Frozen start: `2026-08-06T07:00:00+08:00`

Wall-clock stop deadline: `2026-08-06T19:00:00+08:00`

Effective work limit: `12 hours`

Parent STOP tag: `nt-gate-1a-v1.1-stop`

## 1. Sole objective

Repair and falsify-test exactly two production `FixedTargetStrategy` callback
defects found in v1.1:

1. persist every raw `OrderFilled` event durably before applying it to the
   event-sourced coordinator;
2. route an unknown fill through durable `ANOMALY` and `fail_closed` handling
   before any order lookup can raise.

No other strategy behavior may change.

## 2. Hard exclusions

- no Gate 1B;
- no exchange, Binance Demo, Testnet, network, credentials, or market data;
- no alpha, signal, parameter, instrument, timeframe, or risk-rule research;
- no real or simulated discretionary trading;
- no legacy stopped strategy;
- no A-share code, data, environment, output, report, commit, or conclusion;
- no protocol relaxation, late signing, timestamp substitution, or evidence
  reuse as a replacement for fresh v1.2 evidence.

## 3. Shared production path

The same `FixedTargetStrategy.on_order_filled` callback must be exercised by
the tests and remain the future client callback. Test-only helpers may create
events and inject a crash immediately after durable inbox append, but they may
not implement a second fill-processing path.

The callback sequence is frozen as:

1. canonicalize the raw fill fields;
2. append and `fsync` the fill to the Strategy's durable inbox;
3. call `EventSourcedCoordinator.apply_fill`;
4. only after successful apply, read the known order intent and coordinate
   protection orders;
5. leave the inbox record append-only so replay deduplication proves exactly-once
   accounting after a crash.

## 4. Preregistered callback oracle

The semantic oracle is frozen before implementation at:

`src/global_quant/gate1a/fixtures/nt_gate_1a_strategy_callback_oracle_v2.json`

Frozen SHA-256:
`6bb21fc49e604bf300ed676b90c2b4322fa7e04ef7f3d0c25172e983987e1a21`

The oracle requires:

- known fill: inbox durable before crash, zero ledger fills before crash, one
  fill after recovery, exact position and wallet, and zero newly applied fills
  on a second recovery;
- unknown fill: inbox record durable, callback raises `UnexplainedEventError`,
  final ledger event is `ANOMALY`, durable `fail_closed=true`, and restart
  raises `RecoveryBlockedError`.

Changing the oracle after the protocol commit is an automatic `STOP` unless a
new human-authorized Gate version is opened.

## 5. Mandatory red tests

Before implementation changes, preserve failing tests which:

1. run the real `FixedTargetStrategy.on_order_filled` callback in an independent
   child process;
2. force real `SIGKILL` immediately after the Strategy durable inbox append and
   before coordinator apply;
3. restart from disk and prove the known fill is applied exactly once;
4. restart a second time and prove no duplicate fill, fee, position, or wallet
   effect;
5. deliver an unknown Nautilus `OrderFilled` through the real Strategy callback;
6. prove `ANOMALY` and `fail_closed` are durable and restart refuses trading.

Calling `coordinator.apply_fill` directly cannot satisfy these callback tests.

## 6. Tool-version evidence

Python, NautilusTrader, pytest, uv, platform, and architecture versions must be
sampled from the tested process and written to machine evidence. Hard-coded
version strings cannot satisfy PASS.

## 7. Existing v1.1 conditions retained

All v1.1 network-denial, real Nautilus backtest, protection coordination,
ledger, scenario, determinism, command logging, source-object, checksum, clean
worktree, and no-credential conditions remain mandatory. The original v1.1
evidence is historical only; v1.2 must generate a fresh evidence root.

## 8. WorkBuddy deadline

WorkBuddy must complete an independent read-only review before the final
manifest and machine verdict are generated and before the frozen deadline.
Its JSON review must be checksum-bound into the final manifest.

WorkBuddy must verify the exact tested commit and fresh evidence. It may not
edit code, run an alternative implementation, inspect A-share files, connect
to an exchange, or infer PASS from Codex's summary.

## 9. PASS conditions

All must be true:

1. protocol and callback oracle were committed before the frozen start;
2. both real Strategy callback scenarios match the frozen oracle;
3. the known fill survives `SIGKILL` and is applied exactly once;
4. unknown fill durably records `ANOMALY`, sets `fail_closed`, and blocks restart;
5. sampled tool versions are present and match the executing environment;
6. all retained v1.1 mandatory evidence passes freshly;
7. WorkBuddy review records `PASS`, `P0=0`, and `P1=0` before the deadline;
8. the final manifest, verdict, source objects, review, and evidence checksums
   are mutually bound;
9. final machine verdict is generated no later than
   `2026-08-06T19:00:00+08:00`;
10. no unresolved P0 or P1 exists.

## 10. STOP conditions

Any P0/P1, callback-path substitution, oracle change, duplicate or lost fill,
missing durable anomaly, restart that does not remain fail-closed, hard-coded
tool version evidence, network/credential access, dirty tested tree, checksum
mismatch, missing/late WorkBuddy review, or deadline overrun is `STOP`.

`STOP` forbids Gate 1B but does not permanently terminate the global-quant
project. Only the user may authorize another version.
