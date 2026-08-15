# GMAQ Freqtrade cutover report

`RESULT = PASS`

`DIRECTION_PASS = YES`

`IMPLEMENTATION_PASS = YES`

## Historical identity

- Tag: `gmaq-pre-freqtrade-cutover-2026-08-15`
- Annotated tag object: `0bd9e0b309c13f0fc2cf0d49d6c86b632ee574b5`
- Pre-cutover commit: `621e56ba8754d9231c7b933bc633d065c94b9eeb`
- Pre-cutover tree: `3af648c742685aa2e3e65919e11ccec99edd3f0e`
- Git identity: `m y <ASUS@MacBook-Air-2.local>`
- Cutover branch: `codex/freqtrade-cutover`

The tag is the only archive mechanism. There is no archive branch or retained
second execution implementation.

## Final active structure

```text
global-quant/
├── configs/
│   └── LIVE_READINESS.md
├── research/
│   ├── CUTOVER_REPORT_2026-08-15.md
│   └── FREQTRADE_SPIKE_2026-08-15.md
├── scripts/
│   ├── gmaq
│   └── reliability-soak
├── tests/
│   └── test_runtime_contract.py
├── user_data/
│   ├── config.json
│   ├── data/.gitkeep
│   ├── logs/.gitkeep
│   └── strategies/LiveExecutionCanaryStrategy.py
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

The trade database and stopped backups are separate Docker named volumes, not
active host bind-mounted SQLite files.

## Deleted active source

- Entire `src/global_quant/gate1a/` implementation: arbiter, coordinator,
  environment, ledger, recovery, scenarios, strategy, and fixtures.
- Entire `src/global_quant/gate1b/` implementation: authorization, credential
  wrappers/transports/sessions, custom Binance HTTP/preflight, durable intent,
  execution evidence/journal/kernel/lifecycle/projection, final evidence,
  mutation protocol, process boundary, review artifacts, runner/runtime,
  runtime binding, safety, and supervisor.
- Old launch/evidence/verifier scripts: `build_gate_manifest.py`,
  `decide_gate.py`, `decide_gate_1b.py`,
  `generate_gate_1b_acceptance_artifact.py`,
  `run_determinism_evidence.py`, `run_gate_1a_evidence.sh`,
  `run_gate_1b_demo.py`, `run_gate_1b_prompted.py`,
  `run_gate_1b_v1_11_readonly_preflight.py`,
  `run_gate_1b_v1_5_readiness.py`, `run_gate_1b_v1_6_demo.py`,
  `run_gate_1b_v1_9_readonly_preflight.py`, `run_logged.py`,
  `run_offline.sh`, `run_scenario_matrix.py`,
  `verify_gate_1b_acceptance.py`, and
  `verify_gate_1b_v1_9_acceptance.py`.
- `tools/network_probe.py`, `tools/offline_guard/`, all old Gate protocols,
  reviews, curated evidence copies, active-gate/checkpoint documents, the
  Nautilus dependency lock, and the local Nautilus virtual environment.

All deleted tracked content remains recoverable from the historical tag.

## Deleted tests

- Helpers: `crash_worker.py`, `nautilus_uncertain_worker.py`,
  `strategy_callback_worker.py`.
- Integration: `test_crash_recovery.py`, `test_determinism_matrix.py`,
  `test_gate1b_node_build.py`,
  `test_gate1b_v1_6_process_execution_acceptance.py`,
  `test_nautilus_backtest.py`, `test_network_isolation.py`,
  `test_scenario_matrix.py`, `test_strategy_callback_recovery.py`, and
  `test_tool_versions.py`.
- Unit: `test_arbiter.py`, `test_command_logger.py`, `test_coordinator.py`,
  `test_gate1b_acceptance_verifier_cli.py`, `test_gate1b_arbiter.py`,
  `test_gate1b_config.py`, `test_gate1b_credential_prompt.py`,
  `test_gate1b_demo_preflight.py`, `test_gate1b_funding.py`,
  `test_gate1b_preflight.py`, `test_gate1b_runner.py`,
  `test_gate1b_safety.py`, `test_gate1b_strategy_cap.py`,
  `test_gate1b_v1_10_read_only_diagnostics.py`,
  `test_gate1b_v1_11_signed_request_correctness.py`,
  `test_gate1b_v1_5_readiness.py`, `test_gate1b_v1_6_authorization.py`,
  `test_gate1b_v1_6_cli.py`,
  `test_gate1b_v1_6_credential_execution_session.py`,
  `test_gate1b_v1_6_credential_session.py`,
  `test_gate1b_v1_6_credential_transport.py`,
  `test_gate1b_v1_6_durable_intent.py`,
  `test_gate1b_v1_6_execution_evidence_log.py`,
  `test_gate1b_v1_6_execution_journal.py`,
  `test_gate1b_v1_6_execution_kernel.py`,
  `test_gate1b_v1_6_execution_lifecycle.py`,
  `test_gate1b_v1_6_execution_projection.py`,
  `test_gate1b_v1_6_final_evidence.py`,
  `test_gate1b_v1_6_mutation_protocol.py`,
  `test_gate1b_v1_6_process_boundary.py`,
  `test_gate1b_v1_6_redirect_attack.py`,
  `test_gate1b_v1_6_review_artifact.py`,
  `test_gate1b_v1_6_runtime_binding.py`,
  `test_gate1b_v1_6_supervisor.py`,
  `test_gate1b_v1_8_filter_preparation.py`,
  `test_gate1b_v1_9_read_only_preflight.py`,
  `test_gate1b_v1_9_review_artifact.py`,
  `test_gate1b_version_aware_acceptance.py`, `test_ledger.py`,
  `test_manifest_evidence.py`, `test_scenario_oracle.py`, and
  `test_strategy_contract.py`.

The replacement is one 5-test runtime contract module.

## Remaining GMAQ-owned production surface

Approximately 413 lines excluding tests and documentation:

- 76 lines: one `LiveExecutionCanaryStrategy`, explicitly
  `NOT_PROVEN_ALPHA = TRUE`;
- 78 lines: credential-free dry-run Freqtrade configuration;
- 36 lines: pinned official image and named-volume Compose layout;
- 61 lines: fail-closed one-command product wrapper;
- 162 lines: bounded 48–72h dry-run reliability driver.

GMAQ owns only strategy/canary behavior, one-pair allowlisting, 1x leverage,
fixed sizing, cooldown/stoploss constraints, dry-run launch safety, operational
reliability checks, research, and live-readiness policy. Freqtrade owns all
generic connectivity, market/account runtime, order lifecycle, persistence,
reconciliation/recovery, execution, standard risk controls, API, and UI.

## Product acceptance

- `GMAQ_ACTIVE_RUNTIME = FREQTRADE`
- `OLD_GATE_ACTIVE_RUNTIME = ABSENT`
- `NAUTILUS_ACTIVE_RUNTIME = ABSENT`
- `BOT_START = PASS` — Freqtrade 2026.7 reached RUNNING.
- `FREQUI = PASS` — loopback root returned the FreqUI HTML application and API
  ping returned `{"status":"pong"}`.
- `BINANCE_PUBLIC_DATA = PASS` — Binance USD-M swap initialized and a public
  ETH order-book price drove the simulated entry.
- `DRY_RUN = PASS` — config, API, and runtime logs all reported dry-run.
- `SIMULATED_TRADE = PASS` — one trade with one unique dry-run buy and one
  unique dry-run sell.
- `PERSISTENCE_RESTART = PASS` — trade ID, pair, open time, and order ID were
  identical before and after named-volume restart; no duplicate pair appeared.
- `DB_BACKUP_RESTORE = PASS` — stopped database and restored copy both passed
  SQLite integrity checks.
- `OPEN_POSITION_AFTER_FINAL_STOP = 0`
- `REAL_CREDENTIALS_USED = FALSE` — exchange fields were empty, Compose passed
  no sensitive exchange environment variables, and no credential path ran.
- `REAL_ORDER_SUBMITTED = FALSE` — both order identities had the `dry_run_`
  prefix and no authenticated exchange route existed.
- Focused tests: `5 passed`.

## Reliability and live readiness

Next command: `./scripts/reliability-soak 72` (48–72 accepted). The driver is
prepared and syntax/contract checked; the full 72-hour interval has not been
claimed as completed in this cutover task.

`LIVE_READINESS_BLOCKERS` remain the dedicated account/subaccount and sole-bot
operator proof, One-way and Single-Asset mode proof, current symbol filters and
minimum notional, fees/funding and quantitative-rule headroom, region/API
eligibility, approved live notional/loss caps, secret management/IP restriction,
monitoring/alerts, and completed 48–72h dry-run reliability result. See
`configs/LIVE_READINESS.md` for the read-only inventory.
