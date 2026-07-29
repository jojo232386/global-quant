# Current Active Gate

Updated: `2026-07-30T07:20:00+08:00`

## Project

`global-quant`

Repository: `/Users/ASUS/Desktop/global-quant`

Legacy archive: `/Users/ASUS/Desktop/trading-assistant`

## Gate

`NT-GATE-1A`

## Status

`IN_PROGRESS`

Protocol: `protocols/NT_GATE_1A.md`

Frozen protocol commits:

- `617a5dafe201f7ca56c1148295753a8e57f8cbed`
- `8638c1b0003b12215d01a9d867fc82dd39b5e224` (pre-implementation review fix)

Effective work limit: `12 hours`

Wall-clock stop deadline: `2026-07-30T18:26:32+08:00`

## Sole objective

Verify offline no-alpha strategy-source parity, append-only ledger replay,
idempotent order handling, forced-crash recovery, and deterministic business
state.

## Exclusions

- no network;
- no credential;
- no exchange;
- no alpha or market data;
- no Gate 1B;
- no stopped legacy strategy;
- no A-share project access.

## Current evidence

- shared Nautilus `Strategy` delegates to the shared event-sourced coordinator;
- append-only hash-chained ledger and atomic checkpoint;
- twelve scenario tests;
- eleven crash boundaries, including real `SIGKILL`;
- process-level macOS network sandbox and Python call-stack guard;
- machine arbiter that fails closed.

## Sole next action

Commit the implementation snapshot, then generate clean offline evidence and a
machine candidate verdict before WorkBuddy review.
