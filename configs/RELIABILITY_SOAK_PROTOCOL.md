# Reliability Observation Protocol (48–72h post-canary, dry-run)

> `DRY_RUN_ONLY = TRUE`

This protocol defines a bounded canary followed by a DISARMED reliability
observation and the evidence package it must produce. It exercises the runtime
and control plane; it does not test alpha and it does not authorize live
trading. Its `OBSERVATION_ONLY_PASS` verdict is deliberately distinct from the
legacy continuously-ARMED 48-hour protocol and does not automatically remove a
blocker from `configs/LIVE_READINESS.md`.

## Scope and layout

- Single runtime: the committed `docker-compose.yml`, image digest pinned.
- Committed dry-run config and `LiveExecutionCanaryStrategy` — the promoted
  layout, no local substitutions.
- One pair (`ETH/USDT:USDT`), isolated margin, 1x, one open trade maximum,
  25 USDT dry-run stake.
- Control plane in the loop: `scripts/gmaq-control`, audit journal enabled.
- Execution wrapper: `scripts/reliability-soak <48-72> --authorization-id ID`
  drives one canary authorization and the scheduled DISARMED observations. The
  explicit id authorizes this dry-run canary only.
- Non-mutating validation: `scripts/reliability-soak --smoke` proves the exact
  runtime binding, health, audit chain and zero-state reconciliation while the
  entry gate remains `DISARMED`. It never counts as 48–72h evidence.
- Authorization: exactly one ARM is permitted. It ends immediately after one
  complete canary entry/exit lifecycle; there is no periodic authorization
  refresh or re-ARM during observation.
- Duration: 48–72 hours continuous after the canary is complete and the gate is
  DISARMED. A shorter observation does not receive `OBSERVATION_ONLY_PASS`.
- A monitor-loop gap over five minutes or a backward host-clock jump fails the
  run; suspended host time never counts toward continuous duration.

## Entry gates (before starting the clock)

1. Exact sanitized runtime manifest from `scripts/gmaq-runtime-manifest`, with
   candidate/config/container/image/run identity verdict `EXACT_MATCH`.
2. `./scripts/gmaq-control preflight` verdict PASS.
3. `./scripts/gmaq-control audit verify` chain ok.
4. `./scripts/gmaq-exchange-preflight` verdict PASS_PUBLIC, same day.
5. `./scripts/gmaq-liquidity` verdict PASS, same day.
6. Kill switch rehearsed once from a running state: `./scripts/gmaq-control
   kill` stops the runtime, then recovery to DISARMED per
   `configs/CONTROL_PLANE.md`.
7. Entry record appended to the audit journal. After one complete canary
   lifecycle, the runner immediately records a DISARM and only then starts the
   observation clock. Both pre-canary preflights require trustworthy clock
   evidence; unavailable time evidence is not treated as confirmed drift.

## Scheduled exercises

Each exercise appends an audit record and is listed in the evidence table.

| # | exercise | frequency | expected evidence |
|---|---|---|---|
| E1 | health sample | every 6h | HEALTHY verdict records |
| E2 | reconcile | every 6h | MATCH verdicts, unique identities |
| E4 | clean restart while DISARMED | 1x per 24h | recovery with zero duplicate trades/orders |
| E5 | short network interruption while DISARMED | 1x | runtime survives, no unaccounted state |
| E6 | stopped-database backup/restore | 1x | restored runtime equals pre-backup state |
| E7 | API reconnection | 2x | bound API ping reachable after reconnect |
| E8 | duplicate identity and audit scan | every 12h | database trade/order ids and audit sequences remain unique |

Health, reconciliation, audit verification and duplicate checks continue for
the whole observation. Because the entry gate is DISARMED, health does not call
the Binance time endpoint; fresh trusted clock evidence remains mandatory
before any later ARM outside this observation.

## Exit criteria (all mandatory)

- Final controlled exit in dry-run followed by a reconcile with **zero open
  positions and zero open orders**.
- The flat database identity baseline captured immediately before E0 must be
  followed by exactly the authorized canary phase producing at least one fully
  closed `LiveExecutionCanaryStrategy` dry-run trade with closed buy and sell
  orders. A zero-trade run fails, and the observation cannot start until this
  lifecycle is complete and the gate is DISARMED.
- No duplicate trade/order identities across the whole run.
- Audit hash chain intact from entry to exit.
- Every scheduled exercise has a matching evidence record; a missed exercise
  fails the run unless re-scheduled and completed within the window.
- A killed runtime at any point restarts the soak clock.

## Evidence package

Produce one dated folder under `user_data/audit/soak-<UTC date>/` containing:

- `manifest.json`: run id, repo/tree/config/compose SHA, image digest, exact
  container and volume bindings, and a statement that it contains no secrets;
  admission re-derives the committed tree/config/compose/image contract.
- `events.jsonl`: entry gates and exercise table with per-exercise verdicts.
- Supporting entry-gate records include start/end UTC and the explicit dry-run
  authorization id only in the append-only audit journal.
- `audit-journal.jsonl`: the exact journal segment for the run window.
- `audit-start-anchor.json` and `initial-audit-verify.json`: the verified
  predecessor hash and record count anchoring that segment to the pre-soak chain.
- `health-samples.jsonl`: timestamped E1 verdict records at the six-hour cadence.
- `preflight.json` and `preflight-after-kill.json`: the complete five-sample
  clock evidence and every other check before the only ARM.
- `reconcile-records.jsonl`: timestamped E2 verdict records at the six-hour cadence.
- `audit-verify-records.jsonl`: periodic audit-chain verification during the
  DISARMED observation.
- `trade-baseline.json` and `trade-lifecycle.json`: the pre-E0 database
  identity boundary and proof of a complete post-baseline canary round trip.
- `backup-restore.md`: E6 steps and comparison result.
- `final-exit.json`: the final reconcile record proving zero positions and
  zero open orders.
- `final-audit-verify.json`: the final append-only audit-chain verification.
- `verdict.md`: PASS / FAIL with the exact failing criterion when failed.

## Acceptance

- `OBSERVATION_ONLY_PASS` proves only the stated canary plus DISARMED observation
  contract. It is not the legacy continuously-ARMED 48-hour `PASS` and does not
  by itself remove the "reliability run not completed" blocker.
- `SMOKE_ONLY_PASS` does not remove that blocker and does not authorize entries.
- Live remains BLOCKED by every item listed in `configs/LIVE_READINESS.md`
  that is still unverified, especially the authenticated account checks.
- This protocol does not authorize live trading.
