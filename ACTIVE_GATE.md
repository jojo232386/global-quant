# Current Active Gate

Updated: `2026-08-06T15:05:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.4`

## Status

`READY`

Protocol: `protocols/NT_GATE_1B_V1_4.md`

Parent closed tag: `nt-gate-1b-v1.3-stop`

Frozen start: `2026-08-06T15:15:00+08:00`

Wall-clock stop deadline: `2026-08-07T03:15:00+08:00`

Effective work limit: `12 hours`

Parent PASS tag:

- `nt-gate-1a-v1.2-pass`

## Last closed result

`NT-GATE-1B v1.3` is frozen as `STOP` at `nt-gate-1b-v1.3-stop`. It stopped
before network access, and the affected Demo key was deleted. Signed requests
and orders: `0`.

## Exclusions

- no production or Testnet endpoint;
- no real-money credential or account;
- no historical market research or alpha;
- no Gate 2;
- no stopped legacy strategy;
- no A-share project access.

## Sole next action

Commit and annotated-tag the v1.4 protocol before its frozen start. Only then
generate the new local Ed25519 key pair. Do not connect, query the account,
start the Demo node, submit an order, or enter Gate 2 before the tag.
