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
profitability, or live readiness. Gate 1B remains forbidden until separately
authorized; no alpha, Demo, exchange, or live work is active.

## GitHub

The private personal remote is
[`jojo232386/global-quant`](https://github.com/jojo232386/global-quant).
The local repository remains the execution source of truth; pushed commits are
the off-machine audit trail.
