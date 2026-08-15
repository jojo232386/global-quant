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

## Current blockers

- Dedicated account/subaccount ownership and sole-operator status are unverified.
- One-way and Single-Asset account modes are unverified.
- Current symbol filters, minimum notional, fee/funding, quantitative-rule
  headroom, regional eligibility, and API permission behavior are unverified.
- Live stake/loss/notional numbers are not approved.
- Live secret storage, rotation, IP restriction, monitoring, and alert routing
  are not configured.
- The 48–72h reliability run has not yet been completed on the promoted layout.
- Real fill/slippage, funding, liquidation, and live restart behavior remain
  untested and cannot be inferred from dry-run.

Any one of these remains a `LIVE_READINESS_BLOCKER`; none should be repaired by
placing a real order during readiness review.

## Authoritative references

- Freqtrade exchange notes: <https://www.freqtrade.io/en/stable/exchanges/>
- Freqtrade leverage/account assumptions: <https://www.freqtrade.io/en/stable/leverage/>
- Freqtrade dry-run and secret separation: <https://www.freqtrade.io/en/stable/configuration/>
- Binance API permissions and security: <https://academy.binance.com/en/articles/what-are-api-keys-and-security-types>
