# Sprint 002 OSS fallback pre-code audit

Status: `OSS_FALLBACK_POC_COMPLETE`
Scope: Research only. This does not authorize a strategy, orders, dry-run, or
live/runtime change. The authoritative data semantics remain GMAQ Price V1,
the bounded PIT Instrument Master V1 `universe_at(t)`, and Lifecycle V1.

## Selection matrix

Activity and stars are only rough references, observed from the projects'
official GitHub repositories on 2026-08-25; they are not quality or safety
evidence. “MCP/Agent” means a documented integration surface, not a claim of
safe autonomous trading.

| Framework | Maintenance / license / reference | Apple Silicon + Python | Binance USD-M futures | Research/backtest; walk-forward/parameter testing | Custom data / strategy API | Fees, slippage, dry-run, UI, MCP/Agent | GMAQ migration cost | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Freqtrade | Active, ~53.6k stars; GPL-3.0. | Project runtime is already Docker-pinned; local host compatibility is therefore not an adoption criterion here. | Documented futures mode. | Mature backtesting and Hyperopt; lookahead analysis. | Python strategies and DataProvider. | Fees and configurable protections; established dry-run/UI path. Agent support is not a research guarantee. | Medium-to-high: needs an adapter to preserve the bounded master and terminal-close semantics. | Retain unchanged for eventual execution/dry-run only. |
| Jesse | Active, ~8.4k stars; MIT. | No current local install verified. | Project documents Binance futures support. | Custom strategies/backtests and research tooling; its Research API documentation states multiple routes simultaneously are not supported. | Custom Python strategies; custom-data route would still be required. | Backtest fees/slippage are configurable; no GMAQ dry-run/UI adoption evidence collected. | Medium: adapter plus Research API multi-route limitation. | Not selected. |
| vectorbt | Active, ~8.8k stars; Apache-2.0 **with Commons Clause** (verify suitability before redistribution/use beyond this local PoC). | Current package metadata supports Python 3.12; tested through a separate local ARM64 environment only. | No exchange connector is required for this offline PoC. | Vectorized custom signals and `Portfolio.from_orders`; parameter sweeps are native array operations, but no GMAQ walk-forward gate adapter exists. | DataFrames/custom order arrays are the API; ideal for thin read-only adapter. | Explicit fee/slippage simulation; no dry-run, execution venue, or production UI is sought. No MCP claim. | Lowest: only a small read-only bridge over frozen loaders/master/lifecycle. | **Selected: `TOTAL_COMPLEXITY_MINIMUM` for Research.** |
| NautilusTrader | Active, ~27.8k stars; LGPL-3.0. | Officially documents macOS ARM64 and Python 3.12–3.14. | Venue/instrument adapters are available but are unnecessary for the bounded offline dataset. | Event-driven historical backtesting and optimization-capable architecture. | Strong custom-data and strategy APIs. | Detailed fee/modeling infrastructure; not a smaller dry-run/UI path than retained Freqtrade. | High: event/data model conversion and added operational surface. | Not selected. |
| Hummingbot | Active, ~19.6k stars; Apache-2.0. | Docker/Conda-first installation documented; no local install verified. | `binance_perpetual` connector documented. | Strategy/controller backtesting emphasis, primarily execution and market making. | Strategy/controller interfaces; no exact historical PIT/Lifecycle bridge. | Paper trade, dashboards/clients, and agent-oriented ecosystem; these do not supply GMAQ gates. | High for cross-sectional research and no execution need beyond retained Freqtrade. | Not selected. |

Exact official sources:

- Freqtrade: [custom strategy/DataProvider](https://www.freqtrade.io/en/stable/strategy-customization/), [backtesting](https://www.freqtrade.io/en/stable/backtesting/), [lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/), [futures mode](https://www.freqtrade.io/en/stable/leverage/), and [Hyperopt](https://www.freqtrade.io/en/stable/hyperopt/).
- Jesse: [official repository](https://github.com/jesse-ai/jesse), [custom strategies](https://docs.jesse.trade/docs/strategies/custom-strategies/), [backtesting candles](https://docs.jesse.trade/docs/backtesting-candles/), and [Research API](https://docs.jesse.trade/docs/research/research-api/).
- vectorbt: [official repository](https://github.com/polakowo/vectorbt) and [`Portfolio` API](https://vectorbt.dev/api/portfolio/base/).
- NautilusTrader: [official repository](https://github.com/nautechsystems/nautilus_trader) and [backtesting concepts](https://nautilustrader.io/docs/latest/concepts/backtesting/).
- Hummingbot: [official repository](https://github.com/hummingbot/hummingbot) and [strategy documentation](https://hummingbot.org/strategies/).

## Selected thin PoC

`OSS-VBT-PIT-EQUALWEIGHT-LONG-001` is a deterministic **equal-weight,
long-only market benchmark**, explicitly labelled `BENCHMARK_ONLY_NOT_ALPHA`.
It is not a Sprint-002 candidate run and reports no return, PnL, Sharpe, IC,
or alpha conclusion.

- Load and validate Price V1 with its existing pinned loader, then load all
  80 bounded cohort records from the existing master; no exchange fetches or
  substitutions.
- At each completed UTC close, query `universe_at(t)`; submit equal-weight
  target-percent orders at the next UTC open, each Monday from 2021-01-11
  through 2023-11-06. Explicitly close at the 2023-11-13 UTC open.
- Preserve Lifecycle V1 terminal treatment: retain through the terminal day's
  open-to-final-close interval and force a close-price liquidation at the
  exact terminal timestamp. A separate event row avoids overwriting a
  same-day next-open rebalance.
- Simulate with `Portfolio.from_orders`, `cash_sharing=True`, 5 bps fee, and
  10 bps one-side slippage. Canonical input JSON receives a SHA-256
  fingerprint, and the focused test replays it twice.

Tracked implementation is one `research/oss/vectorbt_pit_baseline.py`
(256 core LOC), a small requirements input/lock, and one focused test.
The external environment is `/Users/ASUS/Desktop/.venvs/gmaq-vectorbt-poc`;
no project, Docker, Freqtrade, runtime, credentials, or production dependency
is modified.

## Conclusion

`NO_EXACT_READY_REUSE`: none of these frameworks natively enforces GMAQ's
fixed Price V1 identity, bounded PIT cohort, Lifecycle terminal rule,
fail-closed missingness, and Tier-1 gates. A read-only vectorbt bridge is
smaller than an adapter for the other four. Select vectorbt for Research at
`TOTAL_COMPLEXITY_MINIMUM`, while retaining Freqtrade unchanged for execution
if live admission evidence later warrants it. Tier 1 remains research gating;
exploration-only, funding-not-modeled, and no strategy/live claim.

`RESULT=SWITCH_TO_OPEN_SOURCE_RESEARCH_STACK`

`OSS_POC=research/oss/vectorbt-pit-baseline-artifact.json`

`OSS_POC_RESULT=BENCHMARK_ONLY_NOT_ALPHA`
