# NT-GATE-1A Pre-implementation Review

Reviewed: `2026-07-30`

Scope: protocol only; no production implementation, network, credential,
exchange, old strategy, or A-share access.

## Result

`CONDITIONALLY_APPROVED`

P0: `0`

P1 found: `5`

P2 found: `4`

## P1 findings and disposition

1. Durability ordering around decisions, intents, external events, projections,
   and checkpoints was underspecified.
   - Resolved in protocol 1.1 with a write-ahead and `fsync` contract.
2. Four high-risk forced-crash boundaries were absent.
   - Resolved by expanding the matrix from 7 to 11 boundaries.
3. Ledger durability, decimal precision, dedupe, and integrity metadata were
   underspecified.
   - Resolved with schema versioning, monotonic sequence, source IDs, dedupe
     keys, a SHA-256 hash chain, settlement currency, frozen precision, and
     atomic checkpoints.
4. A machine-readable verdict was not necessarily machine-decided.
   - Resolved by requiring an independent fail-closed arbiter. Test and strategy
     code cannot write `PASS`.
5. Python socket replacement did not prove process-level isolation.
   - Resolved by requiring a macOS process sandbox plus parent, child, DNS,
     IPv4, and IPv6 probes.

## Preserved P2

- Protection quantity must track partial fills.
- Protection sibling fill/cancel races require a subcase.
- The original wall-clock deadline remains in force.
- A ten-instrument capacity test is deferred until after Gate 1B and before
  Gate 2.

## Binance proof boundary

Gate 1A remains a local state-machine and recovery test. It does not prove
Binance conditional-order, cancel-race, hedge-mode, reduce-only, funding,
reconnect, liquidation, ADL, or reconciliation semantics.

