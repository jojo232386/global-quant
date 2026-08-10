# NT-GATE-1B v1.6 Mutation Protocol Final Review (Freeze)

Review date: `2026-08-10` (Asia/Shanghai)

Reviewer role: Work Buddy as implementer (executing the Codex→Work Buddy handoff after
Codex quota exhaustion). This is an implementer self-audit recorded at freeze, not an
independent read-only review for a credential-bearing runtime PASS.

Candidate base commit: `88a82b238415927e9f802e9605bfa4c5ed7f6a31`

Candidate commit (draft): `974902462ef0c3ba0db2fbbac827bbc2d4293934`

Parent protocol tag: `nt-gate-1b-v1.5-protocol` peeling to
`0c4a184c30ee2ee8bda8beaee35c1e19798eb5a9`

Parent Auth result:
`GATE1B_V1_5_AUTH_CLOSEOUT_RESULT = PASS_READY_FOR_DEMO_TRADING_AUTHORIZATION`

Protocol tag (created at this freeze): `nt-gate-1b-v1.6-protocol` (annotated, local only,
not pushed)

## Verdict

`PASS_READY_FOR_THIN_RUNNER_IMPLEMENTATION`

This verdict means only that the v1.6 mutation protocol is frozen with the user-approved
`OPTION_A` boundary and the two determinism clarifications resolved. It does not
authorize a credential-bearing session, a Demo mutation, an order, a cancel, a fill, an
account-setting change, or any production connection. The thin runner implementation and
offline tests are the next stage, to be committed only after this frozen protocol
commit/tag and without changing the tagged protocol bytes.

## User decision bound to this freeze

`OPTION_A_APPROVED`

The user explicitly approved: `ETHUSDT`, `10 USDT` maximum notional, BUY LIMIT GTX at the
fresh best bid discounted by 1%, no modify, existing ISOLATED/1x/auto-add-off required,
no account correction, and the non-zero Demo accidental-fill risk plus the bounded
exactly-owned containment path.

## Determinism clarifications resolved at freeze

### selfTradePreventionMode — frozen value: field not sent

The probe create payload and the contingency reduce-only close payload deliberately omit
`selfTradePreventionMode`. This is a recorded frozen decision, not a reliance on an
undocumented implicit default: `GTX` is post-only and cannot cross at acceptance, so
self-trade prevention cannot fire at acceptance regardless of the account-level default.
The protocol does not depend on any account-level `selfTradePreventionMode` default being
any particular value at acceptance. Resting fill risk is handled solely by section 14
containment, and any fill makes the run non-PASS independent of self-trade semantics. The
runner must not add this field to either payload. A unit test
(`test_frozen_payloads_omit_self_trade_prevention_mode`) now mechanically proves the
omission and the rejection of an added field as `UNFROZEN_ORDER_PARAMETER`.

### recvWindow — frozen value: 5000 ms

The signed `recvWindow` is frozen at `5000 ms`, reusing the v1.5 Auth-closeout verified
SIGNED-request value. It is applied identically to the probe create payload, the
contingency reduce-only close payload, and every read query parameter map. The frozen
value is recorded as `RECEIVE_WINDOW_MS = 5000` in the offline contract helper and is
bound into evidence via the canonical request reservation digests. v1.6 introduces no
new time-window strategy.

## Scope reviewed at freeze

- The v1.5 Auth closeout and protocol/tag ancestry were treated as the completed
  baseline; candidate commit `9749024` is a direct descendant of the v1.5 Auth runtime
  commit `88a82b2`.
- The candidate freezes one bounded `ETHUSDT` create-query-cancel lifecycle with no
  modify step.
- The pure offline contract helper covers account/symbol/filter/order validation, exact
  request guarding, durable attempt semantics, duplicate recovery, cleanup ownership, and
  sanitized summary validation.
- Unit tests exercise happy paths and fail-closed counterexamples without credentials or
  network I/O.
- The freeze advances the candidate status from `CANDIDATE_NOT_FROZEN` to
  `FROZEN_OPTION_A_APPROVED` and adds the formal `protocols/NT_GATE_1B_V1_6.md`, the
  `selfTradePreventionMode` omission test, and this final review.

No Auth Closeout, credential prompt, Binance authenticated request, Demo mutation, order,
cancel, position change, or production request was executed during this freeze.

## Credential-isolation assessment

- This freeze requested no API key, private key, secret, or credential identifier.
- No credential value or identifier entered chat, agent context, command arguments,
  parent environment, shell history, Git, evidence, tracebacks, or hashes.
- The frozen protocol retains the v1.5 human-operated, agent-uncontrolled credential
  boundary with the process-lifecycle clarification for mutation evidence (child +
  credential-free supervisor attestation).
- Evidence remains local/ignored. The client order ID is protocol-generated and
  non-secret; venue order/trade IDs are retained only as domain-separated SHA-256
  representations.

## Protocol-integrity assessment

- The candidate commit `9749024` is the direct parent of this freeze commit.
- The v1.5 protocol tag `nt-gate-1b-v1.5-protocol` is an annotated tag peeling to
  `0c4a184c`, and both `0c4a184c` and `88a82b2` are ancestors of this freeze commit.
- `origin/main` remains `bf61a3cb1838e9ff4cd59dff0f0e03c2bd782fe7`; no push, merge, PR,
  release, remote tag mutation, destructive reset, rebase, `git clean`, or `git gc` was
  performed.
- The thin runner and its offline tests are committed only after this frozen protocol
  commit/tag, without changing the tagged protocol bytes.

## Findings

- `P0 = 0`
- `P1 = 0`
- `P2 = 0`
- `P3 = 0`

## Independent reviewer status

Work Buddy is the implementer of this freeze and thin runner, not an independent
reviewer. Work Buddy's self-review recorded here is a self-audit only. It does not
constitute the independent read-only review required before a credential-bearing Demo
mutation runtime PASS. Per the protocol acceptance contract, a credential-bearing
mutation runtime requires a stricter mandatory independent review. If that independent
reviewer is unavailable (Codex quota not yet restored, and no other independent reviewer
available), the implementer stops at
`PASS_READY_FOR_CREDENTIAL_BEARING_DEMO_RUN_PENDING_INDEPENDENT_REVIEW` and waits. Work
Buddy must not fabricate independent reviewer approval, and the v1.5 protocol-only
fallback must not be applied to a mutating run.

## Sole next action

Implement and offline-test the thin Demo lifecycle runner that serializes this frozen
protocol, then stop at the credential-bearing Demo gate pending independent review. Do
not request credentials, do not perform a Demo mutation, and do not enter a
credential-bearing session without another explicit, separate authorization.
