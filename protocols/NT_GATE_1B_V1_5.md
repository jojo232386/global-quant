# NT-GATE-1B v1.5 Protocol-Only Freeze

Protocol version: `1.5`

Status at freeze: `PROTOCOL_ONLY`

Authorized scope: credential-free protocol design, offline readiness implementation,
mock/static validation, tests, and independent read-only review.

Base commit: `bf61a3cb1838e9ff4cd59dff0f0e03c2bd782fe7`

Protocol tag: `nt-gate-1b-v1.5-protocol`

Parent closed versions:

- `NT-GATE-1B v1.3`: permanently `STOP` at `nt-gate-1b-v1.3-stop`;
- `NT-GATE-1B v1.4`: permanently `STOP` at `nt-gate-1b-v1.4-stop`.

This freeze authorizes no credential creation, credential read, authenticated request,
account query, order, cancel, funding action, position mutation, paper trading, or live
trading. A later credential-bearing stage requires separate explicit authorization.

## 1. Objective

Freeze the smallest v1.5 contract which keeps credential-bearing activity outside agent
control and proves the repository is ready for a separately authorized, human-operated,
signed read-only Demo preflight.

The protocol-only stage may create machine-readable readiness evidence. That evidence
must prove only repository and invocation invariants. It cannot claim authentication,
venue behavior, account cleanliness, profitability, or trading readiness.

## 2. Failure evidence inherited from v1.3 and v1.4

Version 1.3 stopped after a Demo API-key identifier entered agent context during browser
diagnosis. The private-key value did not enter agent context, no signed request was sent,
and the affected Demo key was deleted.

Version 1.4 added guarded local-file Ed25519 loading and passed its offline suite, then
stopped at its frozen wall-clock deadline before authenticated preflight. No credential
was registered and no authenticated request or order occurred.

Version 1.5 does not reopen either version. It removes the agent-controlled credential
session from the authorized execution path.

## 3. Credential isolation boundary

Any future v1.5 credential-bearing preflight must follow all of these rules:

1. A human operator launches the credential-bearing process from a Terminal session
   which is not controlled, recorded, or observed by an AI agent or browser automation.
2. The agent must not launch, drive, inspect, transcribe, screenshot, record, or attach to
   that Terminal session.
3. No credential value may enter chat, agent context, clipboard automation, browser DOM
   extraction, command arguments, parent environment, shell history, logs, Git, evidence,
   or review material.
4. The parent environment must contain none of the recognized Binance Demo, Testnet, or
   production credential variable names.
5. The API key may be entered only through a non-echoing local terminal prompt. An
   Ed25519 private key may be read only by the short-lived human-operated process from an
   owner-only regular file using the existing v1.4 file guards.
6. The credential-bearing process may emit only sanitized evidence. It must terminate
   after the read-only preflight.
7. The agent may inspect sanitized evidence only after the human-operated process exits.
8. Exposure of a credential value or identifier to an agent is an immediate `STOP`.

Software cannot prove that a person, rather than an agent, controls a terminal. The
human-operated session rule is an authorization boundary, not a claimed OS sandbox.

## 4. Protocol-only implementation contract

### Objective

Add a standalone offline readiness command which checks protocol integrity and rejects
credential-bearing environments without reading credential values.

### Allowed files

- `protocols/NT_GATE_1B_V1_5.md`;
- `src/global_quant/gate1b/protocol_readiness.py`;
- `scripts/run_gate_1b_v1_5_readiness.py`;
- `tests/unit/test_gate1b_v1_5_readiness.py`;
- `reviews/NT_GATE_1B_V1_5_PROTOCOL_FINAL.md` after implementation and review.

### Forbidden files and state

- all Gate 1A source, tests, protocols, reviews, and evidence;
- all v1.3/v1.4 protocols, reviews, tags, commits, and runtime evidence;
- `ACTIVE_GATE.md`, `CHECKPOINT_2026-08-07.md`, and `README.md`;
- existing Gate 1B config, runtime, safety, prompt, preflight, and runner modules;
- recovery refs, recovery bundles, recovery copies, and linked Gate 1A worktrees;
- the three wb-worker recovery candidates in the frozen STOP worktree;
- dependencies and `uv.lock`.

### Invariants

- The readiness command accepts only an evidence directory. It accepts no credential,
  private-key path, endpoint, account, instrument, order, or network option.
- It inspects environment keys only and never reads values.
- Any recognized Binance credential key causes `STOP` before Git or filesystem evidence
  collection.
- It performs no DNS, socket, HTTP, WebSocket, authenticated, exchange, account, or order
  operation.
- It verifies that the annotated protocol tag exists, is an ancestor of the tested
  commit, and contains the exact current protocol bytes.
- Readiness evidence contains commit/tag/hash metadata and explicit zero-impact fields.
- The command does not import or invoke the credential prompt, signed preflight, runtime,
  or execution node.

### Acceptance criteria

1. The protocol is committed and annotated-tagged before implementation.
2. Targeted tests prove environment-key rejection without reading values, frozen protocol
   integrity, fail-closed Git errors, atomic sanitized evidence, and zero network calls.
3. The credential-free readiness command returns `PASS` against the completed v1.5 branch.
4. The complete existing test suite passes.
5. Lint, changed-file format, lock consistency, build/install smoke, `git diff --check`,
   allowed-path audit, and frozen-evidence zero-diff pass.
6. Independent review reports no P0 or P1, or the reviewer is unavailable and Codex
   records a complete machine-evidence review.

### Stop conditions

- a credential value, credential file content, or real credential identifier reaches any
  agent or tool;
- any authenticated or trading request occurs;
- implementation requires modifying a forbidden file or dependency;
- protocol integrity cannot be checked without weakening the frozen boundary;
- a P0/P1 requires architectural expansion outside the allowed files.

## 5. Offline readiness evidence contract

On `PASS`, the command writes one JSON object with at least:

- `status = PASS`;
- `gate = NT-GATE-1B` and `protocol_version = 1.5`;
- `mode = PROTOCOL_READINESS_ONLY`;
- protocol tag, protocol commit, tested commit, and protocol SHA-256;
- `credential_environment_empty = true` and `credentials_read = false`;
- `network_accessed = false` and `authenticated_request_sent = false`;
- zero orders, cancels, fills, fees, funding events, and position changes;
- `agent_credential_access_allowed = false`;
- `next_action = WAIT_FOR_EXPLICIT_CREDENTIAL_AUTHORIZATION`.

Failures write no raw exception text, environment value, command output, or credential
material. They return a stable reason code and fail closed.

## 6. Review boundary

The independent reviewer may read the branch, frozen protocol, source, tests, and
credential-free evidence. The reviewer must not invoke a credential prompt, inspect a
credential location, connect to Binance, or modify the worktree.

Protocol-only `PASS` requires `P0=0` and `P1=0`. It means the repository is ready to ask
for a separate credential authorization. It does not authorize that credential stage.

## 7. Sole next action

Complete the allowed offline implementation, tests, readiness evidence, scope audit, and
independent review. Then stop at either:

- `PASS_READY_FOR_CREDENTIAL_AUTHORIZATION`; or
- `BLOCKED` with the recorded reason.

After `PASS_READY_FOR_CREDENTIAL_AUTHORIZATION`, wait for the user to decide whether to
authorize a bounded human-operated credential session and signed read-only Demo preflight.
