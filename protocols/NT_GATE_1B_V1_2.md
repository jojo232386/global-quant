# NT-GATE-1B v1.2 Frozen Protocol

Protocol version: `1.2`

Status at freeze: `READY`

Frozen start: `2026-08-06T08:20:00+08:00`

Wall-clock stop deadline: `2026-08-06T20:20:00+08:00`

Effective work limit: `12 hours`

This protocol must be committed before the frozen start. No Binance connection,
credential use, or Demo order is permitted before that commit. Version `1.0`
remains preserved at tag `nt-gate-1b-v1.0-protocol`; a pre-implementation
adapter audit found that its two-host allowlist omitted the pinned adapter's
authenticated Demo USD-M WebSocket API route. No connection occurred under
version `1.0`. Version `1.1` remains preserved at tag
`nt-gate-1b-v1.1-protocol`; a second pre-implementation feasibility count found
that its 20-order cap could not contain its own mandatory schedule plus
rejection, cancel, partial-fill, funding, and protection-trigger probes. It
also left a mandatory protective fill dependent on a 20% market move. No
connection occurred under version `1.1`.

## 1. Sole objective

Falsify-test the already shared NautilusTrader Strategy, order coordinator, and
append-only ledger against actual Binance USD-M Futures Demo behavior. The gate
tests venue acknowledgements, conditional orders, cancel/fill races, restart
reconciliation, fees, funding, and bounded multi-instrument operation.

This is an execution-engineering gate. It does not test alpha or profitability.

## 2. Explicit exclusions

- No Binance production or general legacy Testnet connection. The one exact
  `testnet.binancefuture.com/ws-fapi/v1` route which NautilusTrader `1.230.0`
  selects for authenticated USD-M Demo WebSocket API traffic is permitted only
  with `BinanceEnvironment.DEMO` and Demo credentials.
- No real-money credential, account, balance, position, order, or market action.
- No historical-market-data research, alpha, indicator, return optimization,
  parameter search, or Gate 2 work. Real-time Demo data is permitted only to
  drive the frozen mechanical execution sequence.
- No old SMC, ATR, SMA, or terminal-trend strategy.
- No unattended daemon, launchd job, or startup service.
- No A-share repository, code, environment, data, output, report, or credential.
- No native Futures OCO or Nautilus bracket-order claim. Protection remains two
  independent conditional orders coordinated locally.
- No result may be promoted from Demo behavior to production reliability.

## 3. Frozen software and shared-core contract

- Python: `3.12.x` from the project `uv` environment.
- NautilusTrader: `1.230.0`.
- The production Strategy remains
  `global_quant.gate1a.strategy.FixedTargetStrategy`.
- Demo and offline verification use the same Strategy decision logic, order
  intent logic, coordinator, ledger, protection-group logic, and recovery model.
- Runtime configuration, data client, execution client, and fault harness may
  differ. The Strategy may not inspect `backtest`, `demo`, `testnet`, or `live`
  to choose different trading behavior.
- Strategy, coordinator, ledger, recovery, Demo configuration, and protocol
  source objects are SHA-256 bound in the verdict.

## 4. Hard environment and credential guard

The only allowed venue configuration is:

- `BinanceEnvironment.DEMO`;
- `BinanceAccountType.USDT_FUTURES`;
- venue `BINANCE`;
- one-way/netting position mode;
- `use_reduce_only=True`;
- no base URL override;
- no proxy override.

Only `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` may be read. The runner
must fail before DNS or socket creation if either is absent, empty, or if any of
these are present: `BINANCE_API_KEY`, `BINANCE_API_SECRET`,
`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`,
`BINANCE_FUTURES_TESTNET_API_KEY`, or
`BINANCE_FUTURES_TESTNET_API_SECRET`.

Secrets must never appear in arguments, configuration files, logs, evidence,
tracebacks, process listings, Git objects, hashes, or reports. The runner passes
them directly from process memory into Nautilus configuration. Evidence records
only presence booleans, the Demo account identifier returned by the venue after
redaction, and redaction status. Neither credential value is ever hashed or
retained.

The resolved network allowlist is exactly:

- `demo-fapi.binance.com:443` for USD-M HTTP;
- `demo-fstream.binance.com:443` for public/private WebSocket streams;
- `testnet.binancefuture.com:443/ws-fapi/v1` for the authenticated USD-M Demo
  WebSocket API selected by the pinned adapter.

Any production, Spot, COIN-M, other Testnet path, custom, redirected, or
unrecognized host is a safety failure. Host-only validation is insufficient for
`testnet.binancefuture.com`; its scheme, host, port, and `/ws-fapi/v1` path must
match exactly. The runner must require an explicit
`--confirm-demo-only` arming flag in addition to valid Demo variables.

## 5. Account preflight

Before order submission, the runner must prove and record:

1. Demo environment and all three allowlisted endpoints are resolved from the
   pinned adapter implementation.
2. Credentials authenticate against Demo and have Futures trading permission.
3. The USD-M account is in one-way mode.
4. There are no open positions and no open orders anywhere in the Demo USD-M
   account.
5. BTCUSDT and ETHUSDT perpetual instruments are trading and their live filters
   are loaded.
6. Exchange server time skew is within the configured receive window.
7. Demo balance is virtual and sufficient for the bounded mechanical sequence.

An unclean account is not automatically modified. Unknown positions or orders
produce `STOP`; the system must not cancel or close activity it cannot prove it
owns.

## 6. Frozen mechanical sequence and risk caps

The no-alpha target sequence remains:

| Step | BTC target | ETH target |
|---|---:|---:|
| 0 | flat | flat |
| 1 | long | short |
| 2 | flat | flat |
| 3 | short | long |
| 4 | flat | flat |

Configuration may scale target quantities only to satisfy live instrument
filters while respecting all caps:

- leverage: `1x`;
- maximum absolute entry notional: `200 USDT` per instrument;
- maximum aggregate gross notional: `400 USDT`;
- maximum simultaneous non-flat instruments: `2`;
- maximum submitted orders for the entire gate: `32`;
- maximum one-way net position per instrument: one;
- the primary target sequence uses the shared Strategy's 20% protection
  defaults;
- every protection order is reduce-only;
- no order may survive the final account-flat check.

A separate one-instrument protection-trigger probe may set only the take-profit
fraction to `0.001` (0.10%) while retaining the 20% stop fraction, the same
shared Strategy source, and the same notional caps. It may not change target
logic, order types, coordinator behavior, or accounting.

If the exchange minimum notional exceeds a cap, the gate is `INCONCLUSIVE`; the
cap may not be raised after seeing the result.

## 7. Required live-Demo evidence matrix

All scenarios record request intent, client order ID, venue order ID, event
timestamps, receive timestamps, state transitions, fills, fees, positions,
balances, protection group, and final account query.

Mandatory scenarios:

1. Authenticate, load instruments, and receive public plus private streams.
2. Submit and fill BTC-long plus ETH-short entry orders.
3. Confirm separate stop-market and market-if-touched protection orders in the
   primary schedule, and run the frozen 0.10% one-instrument trigger probe.
4. Close both positions and prove sibling protection cancellation.
5. Submit and fill BTC-short plus ETH-long entry orders, then return flat.
6. Exercise a venue rejection without exceeding the frozen risk caps.
7. Exercise submit-then-cancel and capture either cancel-before-fill or the
   actual fill/cancel race outcome without rewriting expectations.
8. Exercise at least one partial fill, then complete or cancel the remainder.
9. Observe at least one actual funding settlement while a bounded Demo
   position is open, and reconcile it separately from trade PnL and commission.
10. Query orders, trades, positions, balances, fees, and funding after each
    phase and match them to the local ledger.

If Demo liquidity does not produce a partial fill, the protection probe does
not trigger, or the funding boundary is unavailable within the frozen deadline,
the result is `INCONCLUSIVE`, not PASS and not an invitation to alter the
protocol.

## 8. Protection-order semantics

Futures protection uses independent `STOP_MARKET` and `MARKET_IF_TOUCHED`
orders. The local protection coordinator owns the relationship.

The gate must prove:

- both orders are acknowledged by Binance Demo;
- both are reduce-only and cannot enlarge a flat or reversed position;
- one protective fill makes the sibling cancellation mandatory;
- a close of the main position cancels both live or inflight protections;
- an algo order is canceled through the venue semantics selected by the pinned
  adapter before or after trigger;
- a cancel/fill race never leaves an unprotected position or risk-increasing
  orphan order.

## 9. Connection loss and forced-restart matrix

Restart tests use independent processes and real Demo state. At minimum:

1. Force-kill after local intent persistence and before venue acknowledgement.
2. Force-kill after venue acknowledgement and before local checkpoint.
3. Force-kill after a partial fill.
4. Force-kill after a cancel request and before its terminal event.
5. Force-kill while a protection group has two active children.
6. Disconnect the private stream, reconnect, and reconcile missed events.

On restart, venue queries are authoritative for external state while the hash-
chained ledger is authoritative for already applied economic events. Recovery
must reuse client order IDs, apply each fill/fee/funding event exactly once,
record discrepancies, and fail closed on anything unexplained.

A graceful stop or in-process reset is not crash evidence.

## 10. Accounting and reconciliation invariants

The Gate 1A invariants remain mandatory. Gate 1B additionally requires:

1. Venue order/trade IDs map one-to-one to local durable events.
2. A venue fill, commission, or funding event changes the ledger once.
3. Local net position equals the Binance Demo position after every phase.
4. Local open-order set equals the venue open-order set after reconciliation.
5. Wallet balance, realized PnL, commission, funding, and equity reconcile to
   the precision exposed by Binance.
6. No unexplained external order, trade, transfer, position, or balance change
   may be ignored.
7. A flat account has zero live or inflight protection orders.
8. Final account state is flat with no open orders, even after a failed test.

## 11. Bounded multi-instrument capacity check

The gate includes only BTCUSDT and ETHUSDT concurrent actions. A ten-instrument
order storm remains a separate capacity gate required before Gate 2. A two-
instrument PASS may not be described as ten-instrument readiness.

## 12. Failure containment and cleanup

- Every runner has a bounded timeout and a final reconciliation phase.
- Normal failure attempts to cancel only orders whose client IDs and source
  hashes prove ownership, then closes only positions proven to have been opened
  by this gate.
- Unknown external activity prevents automated cleanup and forces `STOP` plus
  manual Demo inspection.
- `SIGKILL` scenarios must have a separately launched recovery process.
- No background process may remain after the evidence run.

## 13. Verdict semantics

`PASS` requires every mandatory scenario, restart point, accounting invariant,
safety control, final-flat check, evidence check, and independent review.

`INCONCLUSIVE` is allowed only for an external precondition that provides no
adverse engineering evidence: missing Demo credentials, Demo outage, exchange
minimum above the frozen cap, no partial fill, no protection-trigger event, or
no funding boundary within the deadline. It forbids Gate 2 and requires a
separately authorized retry.

`STOP` applies to any safety or engineering failure, including:

- any production, unapproved Testnet, custom endpoint, or real credential
  exposure;
- duplicated order, fill, fee, or funding accounting;
- unexplained order, trade, position, balance, or protection state;
- wrong-side or above-cap exposure;
- orphan protection or non-flat final account;
- restart recovery requiring old process memory;
- ledger replay mismatch;
- unresolved P0 or P1;
- failure to finish within the frozen deadline for reasons other than the
  narrow external conditions allowed for `INCONCLUSIVE`.

Neither `STOP` nor `INCONCLUSIVE` authorizes protocol changes, Gate 2, alpha,
or real-money work.

## 14. Required evidence and machine verdict

The independent arbiter writes `gate_1b_verdict.json` containing at least:

- verdict and reason codes;
- frozen start, completion, and effective duration;
- repository, branch, protocol commit, tested commit, and dirty status;
- software versions and source/config hashes;
- credential redaction and endpoint-allowlist results;
- preflight and account-state results;
- scenario and forced-restart results;
- order, fill, fee, funding, position, balance, and protection summaries;
- ledger replay hash and final-flat proof;
- command exit codes and evidence paths;
- unresolved P0/P1/P2;
- WorkBuddy review identity and result.

Raw responses must be sanitized before retention. Detached SHA-256 files cover
the machine verdict and final evidence manifest. Missing, skipped, altered,
secret-bearing, or checksum-invalid evidence cannot PASS.

## 15. Independent review

WorkBuddy is read-only. It may inspect the frozen protocol, source, sanitized
raw evidence, venue/local reconciliation, hashes, and final account proof. It
must not write alternative code, connect independently, use credentials, alter
the account, or approve from Codex summaries alone. PASS requires WorkBuddy
`P0=0` and `P1=0` before the deadline.

## 16. Sole next action

- `PASS`: freeze Gate 1B; separately preregister the ten-instrument capacity
  gate before any Gate 2 hypothesis work.
- `INCONCLUSIVE`: freeze the evidence and wait for explicit authorization of a
  newly versioned retry.
- `STOP`: freeze the failure evidence and present the smallest redesign; do
  not connect again without explicit authorization.

## 17. Frozen primary references

- NautilusTrader Binance integration:
  `https://nautilustrader.io/docs/latest/integrations/binance/`
- NautilusTrader `v1.230.0` Futures Demo example:
  `examples/live/binance/binance_futures_demo_exec_tester.py` in
  `nautechsystems/nautilus_trader`.
- The pinned adapter resolves USD-M Demo HTTP and stream traffic to
  `demo-fapi.binance.com` and `demo-fstream.binance.com`, and its authenticated
  WebSocket API to `testnet.binancefuture.com/ws-fapi/v1`.
