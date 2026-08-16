# GMAQ Control Plane (dry-run scope)

> `DRY_RUN_ONLY = TRUE`

This document specifies the control surface that must exist around the
Freqtrade runtime before any live canary can even be proposed. Everything
here is designed and validated under dry-run only.
This document does not authorize live trading. Live use additionally
requires the same-day read-only exchange verification
(`configs/LIVE_READINESS.md`) and a new, explicit, one-time authorization.

## 1. Armed state model

States: `DISARMED`, `PREFLIGHTING`, `PREFLIGHT_PASS`, `ARMED`, `PAUSED`,
`KILLED`.

- `DISARMED`: repository default. The bot may run dry-run.
- `PREFLIGHTING`: `scripts/gmaq-control preflight` is checking the control
  surface.
- `PREFLIGHT_PASS`: all preflight gates passed for the current exact
  configuration SHA. Any config change invalidates this state.
- `ARMED`: only reachable with a fresh explicit authorization for a live
  configuration. Never reachable from the committed dry-run config.
- `PAUSED`: operator or a protection halted trading while keeping the
  runtime alive.
- `KILLED`: the independent kill switch fired. Restart requires a full
  preflight and a new authorization.

Transitions allowed: `DISARMED -> PREFLIGHTING -> {PREFLIGHT_PASS | DISARMED}`;
`PREFLIGHT_PASS -> ARMED` (authorization only); `ARMED -> PAUSED -> ARMED`;
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

## 4. Reconciliation

- When live: REST and WebSocket views of the broker must both be captured
  and cross-checked after every mutation and on a schedule. Any mismatch
  between intent, broker REST, broker WS, and bot internal state enters
  quarantine with an alert; there is no self-healing.
- Dry-run: `scripts/gmaq-control reconcile` cross-checks the bot's REST view
  against the audit journal and verifies identity uniqueness. The WS path is
  exercised by the runtime and verified in the 48–72h reliability soak.

## 5. Audit manifest

- Append-only JSONL at `user_data/audit/manifest.jsonl` (runtime state, not
  committed).
- Each record: sequence, UTC timestamp, actor, event, references, and
  `sha256` of the previous serialized record, forming a hash chain. The chain
  is verified on every control-plane action; a broken chain is a hard stop.

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

- `scripts/gmaq-control exit` force-exits all open trades through the bot API
  and then polls until open orders and open trades are both zero, recording
  the proof in the audit journal. A timeout is a hard failure (`TIMEOUT`),
  never a silent pass.
- `exit` is the orderly counterpart of `kill`: it requires a healthy bot,
  while `kill` must work without one.

## 10. Dry-run constraints

- The committed configuration stays dry-run. The control plane validates
  that: `dry_run` is true, key and secret are empty, protections are present,
  and armed states are unreachable without fresh authorization.
- Live must not reuse `initial_state=running`,
  `cancel_open_orders_on_exit=false`, or `stoploss_on_exchange=false`
  without the explicit protections in this document, exchange-side order
  protection, and reconciliation proof. A live config requires its own
  review and its own config file.

## Validation

- Contract tests: `tests/test_control_plane_contract.py`.
- Runtime preflight: `./scripts/gmaq-control preflight`.
- Audit chain: `./scripts/gmaq-control audit verify`.
