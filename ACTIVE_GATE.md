# Current Active Gate

Updated: `2026-08-06T08:10:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.2`

## Status

`READY`

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

## Preflight state

- protocol authorized by the user;
- private repository and clean parent Gate verified;
- NautilusTrader `1.230.0` exposes `BinanceEnvironment.DEMO` and
  `BinanceAccountType.USDT_FUTURES`;
- pre-implementation review found and v1.1 records the pinned adapter's exact
  authenticated Demo WS API route at `testnet.binancefuture.com/ws-fapi/v1`;
- v1.2 raises only the non-economic order-count ceiling to 32 and freezes a
  bounded 0.10% one-instrument protection-trigger probe so the mandatory
  protection-fill claim is observable;
- all Demo and live Binance credential variables were absent at protocol draft
  time;
- no Binance connection or order has occurred under Gate 1B.

## Sole next action

Commit and tag this protocol before the frozen start. Then implement and run
only the bounded Demo safety and execution matrix. Missing Demo credentials may
produce only `INCONCLUSIVE`; it may not be bypassed with production or Testnet
credentials.
