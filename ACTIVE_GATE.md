# Current Active Gate

Updated: `2026-08-06T06:42:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1A v1.2`

## Status

`READY`

Protocol: `protocols/NT_GATE_1A_V1_2.md`

Parent STOP tag:

- `nt-gate-1a-v1.1-stop`

Effective work limit: `12 hours`

Frozen start: `2026-08-06T07:00:00+08:00`

Wall-clock stop deadline: `2026-08-06T19:00:00+08:00`

## Sole objective

Repair and falsify-test the two v1.1 production Strategy callback P1 findings:
durable raw-fill persistence before apply, and durable fail-closed handling for
unknown fills.

## Exclusions

- no network;
- no credential;
- no exchange;
- no alpha or market data;
- no Gate 1B;
- no stopped legacy strategy;
- no A-share project access.

## Frozen evidence contract

- protocol and callback oracle committed before the start;
- real Strategy callback and real `SIGKILL` boundary;
- exact-once recovery and durable unknown-fill lockout;
- freshly sampled tool versions;
- all retained v1.1 offline tests and evidence;
- WorkBuddy review and final machine verdict before the deadline.

## Sole next action

Freeze the v1.2 protocol and callback oracle in Git before the start. Do not
edit implementation until the frozen start time.
