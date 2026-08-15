# GMAQ Freqtrade Replacement Spike

Date: 2026-08-15  
Decision: `ADOPT_FREQTRADE`  
`DIRECTION_PASS = YES`

## Scope and safety

This spike evaluates Freqtrade 2026.7 as the single execution/product runtime for
the declared GMAQ target: Binance USD-M futures, automated strategy execution,
FreqUI, dry-run first, and later separately authorized very-small live trading.

No Binance API key or secret was entered, read, or requested. No authenticated
Binance endpoint was used. No Binance account setting was changed. No real order
was placed. The old Nautilus launcher and its worktree were not run or modified.

## Exact runtime

- Host: macOS 26.5.2, Apple Silicon (`arm64`), 16 GiB RAM.
- Freqtrade: 2026.7, CCXT 4.5.68, Python 3.14.6 inside the official image.
- Image: `freqtradeorg/freqtrade:stable` at
  `sha256:50720a4af314a812be2cfbf5cc6331c63e9332b06f3f4372241f54bc61a35486`.
- Container tools: Docker CLI 29.7.2, Docker engine 29.5.2 (`arm64`), Compose
  5.4.0, Colima 0.10.3.
- Pair: `ETH/USDT:USDT`; futures, isolated, 1x; fixed 50 USDT stake;
  `max_open_trades = 1`; dry-run wallet 1,000 USDT.
- UI/API exposure: `127.0.0.1:8080` only.
- Persistence: SQLite at `/freqtrade/user_data/data/tradesv3.dryrun.sqlite`.

Docker Desktop was not installed. Docker's first-party Desktop application
requires the user to accept its own license, so the spike used a lightweight
local VM to run the official Freqtrade multi-architecture image and Compose
layout. Promotion may use Docker Desktop or a Linux host without changing the
Freqtrade files.

## Phase 0 — current official capability check

| Capability | Official current result | Spike implication |
|---|---|---|
| macOS / Docker | Docker is the supported path for ARM64 macOS; Freqtrade publishes a multi-arch official image and Compose file. | PASS on Apple Silicon with the official image. |
| Binance Futures | Binance is an officially supported futures exchange. CCXT futures symbols use `base/quote:settle`. | `ETH/USDT:USDT` loaded successfully. |
| FreqUI | Installed automatically by script/Docker and served by the built-in API server. | No frontend build or fork is needed. |
| Keyless dry-run | API keys are normally not required in dry-run; the wallet and trades are simulated. | Empty key/secret worked. |
| Persistence / restart | Trade and position state is stored in SQLite by default; open activity is restored after restart. | Closed history and an open trade were restored. |
| Isolated futures | `trading_mode=futures` and `margin_mode=isolated` are supported for Binance. | Configuration validated and ran. |
| Position/risk controls | Fixed or dynamic stake sizing, `max_open_trades`, per-entry leverage callback, stoploss, exchange stop orders, liquidation buffer, position adjustment, cooldown, stoploss guard, and max-drawdown protection exist. | Generic execution/risk infrastructure does not need to be rebuilt. |

Primary official references:

- https://www.freqtrade.io/en/stable/installation/
- https://www.freqtrade.io/en/stable/docker_quickstart/
- https://www.freqtrade.io/en/stable/freq-ui/
- https://www.freqtrade.io/en/stable/configuration/
- https://www.freqtrade.io/en/stable/advanced-setup/
- https://www.freqtrade.io/en/stable/leverage/
- https://www.freqtrade.io/en/stable/exchanges/
- https://www.freqtrade.io/en/stable/stoploss/
- https://www.freqtrade.io/en/stable/plugins/
- https://www.freqtrade.io/en/stable/rest-api/

### Binance-specific limitations

1. A leveraged account must be dedicated to one Freqtrade bot; multiple bots or
   manual trading on the same account are outside its assumptions.
2. Standard Binance isolated futures requires One-way Position Mode and
   Single-Asset Mode. Freqtrade checks these settings in live startup but does
   not change them. This dry-run did not and could not validate account state.
3. Binance futures pricing must use the order book because a futures ticker is
   unavailable in the required form.
4. Binance Futures Quantitative Rules can restrict repeated low-stake orders.
   These venue limits are not disproved by a successful dry-run.
5. Region/geo-IP eligibility still applies.
6. Freqtrade permits one open trade per pair; scaling is expressed as position
   adjustment on that trade, not arbitrary concurrent position objects.
7. Liquidation fees are not modeled, and missing historical funding rates can
   make futures backtests inaccurate.
8. Manual/unsupported orders (for example OCO or iceberg) have only best-effort
   recovery. GMAQ should not mix them into the bot-owned account.

## Phase 1–2 — visible product proof

The configuration and strategy both passed Freqtrade's own config/strategy
loading. Startup then produced all of the following:

- `Runmode set to dry_run`, `Dry run is enabled`, Binance resolved as the
  exchange, swap/futures mode applied, and `ETH/USDT:USDT` whitelisted.
- FreqUI/API started at port 8080 and returned `{"status":"pong"}`.
- A live public order-book price was obtained from Binance USD-M.
- The strategy emitted a long signal and Freqtrade created a local order whose
  ID began `dry_run_buy_ETH/USDT:USDT_...`.
- FreqUI showed bot online/status, simulated USDT balance, strategy name and
  timeframe, open trades, closed trades, current/closed PnL, and chart markers.
- Three sequential simulated trades eventually closed. They have trade IDs
  1, 2, and 3, each with one unique dry-run entry and one unique dry-run exit.

The strategy is intentionally deterministic and has no alpha claim. Its only
purpose is to exercise the product.

## Phase 3 — stop, restart, and recovery

Verified sequence:

1. Trade 2 was open with one filled local entry order.
2. `POST /api/v1/stop` moved the bot to `state=stopped` while preserving the
   open trade; stop did not liquidate or keep managing it.
3. The container was restarted.
4. Startup logged `Found open trade: Trade(id=2, ...)`.
5. The API and FreqUI showed the same trade ID 2, same open timestamp, same
   amount, and the same `dry_run_...` entry order ID. No second position was
   created for the pair during recovery.
6. The closed trade 1 history and PnL remained visible after restart.
7. FreqUI reconnected and displayed both restored open trade 2 and closed
   trade 1.
8. `pause` was then exercised: it prevented further entries while continuing
   to handle the then-open trade. After it exited, `stop` left the final bot
   state as `stopped` with zero open trades.

Operational semantics:

- `pause` / `stopentry`: no new entries; existing trades continue to be
  managed. This state is not persisted across process restart.
- `stop`: stop the trader loop; it does not close or manage existing trades.
- `forceexit all`: explicit exit of every open trade using the configured
  force-exit order type. In live mode this is a real account mutation.
- `emergency_exit`: not a global kill switch. It is the fallback order type
  (market by default) when creation of an on-exchange stoploss fails.
- Process/container stop: terminates the process. Pending-order behavior is
  additionally controlled by `cancel_open_orders_on_exit`; this spike kept it
  false and used immediately filled dry-run market orders.

### Reliability caveat found during the spike

While the bot was active, a direct host-side SQLite query against the bind-mounted
database caused one `sqlite3.OperationalError: disk I/O error` under the Colima
shared-filesystem path. Compose restarted the bot and the committed history
survived. One in-memory dry-run order ID had been created immediately before
the failed database commit, but it had no external side effect and did not
become a trade or order row. A subsequent clean stop/restart restored trade 2
without duplication.

This is an operator/runtime boundary: never inspect or copy an active SQLite
file directly. Promotion should use API exports, stopped backups, or a database
volume/PostgreSQL appropriate to the deployment. It does not change the PASS
for the clean restart path, but it must become an operations rule.

## Acceptance matrix

| Gate | Result | Evidence |
|---|---|---|
| `FREQTRADE_START` | PASS | Worker 2026.7 reached RUNNING. |
| `BINANCE_FUTURES_MARKET_DATA` | PASS | Binance swap class loaded; ETH USD-M order-book price and candles appeared. |
| `DRY_RUN` | PASS | Config/API/logs all reported dry-run. |
| `SIMULATED_TRADE` | PASS | Three sequential dry-run trades with local dry-run order IDs. |
| `FREQUI` | PASS | Real browser login and Trade/Dashboard/Chart/Logs navigation worked. |
| `PERSISTENCE_RESTART` | PASS | Closed history and open trade 2 restored with identical identity. |
| `NO_REAL_ORDER` | PASS | No credentials; all order IDs were dry-run; no private/account path was available. |
| `NO_CREDENTIAL_REQUIRED` | PASS | Empty exchange key and secret started and traded in simulation. |

## Phase 4 — GMAQ fit and contraction

For the declared Binance Futures product, Freqtrade covers an estimated 85–90%
of the execution/product surface expected over the next one to two years:
exchange adapter, public/private market integration, order lifecycle, wallet,
positions, persistence, restart, pricing, fees/funding plumbing, sizing,
leverage, stoploss, protective locks, REST API, UI, logs, dry-run, backtest and
optimization. It does not establish coverage for a future US-equity or physical/
exchange-traded-gold execution mandate; Freqtrade is a crypto trading bot. Such
a mandate would be a separate future product decision, not a reason to keep two
execution stacks today.

### Keep

- Real GMAQ strategy/alpha logic, once it exists.
- GMAQ-specific risk constraints that cannot be represented by standard config,
  protections, `custom_stake_amount`, `leverage`, stoploss, or position-adjustment
  callbacks.
- Strategy configuration, research inputs, and reproducible research artifacts.
- A small promotion checklist/test that asserts dry-run/live separation, pair
  allowlist, 1x leverage, stake cap, `max_open_trades`, and stoploss settings.

The current `FixedTargetStrategy` explicitly describes itself as a no-alpha
test shell. It is evidence infrastructure, not a production strategy to port
wholesale.

### Delete after promotion is accepted and an archive tag is made

| Old area | Current examples | Replacement |
|---|---|---|
| Gate execution runtime | `gate1b/runtime.py`, `execution_kernel.py`, `execution_lifecycle.py`, `supervisor.py`, `process_boundary.py`, mutation modules | Freqtrade worker + exchange layer + REST control |
| Binance HTTP/client path | credential HTTP/transport/session modules; read-only/demo preflight launchers | Freqtrade Binance/CCXT adapter and standard exchange checks |
| Custom order lifecycle | Gate 1A coordinator/order states and Gate 1B lifecycle/projection | Freqtrade Trade/Order persistence and exchange reconciliation |
| Execution journals | Gate 1A ledger/recovery/inbox and Gate 1B journals/evidence logs/final evidence | Freqtrade database, trade/order history and logs |
| Custom reconciliation | Gate 1A account snapshot reconciliation; Gate 1B projection/supervisor recovery | Freqtrade startup recovery and exchange order/trade handling |
| Nautilus authorization store | Gate 1B authorization, runtime-binding, credential prompt/session and safety wrappers | Deployment-level dry-run/live config separation plus Freqtrade API auth |
| Old launchers | `run_gate_1b_*`, prompted/credential/preflight runners, associated verifier scripts | `docker compose up -d` and Freqtrade API/UI |
| UI adapter plans | Any Nautilus-to-dashboard adapter plan | Built-in FreqUI; no adapter and no frontend fork |
| Covered tests/protocols | Tests and Gate documents whose only purpose is the retired runtime | Freqtrade config/strategy tests and a short operational runbook |

The current repository contains about 36.5k lines of Python source and 28.9k
lines of Python tests, dominated by Gate runtime and evidence machinery. The
target maintained custom surface is:

- 1–3 Freqtrade strategy files;
- one base configuration plus a separately managed live secret overlay;
- focused risk/config/strategy tests;
- research artifacts outside the execution runtime.

Target: roughly 500–1,500 maintained lines including tests for the first
Binance product, excluding research notebooks/data. No Freqtrade core fork, UI
fork, exchange wrapper, compatibility layer, second journal, or second
reconciliation engine.

## Explicit gaps that remain

Freqtrade does not provide native US-equity/gold broker execution, arbitrary
multi-venue institutional OMS semantics, per-order human authorization records,
or a guarantee that manual/unsupported exchange activity will reconcile. It
also does not prove Binance account mode, permissions, quantitative-rule headroom,
real fill/slippage, funding, liquidation, or small-live behavior in a keyless
dry-run. These are promotion checks, not blockers to adopting it for the stated
Binance Futures product.

## Final direction and minimal promotion/contraction plan

`ADOPT_FREQTRADE`

1. Replace the probe strategy with one real GMAQ strategy and express all
   representable risk limits through standard Freqtrade config/callbacks.
2. Add focused backtest/lookahead checks and a 7–14 day continuous dry-run soak
   on a stable Docker Desktop or Linux deployment. Do not query active SQLite
   from the host.
3. Add operational alerts, backup/restore drill, explicit pause/stop/force-exit
   runbook, and random production API/UI secrets.
4. Perform a separate read-only promotion review for Binance account mode,
   dedicated subaccount, permissions, exchange limits and exact live config.
5. Only after fresh explicit authorization, promote to a one-pair, 1x,
   minimum-safe-stake live canary with hard loss/stake/open-trade caps.
6. After the dry-run soak and promotion candidate pass, tag/archive the old Gate
   lineage, delete the listed runtime infrastructure in one contraction change,
   and remove Nautilus from dependencies. Do not keep both systems.

