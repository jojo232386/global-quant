# NT-GATE-1B v1.4 Frozen Protocol

Protocol version: `1.4`

Status at freeze: `READY`

Frozen start: `2026-08-06T15:15:00+08:00`

Wall-clock stop deadline: `2026-08-07T03:15:00+08:00`

Effective work limit: `12 hours`

This protocol must be committed and annotated-tagged before the frozen start.
No new key pair, Binance credential, authenticated request, Demo connection, or
Demo order is permitted before that tag. Version `1.3` remains permanently
closed as `STOP` at tag `nt-gate-1b-v1.3-stop`.

## 1. Sole objective and inherited contract

The sole objective remains falsification of the shared NautilusTrader Strategy,
order coordinator, append-only ledger, protection coordinator, and recovery
model against actual Binance USD-M Futures Demo behavior. This gate tests
execution engineering only. It does not test alpha or profitability.

Except for credential acquisition and injection, v1.4 inherits sections 1-3
and 5-17 of `protocols/NT_GATE_1B_V1_3.md` byte-for-byte by reference. The
parent protocol SHA-256 is:

`7b2277053a0a9966ceb5c31404d6ed499c87ac621c7e3745330b8825ada58fae`

The inherited mechanical sequence, endpoints, two-instrument limit, 1x
leverage, 200 USDT per-instrument cap, 400 USDT gross cap, 32-order cap,
preflight checks, protection semantics, forced-restart matrix, accounting
invariants, evidence requirements, independent-review requirement, verdict
semantics, and Gate 2 prohibition are unchanged.

## 2. Explicit exclusions

- No Binance production credential, account, endpoint, balance, or order.
- No legacy Testnet credential or general Testnet workflow.
- No A-share repository, environment, data, output, report, or credential.
- No Gate 2, alpha research, historical backtest, parameter change, daemon, or
  unattended process.
- No API key or private-key value may be read through browser text extraction,
  DOM inspection, screenshots, agent context, chat, logs, shell history,
  command arguments, Git, or evidence.
- No Demo order is permitted until the signed read-only preflight returns PASS
  and its sanitized evidence is reviewed.

## 3. New Ed25519 key-pair contract

After the protocol tag, generate a new Ed25519 key pair locally with the
system OpenSSL executable. The fixed paths are outside the repository:

- private: `/Users/ASUS/Library/Application Support/global-quant/credentials/binance-demo-v1.4-ed25519.pem`;
- public: `/Users/ASUS/Library/Application Support/global-quant/credentials/binance-demo-v1.4-ed25519.pub.pem`.

The credential directory must be owned by the current user and mode `0700`.
The private key must be an owner-owned regular file, not a symlink, with mode
`0600`. The public key may be mode `0644`. Only the public key may be inserted
into Binance Demo API Management.

The new API key label is `nautilus-demo-ed25519-v1-4`. The agent may automate
the public-key form before creation, but after creation it must not read or
extract the resulting API key. The user alone copies the API key and pastes it
into the hidden local terminal prompt. Any API-key value returned to an agent
or tool is an immediate `STOP` and requires deletion of that key.

## 4. Private-key file guard

The v1.4 runner accepts the private-key *path* as a command argument, never the
private-key content. Before prompting for the Demo API key it must:

1. refuse all Demo, live, and Testnet Binance credential variables in the
   parent environment;
2. require a real interactive terminal;
3. disable process core dumps where the platform supports it;
4. open the private key with `O_NOFOLLOW` where available;
5. verify with `fstat` that it is a regular file owned by the current user;
6. reject any group or other permission bit;
7. reject an empty file or a file larger than 16 KiB;
8. require a complete unencrypted PKCS#8 PEM bounded by
   `-----BEGIN PRIVATE KEY-----` and `-----END PRIVATE KEY-----`;
9. pass the API key and private key only through an in-process mapping to the
   signed preflight;
10. terminate the short-lived process after writing sanitized evidence.

The parent environment, child-process arguments, repository, evidence, and
stdout/stderr must not contain either credential value. Python string clearing
is not claimed as secure memory erasure; short process lifetime, disabled core
dumps, and final API-key revocation are the containment boundaries.

## 5. Signed preflight boundary

The first authenticated action is the inherited read-only preflight. It may
query server time, Futures account information, position mode, position risk,
open regular orders, open algo orders, and exchange information. It may not
submit, modify, cancel, or close anything.

An unclean account, wrong mode, missing permission, endpoint mismatch, excessive
time skew, unsupported instrument, secret-bearing output, or unexplained state
fails closed under the inherited verdict rules. No automatic cleanup occurs at
preflight.

## 6. Credential lifecycle and cleanup

- The Demo API key remains valid only for the bounded v1.4 evidence run.
- The private key stays outside Git and is never copied into the repository.
- At gate closure, normal or abnormal, revoke the Demo API key and remove the
  local private/public key files after recording only non-secret deletion
  proofs.
- If the process, browser, or agent exposes either credential value, stop before
  any further connection, revoke the key, preserve sanitized facts, and issue
  `STOP`.

## 7. PASS, INCONCLUSIVE, and STOP

The inherited v1.3 verdict semantics remain mandatory. In addition:

- `PASS` requires the private-file guard, signed preflight, all inherited Demo
  scenarios, final-flat proof, ledger replay, source/config hashes, credential
  revocation, file cleanup, and WorkBuddy `P0=0/P1=0` before the deadline.
- `INCONCLUSIVE` is limited to inherited external blockers which produce no
  adverse engineering or credential evidence.
- `STOP` includes any credential disclosure, insecure key file, signed mutation
  during preflight, unexplained account state, duplicate event, above-cap
  exposure, orphan order, non-flat closure, replay mismatch, unresolved P0/P1,
  or non-external deadline failure.

No non-PASS result authorizes Gate 2, alpha, Demo retry, or real-money work.

## 8. Sole next action

After the protocol commit and annotated tag, generate the local key pair and
verify only ownership, permissions, public-key fingerprint, and private-key
structure without printing private material. Then create the new Demo API key
from the public key. Run only the signed read-only preflight. Do not start the
Demo node or submit an order unless the preflight returns PASS and sanitized
evidence passes review.
