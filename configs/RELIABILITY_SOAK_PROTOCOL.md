# Reliability Soak Protocol (48–72h, dry-run)

> `DRY_RUN_ONLY = TRUE`

This protocol defines the promoted-layout reliability run and the evidence
package it must produce. The soak exercises the runtime and the control
plane; it does not test alpha and it does not authorize live trading.
Completion of the soak removes exactly one blocker from
`configs/LIVE_READINESS.md` and nothing more.

## Scope and layout

- Single runtime: the committed `docker-compose.yml`, image digest pinned.
- Committed dry-run config and `LiveExecutionCanaryStrategy` — the promoted
  layout, no local substitutions.
- One pair (`ETH/USDT:USDT`), isolated margin, 1x, one open trade maximum,
  25 USDT dry-run stake.
- Control plane in the loop: `scripts/gmaq-control`, audit journal enabled.
- Execution wrapper: `scripts/reliability-soak <48-72>` drives the scheduled
  exercises; this protocol defines what counts as evidence.
- Duration: 48–72 hours continuous. A run shorter than 48h is not evidence.

## Entry gates (before starting the clock)

1. `./scripts/gmaq-control preflight` verdict PASS.
2. `./scripts/gmaq-control audit verify` chain ok.
3. `./scripts/gmaq-exchange-preflight` verdict PASS_PUBLIC, same day.
4. `./scripts/gmaq-liquidity` verdict PASS, same day.
5. Kill switch rehearsed once from a running state: `./scripts/gmaq-control
   kill` stops the runtime, then recovery to DISARMED per
   `configs/CONTROL_PLANE.md`.
6. Entry record appended to the audit journal.

## Scheduled exercises

Each exercise appends an audit record and is listed in the evidence table.

| # | exercise | frequency | expected evidence |
|---|---|---|---|
| E1 | health sample | every 6h | HEALTHY verdict records |
| E2 | reconcile | every 6h | OK verdicts, unique identities |
| E3 | pause / resume | 2x per 24h | state transitions in journal |
| E4 | clean restart | 1x per 24h | recovery with zero duplicate trades/orders |
| E5 | short network interruption | 1x | runtime survives, no unaccounted state |
| E6 | stopped-database backup/restore | 1x | restored runtime equals pre-backup state |
| E7 | FreqUI reconnection | 2x | UI reachable at 127.0.0.1:8080 after reconnect |
| E8 | duplicate identity scan | every 12h | no repeated trade/order ids in the journal |

## Exit criteria (all mandatory)

- Final `forceexit all` in dry-run followed by a reconcile with **zero open
  positions and zero open orders**.
- No duplicate trade/order identities across the whole run.
- Audit hash chain intact from entry to exit.
- Every scheduled exercise has a matching evidence record; a missed exercise
  fails the run unless re-scheduled and completed within the window.
- A killed runtime at any point restarts the soak clock.

## Evidence package

Produce one dated folder under `user_data/audit/soak-<UTC date>/` containing:

- `manifest.json`: run id, start/end UTC, repo SHA, image digest, entry
  gate verdicts, exercise table with per-exercise verdicts.
- `audit-journal.jsonl`: the exact journal segment for the run window.
- `health-samples.jsonl`: E1 verdict records.
- `reconcile-records.jsonl`: E2 verdict records.
- `restart-recovery.md`: E4/E5 evidence and any anomalies observed.
- `backup-restore.md`: E6 steps and comparison result.
- `final-exit.json`: the final reconcile record proving zero positions and
  zero open orders.
- `verdict.md`: PASS / FAIL with the exact failing criterion when failed.

## Acceptance

- A PASS removes the "reliability run not completed" blocker only.
- Live remains BLOCKED by every item listed in `configs/LIVE_READINESS.md`
  that is still unverified, especially the authenticated account checks.
- This protocol does not authorize live trading.
