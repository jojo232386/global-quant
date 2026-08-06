# NT-GATE-1B v1.3 Draft Retry Protocol

Protocol version: `1.3-draft`

Status: `SUPERSEDED BY NT_GATE_1B_V1_3.md`

Prepared: `2026-08-06`

Frozen start: `NOT SET`

Stop deadline: `NOT SET`

This document does not authorize a Binance connection, credential use, order,
or Gate 2 work. The 12-hour gate clock starts only after all activation
prerequisites pass and the final protocol is committed and annotated-tagged.

## 1. Reason for retry

`NT-GATE-1B v1.2` remains permanently closed as
`INCONCLUSIVE/MISSING_DEMO_CREDENTIALS` at tag
`nt-gate-1b-v1.2-inconclusive`.

Version 1.3 may retry only the execution-engineering evidence that v1.2 could
not collect. It may not reinterpret, overwrite, or retrospectively upgrade the
v1.2 verdict.

## 2. Activation prerequisites

All items must be true before this draft can be frozen:

1. The official `https://demo.binance.com/` portal renders for
   the logged-in account and exposes Demo API Management.
2. A Demo-only API key exists. A live-account or legacy Testnet key is not an
   acceptable substitute.
3. The runtime can see only `BINANCE_DEMO_API_KEY` and
   `BINANCE_DEMO_API_SECRET`; all live and Testnet Binance variables are absent.
4. The credential injection procedure is ephemeral, does not write a `.env`
   file, does not place secrets in command arguments, and does not log, hash,
   commit, or echo either value.
5. The Demo USD-M account is expected to be flat with no open regular or algo
   orders. The signed preflight must verify this without cleanup.
6. The repository is clean and based on `nt-gate-1b-v1.2-inconclusive`.
7. The final v1.3 protocol has a new frozen start, deadline, protocol commit,
   and annotated protocol tag.

If the Demo portal is unavailable, the project remains between gates. It does
not start the 12-hour clock and does not authorize a fallback to live or
Testnet credentials.

## 3. Frozen intent

Unless changed before activation and explicitly reviewed, v1.3 inherits the
v1.2 execution contract without economic expansion:

- same `FixedTargetStrategy`, coordinator, append-only ledger, and recovery
  model;
- Binance `DEMO` plus `USDT_FUTURES` only;
- BTCUSDT and ETHUSDT only;
- 1x leverage;
- maximum `200 USDT` absolute notional per instrument;
- maximum `400 USDT` aggregate gross notional;
- maximum 32 submitted orders for the entire gate;
- same ten mandatory Demo scenarios and six forced-restart scenarios;
- final flat account with no regular or algo orders;
- WorkBuddy read-only review required for PASS;
- no alpha, parameter search, historical research, daemon, Gate 2, real money,
  or A-share access.

## 4. Credential boundary

The gate runner must fail before DNS or socket creation unless both Demo
variables are present and every live/Testnet Binance variable is absent.

Secrets must never appear in:

- tracked or untracked project files;
- shell history or command arguments;
- process listings;
- logs, evidence, tracebacks, hashes, screenshots, clipboard captures, review
  prompts, or chat messages.

Credential creation or persistent storage remains a user-controlled action.
Codex may inspect only presence booleans and sanitized signed-preflight output.

## 5. Verdict semantics

The final protocol will retain v1.2 verdict precedence:

- `PASS`: every mandatory Demo scenario, restart, accounting invariant,
  final-flat proof, evidence check, and WorkBuddy review passes within time.
- `INCONCLUSIVE`: only a frozen external condition such as Demo outage, no
  partial fill, no funding boundary, or exchange minimum above the fixed cap.
- `STOP`: any safety or engineering failure, secret exposure, endpoint drift,
  duplicate economic event, unexplained venue state, non-flat final state,
  unresolved P0/P1, or internal overrun.

No verdict authorizes alpha or real-money trading.

## 6. Current readiness evidence

- Main Binance login: confirmed in the user's Chrome session.
- Official Demo portal: returned a generic error page on `2026-08-06`.
- Demo API credentials in the project runtime: absent.
- Live/Testnet Binance environment variables: absent.
- Binance connection, account query, or order under v1.3: none.

## 7. Sole next action

Restore access to the official Demo Trading portal and create a Demo-only API
key through its API Management page. Do not send either credential through
chat. After a secure ephemeral injection path is ready, finalize and freeze
v1.3 before any signed request.
