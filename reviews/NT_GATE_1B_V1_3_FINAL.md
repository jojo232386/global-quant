STOP | 0 signed requests | 0 orders | Demo key deleted | do not enter Gate 2

# NT-GATE-1B v1.3 Final Decision

Completed: `2026-08-06T15:02:16+08:00`

## Decision

NT-GATE-1B v1.3 is `STOP`. The interactive preflight rejected the supplied
private-key input before network access with `INVALID_ED25519_PRIVATE_KEY`.
During diagnosis, a read-only browser extraction returned the Demo API key
identifier into agent context. The private key was never displayed or read.

The affected Demo key was deleted immediately. A post-deletion DOM check found
zero occurrences of its label and zero per-key Delete buttons. No credential
value is retained in this repository or its evidence.

## Verified

- Signed requests: `0`.
- Demo account queries: `0`.
- Orders submitted, filled, or canceled: `0/0/0`.
- Fees and funding events: `0/0`.
- The private key did not enter agent context, command arguments, environment
  variables, logs, repository files, or evidence files.
- The affected Demo API key was deleted before any retry.
- The failed-attempt commit was
  `d8a04eeb4cd94c1d4222b546c562dd92289e5068`.
- The full offline suite passed with `206 passed` and five non-blocking
  third-party/deprecation warnings.

## Not Verified

- Authentication, account state, venue acknowledgements, protection orders,
  cancel/fill races, partial fills, fees, funding, restart recovery, ledger
  replay, and final-flat behavior remain unverified.
- Qwen ACP failed at runtime and Claude ACP timed out. Neither is an independent
  approval.

## Next Action

Do not reconnect under v1.3. A retry requires a newly frozen protocol which
reads the Ed25519 private key only from an owner-only local file, never through
chat, browser extraction, clipboard, command arguments, or parent-process
environment. The user has explicitly authorized proceeding to that contained
retry. Gate 2, alpha work, Demo orders, and real-money activity remain blocked.
