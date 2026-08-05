# NT-GATE-1A v1.2 WorkBuddy Independent Review Request

Perform an independent, read-only review of the global/crypto project only.

## Identity and boundaries

- Repository: `/Users/ASUS/Desktop/global-quant`
- Branch: `codex/nt-gate1a-v1.2`
- Tested commit: `51becd3c4ff28239a2524bbf814b8c5668acffb8`
- Evidence root: `/Users/ASUS/Desktop/global-quant/evidence/runtime/gate1a-v1.2-51becd3c4ff28239a2524bbf814b8c5668acffb8`
- Protocol tag: `nt-gate-1a-v1.2-protocol`
- Protocol commit: `d05651e28222b65e78a906aa5e2be62c36c76a4a`
- Frozen start: `2026-08-06T07:00:00+08:00`
- Deadline: `2026-08-06T19:00:00+08:00`

Do not inspect or modify any A-share repository, file, environment, evidence,
report, commit, or conclusion. Do not modify tracked or untracked repository
files. Do not connect to exchanges, market data, Demo/Testnet, credentials, or
live trading. Do not write an alternative implementation. You may run local,
read-only inspection and verification commands against the exact tested commit
and evidence root.

The only permitted write is the final review JSON at the path specified below.

## Required independent checks

1. Verify the repository, branch, tested commit, clean worktree, private remote,
   protocol commit/tag, protocol-before-start timing, and unchanged frozen
   callback oracle SHA-256.
2. Read the exact tested commit, not Codex's summary. Confirm the production
   `FixedTargetStrategy.on_order_filled` callback durably appends the canonical
   fill before coordinator application and does not look up an unknown order
   before durable fail-closed handling.
3. Confirm the real callback tests invoke Nautilus `OrderFilled` through the
   real Strategy callback in an independent process, use actual `SIGKILL` after
   durable inbox append, and prove exactly-once recovery plus durable unknown
   fill lockout against the preregistered oracle.
4. Confirm the formal evidence is fresh for v1.2 and bound to tested commit
   `51becd3c4ff28239a2524bbf814b8c5668acffb8`. Independently inspect command
   records, JUnit files, scenario/determinism evidence, source hashes,
   checksums, network-denial evidence, sampled tool versions, candidate
   manifest, and candidate verdict.
5. Confirm all required command exits are zero, all six full runs contain at
   least 150 passing tests with no failures/errors/skips, both real callback
   cases pass, all 13 restart groups pass, and sampled versions are generated
   from the running environment rather than hard-coded.
6. Look specifically for evidence substitution, helper-only behavior, late
   signing, dirty-tree testing, checksum gaps, duplicate/lost fill accounting,
   unknown-fill restart bypass, or any P0/P1 not identified by Codex.
7. If any mandatory item cannot be independently verified, return `STOP`, not
   an assumed PASS. P2 observations may be recorded but must not hide P0/P1.

## Required output

Write exactly one JSON object to:

`/Users/ASUS/Desktop/global-quant/evidence/runtime/gate1a-v1.2-51becd3c4ff28239a2524bbf814b8c5668acffb8/workbuddy_review.json`

Required top-level fields:

- `verdict`: `PASS` or `STOP`
- `P0`: integer count
- `P1`: integer count
- `P2`: integer count
- `tested_commit`: exact full commit SHA
- `reviewed_at`: timezone-aware ISO-8601 timestamp
- `reviewer`: `WorkBuddy`
- `read_only`: `true`
- `evidence_root`: exact evidence root
- `checks`: object with explicit per-check PASS/STOP statuses
- `findings`: array of concrete findings
- `notes`: array of limitations or observations

PASS is permitted only if `P0=0`, `P1=0`, every mandatory check is PASS, the
review completes before the frozen deadline, and the review refers to the exact
tested commit. Do not alter the candidate manifest or candidate verdict.
