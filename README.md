# GMAQ

GMAQ is a Freqtrade-based quantitative trading project focused on automated
cryptocurrency futures research, dry-run validation, and controlled live
deployment.

## Current stack

- Freqtrade 2026.7 as the single active trading runtime
- FreqUI as the operational interface
- Binance USD-M Futures
- Docker and Compose, with Colima supported on macOS

## Current status

- Binance public market data and dry-run trading verified
- `ETH/USDT:USDT`, isolated margin, 1x leverage
- FreqUI verified on `http://127.0.0.1:8080`
- persistence, restart recovery, and stopped-database backup/restore verified
- live credentials are not configured or stored in this repository
- live trading is not enabled

`LiveExecutionCanaryStrategy` is a runtime canary, not proven alpha.

## Quick start

Requirements: Docker with Compose, plus Python 3.12 or newer for local tests.

```sh
./scripts/gmaq up
```

The first start creates random local FreqUI/API credentials in the ignored
`.env` file. The values stay on the local machine. Open
<http://127.0.0.1:8080> after startup.

Useful commands:

```sh
./scripts/gmaq status
./scripts/gmaq logs
./scripts/gmaq restart
./scripts/gmaq down
```

## Safety

- the committed configuration is dry-run by default;
- Binance key and secret fields are empty;
- the startup wrapper refuses non-dry-run or credential-bearing exchange config;
- the initial scope is one pair, isolated margin, 1x, one open trade maximum;
- the strategy carries inner protections (StoplossGuard, MaxDrawdown,
  CooldownPeriod);
- the control plane (`configs/CONTROL_PLANE.md`, `scripts/gmaq-control`)
  provides armed states, an order state machine, unique client-order
  identity, reconciliation, an append-only audit manifest, health metrics,
  alerts, and an independent kill switch — all dry-run scope;
- live deployment requires a separate configuration and explicit review;
- no active SQLite database should be copied or queried directly.

## Control plane

```sh
./scripts/gmaq-control preflight   # fail-closed readiness check
./scripts/gmaq-control health      # heartbeat, clock offset, counts
./scripts/gmaq-control reconcile   # bot view vs. audit journal
./scripts/gmaq-control audit verify
./scripts/gmaq-control alert-test  # verify operator alert channels
./scripts/gmaq-control kill        # independent kill switch
```

Operator alerts (webhook / Telegram) are configured with `GMAQ_ALERT_WEBHOOK_URL`,
`GMAQ_TELEGRAM_BOT_TOKEN`, and `GMAQ_TELEGRAM_CHAT_ID` in the local `.env`;
fail verdicts dispatch automatically and every delivery is audit-logged.

## Exchange preflight (read-only, credential-free)

```sh
./scripts/gmaq-exchange-preflight
```

Queries Binance USD-M public market data only and writes a same-day manifest
to `user_data/audit/exchange-preflight.json`: contract status, precision and
filters, minimum notional, implied leverage headroom, funding, spread, and
depth. Account-mode, permission, and fee items are reported as
`UNVERIFIED_REQUIRES_AUTH` and keep live readiness BLOCKED until a separately
authorized read-only session verifies them.

## Execution cost and liquidity

```sh
./scripts/gmaq-liquidity
```

Walks the public order book to model market-order fills (VWAP, slippage,
partial fills), spread, funding carry, and liquidation distance under depth,
spread, latency, and funding stress. See `configs/EXECUTION_COST_MODEL.md`.
Taker fee and maintenance margin are placeholders until authenticated
account verification; the snapshot does not authorize live trading.

## Validate

```sh
python3 -m pytest -q
docker-compose run --rm freqtrade list-strategies \
  --config /freqtrade/user_data/config.json
```

The next bounded reliability run is:

```sh
./scripts/reliability-soak 72
```

It accepts 48–72 hours and exercises runtime controls, restart, a short network
interruption, stopped-database backup/restore, FreqUI reconnection, duplicate
identity checks, and a final dry-run `forceexit all`.

## Project structure

- `user_data/strategies/`: GMAQ strategy logic
- `user_data/config.json`: credential-free dry-run configuration
- `research/`: preregistration, data availability, run manifests, cost model
  baseline, and PASS/REJECT evaluation gate for future strategy research
- `configs/`: live-readiness policy, control-plane spec, and planning
- `tests/`: focused configuration and custom-behavior contracts
- `scripts/`: safe product, reliability, and control-plane commands

Earlier architecture remains recoverable from Git history and the published
historical tag; it is not part of the active runtime.
