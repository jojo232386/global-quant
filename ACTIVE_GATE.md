# Current Active Gate

Updated: `2026-08-06T07:55:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1B v1.0`

## Status

`READY`

Protocol: `protocols/NT_GATE_1B_V1_0.md`

Parent PASS tag:

- `nt-gate-1a-v1.2-pass`

Effective work limit: `12 hours`

Frozen start: `2026-08-06T08:00:00+08:00`

Wall-clock stop deadline: `2026-08-06T20:00:00+08:00`

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
- all Demo and live Binance credential variables were absent at protocol draft
  time;
- no Binance connection or order has occurred under Gate 1B.

## Sole next action

Commit and tag this protocol before the frozen start. Then implement and run
only the bounded Demo safety and execution matrix. Missing Demo credentials may
produce only `INCONCLUSIVE`; it may not be bypassed with production or Testnet
credentials.
