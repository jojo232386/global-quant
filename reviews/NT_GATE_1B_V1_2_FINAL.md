INCONCLUSIVE | 0 orders | 0 fills | no authenticated Demo connection | do not enter Gate 2

# NT-GATE-1B v1.2 Final Decision

Completed: `2026-08-06T09:03:35+08:00`

## Decision

The machine arbiter returned `INCONCLUSIVE` with the sole reason code
`MISSING_DEMO_CREDENTIALS`.

This is the frozen protocol's external-precondition outcome. It is neither a
PASS nor a safety/engineering STOP. It does not authorize Gate 2, alpha
research, Demo execution, or real-money activity.

## Verified

- The frozen protocol is bound to commit `35e849d` and tag
  `nt-gate-1b-v1.2-protocol`.
- The final tested implementation is
  `c163b1588073559403e3009f3063066d66773620`.
- The final system-network-denied suite completed with `192 passed` and five
  non-blocking third-party/deprecation warnings.
- Build-only resolution matched the three frozen Demo endpoints without
  reading credentials or opening a connection.
- A credential-free public probe reached the Demo USD-M HTTP endpoint, found
  BTCUSDT and ETHUSDT perpetual contracts in `TRADING` state, and measured
  `975 ms` absolute server-time skew.
- Missing credentials stopped before account authentication or node startup.
- Credential redaction and endpoint allowlist checks passed.
- The verdict and curated evidence have detached SHA-256 coverage.

## Not Verified

- No authenticated Demo account preflight ran.
- None of the ten mandatory Demo scenarios or six forced-restart scenarios ran.
- No venue acknowledgement, protection trigger, cancel/fill race, partial
  fill, commission, funding settlement, ledger replay, or final-flat proof was
  observed.
- WorkBuddy review was not obtained. The Qwen ACP review stopped after partial
  checks because its runtime failed; it is not approval-equivalent.

## Safety Outcome

- Demo connection opened: `false`.
- Account queried: `false`.
- Orders submitted/filled/canceled: `0/0/0`.
- Fills/fees/funding events: `0/0/0`.
- No credentials or account identifiers are retained in curated evidence.

## Next Action

Preserve this gate unchanged. Retry only after the user has Demo-only
credentials and explicitly authorizes a newly versioned, newly timed protocol.
Do not substitute production or legacy Testnet credentials, and do not reuse
this closed gate as if its deadline were still open.

This decision concerns execution engineering only. It says nothing about
alpha, profitability, or live readiness.
