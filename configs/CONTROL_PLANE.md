# GMAQ Control Plane (dry-run scope)

> `DRY_RUN_ONLY = TRUE`

This document specifies the control surface that must exist around the
Freqtrade runtime before any live canary can even be proposed. Everything
here is designed and validated under dry-run only. The live authorization
plane is intentionally absent.
This document does not authorize live trading. Live use additionally
requires the same-day read-only exchange verification
(`configs/LIVE_READINESS.md`) and a new, explicit, one-time authorization.

## 1. Armed state model

States: `DISARMED`, `PREFLIGHTING`, `PREFLIGHT_PASS`, `ARMED`, `PAUSED`,
`KILLED`.

- `DISARMED`: repository and startup default. The bot may run dry-run, but
  `confirm_trade_entry()` rejects every new entry.
- `PREFLIGHTING`: `scripts/gmaq-control preflight` is checking the control
  surface.
- `PREFLIGHT_PASS`: all preflight gates passed for the current exact
  configuration SHA. Any config change invalidates this state.
- `ARMED`: a short-lived `DEMO_DRY_RUN_ENTRY` authorization only. It is bound
  to the exact candidate commit, config digest, runtime run id, passing
  preflight, expiry, and authorization audit record. It is not live authority.
- `PAUSED`: operator or a protection halted trading while keeping the
  runtime alive.
- `KILLED`: the independent kill switch fired. Restart requires a full
  preflight and a new authorization.

Transitions allowed: `DISARMED -> PREFLIGHTING -> {PREFLIGHT_PASS | DISARMED}`;
`PREFLIGHT_PASS -> ARMED` (fresh dry-run authorization only);
`ARMED -> PAUSED`; a new preflight/authorization is required after stronger
recovery boundaries;
`* -> KILLED`; `KILLED -> DISARMED` (recovery). Everything else is invalid and
must be refused by the control plane.

## 2. Order lifecycle state machine

`INTENDED -> SUBMIT_ACK -> OPEN -> PARTIAL -> CLOSED`, with terminal states
`CANCELED`, `REJECTED`, `EXPIRED`, and `UNKNOWN_OUTCOME`.

- Every transition must be recorded in the audit manifest with the bound
  client-order identity and the evidence (REST ack, WS event) that caused it.
- `UNKNOWN_OUTCOME`: quarantine. No automatic retry, cancel, amend, or
  reorder. The outcome is adjudicated manually against exchange records and
  the decision is written to the audit manifest. `AMBIGUOUS_OUTCOME` stays a
  hard stop.

## 3. Unique client-order identity

- Format: `gmaq-<env>-<pair>-<utc-micros>-<8 base32 chars>`.
- Generated once per order intent, written to the audit manifest before
  submission, and never reused. The identity carries no secrets.
- Dry-run still exercises the format and uniqueness checks so the promoted
  layout behaves identically.
- Current pinned Freqtrade does not expose a Binance client-order-id callback
  or order-create parameter. Therefore this identifier is not claimed as
  exchange-bound in this candidate. Freqtrade/SQLite `order_id` is the
  dry-run reconciliation identity. A live candidate remains blocked until a
  reviewed adapter can prove exchange-bound client identity end to end.

## 4. Reconciliation

- When live: REST and WebSocket views of the broker must both be captured
  and cross-checked after every mutation and on a schedule. Any mismatch
  between intent, broker REST, broker WS, and bot internal state enters
  quarantine with an alert; there is no self-healing.
- Dry-run: `/status` is treated as open trades, never as open orders. Each
  trade's nested orders are normalized and compared with the SQLite `trades`
  and `orders` rows by trade/order identity, side, amount, filled, remaining,
  price, open flag, and status. Duplicate identity, unknown status, partial
  outcome, API/DB failure, or field mismatch disarms entry and returns
  `UNKNOWN`/`MISMATCH`; there is no automatic retry.

## 5. Audit manifest

- Append-only JSONL at `user_data/audit/manifest.jsonl` (runtime state, not
  committed).
- Each record: sequence, UTC timestamp, actor, real top-level verdict,
  candidate/tree/config/image/run/gate identity, event references, and
  `sha256` of the previous serialized record, forming a hash chain. The chain
  is verified on every control-plane action; a broken chain is a hard stop.
- Appends use an exclusive file lock, `O_APPEND`, and `fsync`; state/binding
  replacement is atomic and fsynced. A separate process-and-thread transition
  lock serializes bind, preflight, arm, disarm, pause, recover, and kill so an
  older in-flight authorization cannot overwrite an emergency stop. Concurrent
  append, transition, truncation, and broken-chain behavior is covered by tests.

## 6. Health metrics

- Heartbeat: Freqtrade `last_process` age.
- Clock: offset between host and exchange server time; limit 2 seconds.
- Data freshness: age of the newest candle processed.
- Counts: open trades, open orders, dry-run wallet, protection state.
- Verdicts: `HEALTHY`, `UNHEALTHY`, `UNKNOWN`. Any `UNKNOWN` health input is
  treated as `UNHEALTHY` for readiness purposes (fail-closed).

## 7. Alerts

- Every state change, `UNHEALTHY` verdict, and `UNKNOWN_OUTCOME` writes a
  local state file and an audit line. No failure is silent.
- Operator-routed channels are configurable now: generic webhook
  (`GMAQ_ALERT_WEBHOOK_URL`) and/or Telegram (`GMAQ_TELEGRAM_BOT_TOKEN` +
  `GMAQ_TELEGRAM_CHAT_ID`). Fail verdicts in `preflight`, `health`,
  `reconcile`, `exit`, `audit`, and `kill` dispatch automatically.
- `scripts/gmaq-control alert-test` verifies the channels end-to-end; it
  fails closed when channels are configured but cannot deliver.
- Every dispatch is recorded in the audit journal with the channels and
  delivery outcome; a failed delivery is recorded, never silent.
- Alert payloads contain no credentials and no secrets.

## 8. Independent kill switch

- `scripts/gmaq-control kill` stops the runtime container and records the
  kill in the audit journal. It must work when the bot process is wedged,
  when the API is down, and without any bot cooperation. It is an
  operator-plane control, not a bot feature.
- The bot's own Freqtrade protections (StoplossGuard, MaxDrawdown,
  CooldownPeriod) remain an inner layer; they do not replace the kill switch.

## 9. Controlled close

- `scripts/gmaq-control exit` first disarms new entries, sends at most one
  force-exit request per normalized open trade, and then requires a matching
  REST/SQLite proof of zero open trades, zero open/partial orders, and zero
  unknown outcomes. An unacknowledged request is quarantined and is never
  retried automatically. Timeout remains a hard failure.
- `exit` is the orderly counterpart of `kill`: it requires a healthy bot,
  while `kill` must work without one.

## 10. Dry-run constraints

- The committed configuration stays dry-run. `scripts/gmaq up` and `restart`
  create a new runtime binding and force `DISARMED` before the container is
  started or recreated. Direct Compose startup receives `UNBOUND` identities
  and therefore cannot pass the entry gate.
- `LiveExecutionCanaryStrategy.confirm_trade_entry()` is the final entry
  callback. It rereads the state on every attempt and rejects missing,
  malformed, expired, wrong-environment, wrong-commit, wrong-config,
  wrong-run-id, or broken-audit authorization.
- `scripts/gmaq-control arm --authorization-id <id> --ttl-seconds <30..3600>`
  can authorize only `DEMO_DRY_RUN_ENTRY` after a fresh `PREFLIGHT_PASS`.
  There is no command in this candidate that can authorize live entry.
- Live must not reuse `initial_state=running`,
  `cancel_open_orders_on_exit=false`, or `stoploss_on_exchange=false`
  without the explicit protections in this document, exchange-side order
  protection, and reconciliation proof. A live config requires its own
  review and its own config file.

## Validation

- Contract tests: `tests/test_control_plane_contract.py`.
- Behavioral gate tests: `tests/test_entry_gate_behavior.py`.
- Runtime preflight: `./scripts/gmaq-control preflight`.
- Audit chain: `./scripts/gmaq-control audit verify`.
