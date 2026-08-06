# Current Active Gate

Updated: `2026-08-06T15:02:16+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.3`

## Status

`STOP`

Protocol: `protocols/NT_GATE_1B_V1_3.md`

Parent closed tag: `nt-gate-1b-v1.2-inconclusive`

Frozen start: `2026-08-06T13:40:00+08:00`

Wall-clock stop deadline: `2026-08-07T01:40:00+08:00`

Effective work limit: `12 hours`

Parent PASS tag:

- `nt-gate-1a-v1.2-pass`

## Last closed result

`NT-GATE-1B v1.3` stopped before network access because the private-key input
was invalid and the Demo API key identifier entered agent context during
diagnosis. The affected Demo key was deleted. Signed requests and orders: `0`.

## Exclusions

- no production or Testnet endpoint;
- no real-money credential or account;
- no historical market research or alpha;
- no Gate 2;
- no stopped legacy strategy;
- no A-share project access.

## Sole next action

Freeze the v1.3 STOP evidence and tag. Then preregister a newly timed v1.4
protocol which reads the private key only from an owner-only local file. Do not
connect, query the account, start the Demo node, submit an order, or enter Gate
2 until the new protocol is committed and tagged.
