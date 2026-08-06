# Current Active Gate

Updated: `2026-08-06T09:03:35+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.2`

## Status

`CLOSED — INCONCLUSIVE`

Protocol: `protocols/NT_GATE_1B_V1_2.md`

Parent PASS tag:

- `nt-gate-1a-v1.2-pass`

Effective work limit: `12 hours`

Frozen start: `2026-08-06T08:20:00+08:00`

Wall-clock stop deadline: `2026-08-06T20:20:00+08:00`

## Sole objective

Falsify-test the shared Strategy, coordinator, ledger, and recovery model
against actual Binance USD-M Futures Demo acknowledgements, protection orders,
cancel/fill races, restart reconciliation, fees, and funding.

## Exclusions

- no production or Testnet endpoint;
- no real-money credential or account;
- no historical market research or alpha;
- no Gate 2;
- no stopped legacy strategy;
- no A-share project access.

## Final state

- machine verdict: `INCONCLUSIVE`;
- sole reason: `MISSING_DEMO_CREDENTIALS`;
- tested commit: `c163b1588073559403e3009f3063066d66773620`;
- final offline result: `192 passed`, with network denied;
- public Demo probe used no credentials and submitted no order;
- authenticated Demo connection, mandatory scenario matrix, restart matrix,
  funding reconciliation, and final-flat proof were not run;
- WorkBuddy review was not obtained; Qwen ACP produced only a partial review
  before its runtime failed, and is not approval-equivalent;
- no Binance account was queried and no order, fill, fee, funding event,
  position, or balance change occurred.

Curated evidence: `evidence/nt_gate_1b_v1_2/`

## Sole next action

None is active. Preserve this result. A retry requires explicit user
authorization, a new protocol version, a new frozen start, and Demo-only
credentials. Do not enter Gate 2, alpha research, Demo execution, or real-money
work from this verdict.
