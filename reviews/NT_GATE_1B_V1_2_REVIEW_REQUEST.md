# NT-GATE-1B v1.2 Archived Read-Only Review Request

Status: `CLOSED WITHOUT WORKBUDDY REVIEW`

This request was not completed within the gate. It is retained as historical
evidence and must not be used for a late PASS or retrospective sign-off. The
final machine verdict is `INCONCLUSIVE/MISSING_DEMO_CREDENTIALS`.

Review only `/Users/ASUS/Desktop/global-quant`.

Do not read, mention, modify, or compare any A-share repository. Do not modify
source, tests, protocol, Git state, Binance state, credentials, or account
settings. Do not connect to Binance. Return the review in the WorkBuddy task;
do not write a replacement implementation.

## Frozen identity

- Branch: `codex/nt-gate1b-v1.2`
- Protocol commit/tag: `35e849d` / `nt-gate-1b-v1.2-protocol`
- Final tested implementation commit: `c163b15`
- Protocol: `protocols/NT_GATE_1B_V1_2.md`
- Local runtime evidence:
  `evidence/runtime/gate1b-v1.2-b06a7a71fed3/`

## Claimed current state

- `192 passed` in the final full system-network-denied test run.
- No Demo, Testnet, or live credential was present or retained.
- The build-only runner resolved only the protocol's three allowed Demo
  endpoints and made no network connection.
- Missing Demo credentials stopped before network access and produced
  `INCONCLUSIVE/MISSING_DEMO_CREDENTIALS`.
- A separate credential-free public probe reached only
  `https://demo-fapi.binance.com`, observed server-time skew below five
  seconds, and found BTCUSDT and ETHUSDT perpetuals in `TRADING` state.
- No Demo API credential was available for this gate. The ordinary API
  Management page was intentionally not used because it belongs to the live
  account.
- No order, position, balance change, cleanup, Demo node connection, Gate 2,
  alpha research, daemon, or real-money action occurred.

## Required checks

1. Verify branch, protocol tag, commit, remote, and worktree state.
2. Inspect the diff from `35e849d` through `b06a7a7` for endpoint, credential,
   risk-cap, funding-ledger, preflight, and test correctness.
3. Verify that build-only cannot read credentials or connect, and signed
   preflight cannot run without both Demo variables plus explicit arming.
4. Verify that conflicting live/Testnet variable names fail before network.
5. Verify that no secret values occur in tracked files or local evidence.
6. Verify the full test claim from command evidence or rerun tests only through
   `scripts/run_offline.sh`; never connect to Binance.
7. Inspect the public and missing-credential evidence. Do not inspect or infer
   the user's Binance identity, verification, or live-account state.
8. Verify that the archived result was correctly `INCONCLUSIVE` under section
   13 of the frozen protocol. Do not award a retrospective PASS: the mandatory
   Demo matrix was not run and the gate is closed. Use STOP only if reviewing
   the archive reveals a verified safety or engineering failure.

Return a concise result with `decision`, `P0`, `P1`, `P2`, exact findings, and
the minimum next action. Explicitly state that the review is engineering-only
and says nothing about alpha, profitability, or live readiness.
