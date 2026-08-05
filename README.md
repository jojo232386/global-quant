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

`NT-GATE-1A v1.2`: `READY`, frozen to start at
`2026-08-06T07:00:00+08:00` and stop at `2026-08-06T19:00:00+08:00`.

The gate protocol is frozen in
[`protocols/NT_GATE_1A.md`](protocols/NT_GATE_1A.md). Only this gate may be
active. The first implementation produced strong offline evidence but failed
final review because two real-Strategy persistence paths were not covered and
the fixed review deadline expired. See
[`reviews/GATE1A_FINAL_REVIEW.md`](reviews/GATE1A_FINAL_REVIEW.md).

Version 1.1 remains frozen as `STOP` under tag
`nt-gate-1a-v1.1-stop`. Version 1.2 is authorized only to repair and test the
two real Strategy callback P1 findings. Gate 1B remains forbidden; no alpha,
Demo, exchange, or live work is active.

## GitHub

The private personal remote is
[`jojo232386/global-quant`](https://github.com/jojo232386/global-quant).
The local repository remains the execution source of truth; pushed commits are
the off-machine audit trail.
