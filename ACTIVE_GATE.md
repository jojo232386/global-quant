# Current Active Gate

Updated: `2026-08-06T13:40:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.3`

## Status

`ACTIVE`

Protocol: `protocols/NT_GATE_1B_V1_3.md`

Parent closed tag: `nt-gate-1b-v1.2-inconclusive`

Frozen start: `2026-08-06T13:40:00+08:00`

Wall-clock stop deadline: `2026-08-07T01:40:00+08:00`

Effective work limit: `12 hours`

Parent PASS tag:

- `nt-gate-1a-v1.2-pass`

## Last closed result

`NT-GATE-1B v1.2` remains `INCONCLUSIVE/MISSING_DEMO_CREDENTIALS` at tag
`nt-gate-1b-v1.2-inconclusive`.

## Exclusions

- no production or Testnet endpoint;
- no real-money credential or account;
- no historical market research or alpha;
- no Gate 2;
- no stopped legacy strategy;
- no A-share project access.

## Activation readiness

- main Binance login was confirmed;
- the official `demo.binance.com` trading and Demo API Management pages render;
- a Demo-only API key was created by the user;
- the key and secret were not read, copied, logged, hashed, or committed;
- the interactive in-process prompt path passed six focused tests and the full
  offline suite passed with `198 passed`;
- live and Testnet Binance variables are absent;
- no signed request, account query, node connection, or order occurred;
- the protocol must be committed and tagged before the frozen start.

## Sole next action

Commit and annotate-tag the v1.3 protocol before its frozen start. Then run
only the interactive read-only signed preflight. Do not start the Demo node or
submit an order unless the preflight returns PASS and its sanitized evidence
passes review.
