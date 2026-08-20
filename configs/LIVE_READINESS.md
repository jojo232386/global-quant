# First live canary inventory

`PLANNING_ONLY = TRUE`

This inventory does not authorize credentials, authenticated tests, account
changes, or orders. The active repository contains only credential-free
dry-run configuration.

Minimum external requirements for a future, separately authorized canary:

1. A dedicated Binance Futures account or subaccount used only by this bot.
2. One-way Position Mode and Single-Asset Mode confirmed read-only before start.
3. `ETH/USDT:USDT` only, isolated margin, 1x leverage.
4. A freshly verified exchange-compliant minimum sensible notional; do not copy
   the 25 USDT dry-run stake without checking current filters and fees.
5. API permissions limited to the exact read/trade capability required by
   Freqtrade; withdrawals disabled and IP restrictions enabled where feasible.
6. The bot is the sole operator of the leveraged account: no manual orders,
   second bot, OCO, iceberg, or external position changes.
7. A rehearsed `pause` then `forceexit all` procedure, plus direct exchange
   observation for ambiguous outcomes.
8. Hard predeclared caps for per-trade notional, total open notional, daily loss,
   cumulative canary loss, and one open trade.
9. Random deployment-specific FreqUI/API credentials, private-network exposure,
   monitoring, alerting, clock sync, and stopped-database backup/restore proof.
10. A completed 48–72h dry-run reliability result with zero open positions after
    the final stop and no duplicate trade/order identities after restart.

## Tooling now present (evidence still pending)

The repository now owns dry-run-scope tooling for every check it can perform
without credentials:

- Control plane and armed states, order state machine, dry-run reconciliation,
  audit manifest, health, alerts, and an
  independent kill switch: `configs/CONTROL_PLANE.md`,
  `scripts/gmaq-control`.
- Same-day read-only exchange preflight (contract, precision/filters,
  minimum notional, implied leverage headroom, funding, spread, depth;
  account-mode and fee items fail-closed as UNVERIFIED_REQUIRES_AUTH):
  `scripts/gmaq-exchange-preflight`.
- Market-order fill and stress cost model: `configs/EXECUTION_COST_MODEL.md`,
  `scripts/gmaq-liquidity`.
- 48–72h promoted-layout soak protocol and evidence package:
  `configs/RELIABILITY_SOAK_PROTOCOL.md`, `scripts/reliability-soak`.
- Fail-closed, non-ordering live-candidate evidence aggregation and Binance
  REST/user-stream reconciliation contract: `scripts/gmaq-live-admission`,
  `gmaq_live/admission.py`. Synthetic evidence remains `BLOCKED`; it can never
  arm or submit an order. Strategy PASS must come from a committed result whose
  dataset binding replays through the existing Data Layer V1 registry as
  `VERIFIED / curated / PASS`. Authenticated capture, submission, and
  credential/account-binding adapters remain absent.

Tooling presence does not remove any blocker; only completed, recorded
evidence does.

## Current blockers

- Dedicated account/subaccount ownership and sole-operator status are unverified.
- One-way and Single-Asset account modes are unverified.
- A 2026-08-16 read-only snapshot reported maker 2 bps, taker 5 bps,
  tier-1 MMR 0.4%, and a Portfolio Margin layout. None of those historical
  values is accepted as current, version-bound account evidence; committed
  research values remain placeholders.
- The 2026-08-20 read-only preflight reached neither the classic USD-M nor
  Portfolio Margin account endpoint: both returned Binance `-2015`. The
  account type, position/margin modes, fee rates, and tier-1 MMR therefore
  remain `UNVERIFIED`; no account layout may be inferred from those failures.
- Quantitative-rule headroom, regional eligibility, and API permission
  behavior are unverified.
- Live stake/loss/notional numbers are not approved.
- Live secret storage, rotation, IP restriction, monitoring, and alert routing
  are not configured.
- The 48–72h reliability run has not yet been completed on the promoted
  layout per `configs/RELIABILITY_SOAK_PROTOCOL.md`.
- No strategy has passed the corrected research engine. The runtime canary is
  explicitly `NOT_PROVEN_ALPHA`; see `configs/RESEARCH_REMEDIATION.md`.
- Real fill/slippage, funding, liquidation, and live restart behavior remain
  untested and cannot be inferred from dry-run or from public snapshots.
- The pinned Freqtrade create-order path has no reviewed
  `newClientOrderId` injection point. The keyless broker-truth contract exists,
  but authenticated REST/user-stream capture and the exchange-bound submission
  adapter remain unimplemented blockers.

Any one of these remains a `LIVE_READINESS_BLOCKER`; none should be repaired by
placing a real order during readiness review.

## Authoritative references

- Freqtrade exchange notes: <https://www.freqtrade.io/en/stable/exchanges/>
- Freqtrade leverage/account assumptions: <https://www.freqtrade.io/en/stable/leverage/>
- Freqtrade dry-run and secret separation: <https://www.freqtrade.io/en/stable/configuration/>
- Binance API permissions and security: <https://academy.binance.com/en/articles/what-are-api-keys-and-security-types>
