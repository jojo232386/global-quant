# global-quant

Clean research and execution-engineering repository for non-A-share markets.

## Project boundary

- This repository is only for global and crypto quantitative work.
- It must not read, import, copy, or share code, data, environments, outputs,
  reports, credentials, commits, or conclusions with the A-share project.
- `/Users/ASUS/Desktop/trading-assistant` is a legacy research archive and the
  WorkBuddy coordination inbox. Stopped SMC, ATR, SMA, and terminal-trend
  strategies are not migrated here.
- No real-money trading is authorized.

## Gate status

`NT-GATE-1A v1.2`: `PASS`. The final machine verdict completed at
`2026-08-06T07:29:00.478740+08:00`, within the frozen 12-hour window.

The gate protocol is frozen in
[`protocols/NT_GATE_1A_V1_2.md`](protocols/NT_GATE_1A_V1_2.md). The tested
commit passed six independent `150/150` runs, thirteen restart groups, the real
Strategy post-`fsync` `SIGKILL` recovery path, durable unknown-fill lockout,
runtime version sampling, and WorkBuddy review with `P0=0` and `P1=0`. See
[`reviews/GATE1A_V1_2_FINAL_REVIEW.md`](reviews/GATE1A_V1_2_FINAL_REVIEW.md).

Version 1.1 remains frozen as `STOP` under tag
`nt-gate-1a-v1.1-stop`. Version 1.2 closes only the two real Strategy callback
P1 findings and the evidence gap. It does not prove Binance behavior, alpha,
profitability, or live readiness.

This `PASS` is permanently certified only at tag `nt-gate-1a-v1.2-pass`,
commit `297f8e0527c34ae6a220d5fc8087e0e38a6e3551`. Later commits made
additive changes to the Gate 1A source (`src/global_quant/gate1a/`) during
Gate 1B development; those changes are not covered by this certification, and
no re-certification of the later tree is claimed. Tags, not branch tips, are
the permanent certification pointers for this project.

### Gate 1B

Gate 1B was subsequently authorized and attempted through v1.2-v1.4:

- v1.2 closed `INCONCLUSIVE` (missing Demo credentials; tag
  `nt-gate-1b-v1.2-inconclusive`).
- v1.3 closed `STOP` before any network access, after a credential
  identifier (not a secret value) was caught entering agent context and
  contained (tag `nt-gate-1b-v1.3-stop`).
- v1.4 closed `STOP` at its wall-clock deadline before any authenticated
  preflight (tag `nt-gate-1b-v1.4-stop`). Version 1.4 is permanently closed
  and must never be reopened.

Across v1.2-v1.4, cumulative Binance impact remained zero: no authenticated
or signed request, account query, order, fill, fee, funding event, or
position change.

`NT-GATE-1B` is currently `STOP / PAUSED`. Any future retry requires a new
protocol version, explicit authorization, a protocol frozen and tagged before
execution, and a fresh credential pair generated only after that freeze. See
[`ACTIVE_GATE.md`](ACTIVE_GATE.md) and
[`CHECKPOINT_2026-08-07.md`](CHECKPOINT_2026-08-07.md) for current status.

## GitHub

The private personal remote is
[`jojo232386/global-quant`](https://github.com/jojo232386/global-quant).
The local repository remains the execution source of truth; pushed commits are
the off-machine audit trail.
