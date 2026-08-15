# GMAQ

`GMAQ_ACTIVE_RUNTIME = FREQTRADE`

GMAQ now has one active execution truth: the pinned official Freqtrade image.
Freqtrade owns Binance connectivity, market/account runtime, order lifecycle,
persistence, restart recovery, execution, standard risk controls, REST API, and
FreqUI. GMAQ owns only the canary/alpha strategy, research, project-specific
risk limits, configuration, and focused contract tests.

The checked-in configuration is deliberately incapable of live trading:

- `dry_run = true`;
- Binance key and secret are empty;
- one pair: `ETH/USDT:USDT`;
- USD-M futures, isolated, 1x;
- fixed 25 USDT simulated stake and one open trade maximum;
- FreqUI is bound to `127.0.0.1:8080` by Compose;
- the trade database and stopped backups use separate Docker named volumes;
- `LiveExecutionCanaryStrategy` declares `NOT_PROVEN_ALPHA = TRUE`.

No live configuration or secret overlay exists in this repository.

## Start the product

```sh
./scripts/gmaq up
```

Open <http://127.0.0.1:8080>. The local dry-run-only FreqUI login is `gmaq` /
`gmaq-dry-run-local-only`; replace all API/UI secrets before any separately
authorized deployment. Common controls are `./scripts/gmaq status`,
`./scripts/gmaq logs`, `./scripts/gmaq restart`, and `./scripts/gmaq down`.

The wrapper fails closed unless the committed config remains dry-run with empty
exchange credentials. It intentionally cannot launch a live bot.

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

It accepts only 48–72 hours and only the credential-free dry-run config. It
exercises pause/start, stop/start, restart recovery, a short container-network
interruption, stopped-database backup and restore validation, FreqUI/API
reconnection, duplicate-open-trade checks, and final `forceexit all` before the
bot and container stop. This is runtime reliability validation, not alpha
validation.

Do not query or copy an active SQLite file. The named-volume layout avoids the
macOS shared-filesystem I/O failure found during the spike; backups are taken
only while the worker is stopped.

## Historical recovery

The complete pre-cutover implementation is recoverable from the annotated tag
`gmaq-pre-freqtrade-cutover-2026-08-15`. Do not restore parts of it alongside
Freqtrade; recovery means an explicit historical checkout, not two active
execution stacks. The verified feasibility report remains under `research/`.

Read [configs/LIVE_READINESS.md](configs/LIVE_READINESS.md) before proposing a
first live canary. That document is planning only and grants no mutation or
credential authority.
