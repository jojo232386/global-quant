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
- live deployment requires a separate configuration and explicit review;
- no active SQLite database should be copied or queried directly.

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
- `research/`: strategy research scope and future research artifacts
- `configs/`: live-readiness policy and planning
- `tests/`: focused configuration and custom-behavior contracts
- `scripts/`: safe product and reliability commands

Earlier architecture remains recoverable from Git history and the published
historical tag; it is not part of the active runtime.
