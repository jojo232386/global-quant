# Research engine remediation status

`PROMOTION_BLOCKED = TRUE`

This note records defects found after the existing study results had already
been observed. The old artifacts are retained as historical evidence and are
not silently overwritten. Any corrected formal run needs a new preregistered
study id and freshly pinned inputs.

## Corrected in the engine

| defect | previous behavior | corrected behavior |
|---|---|---|
| stop loss | inspected only the planned exit bar | scans every held bar and models gaps below the stop at the worse open |
| daily risk metrics | omitted zero-trade dates and the initial equity boundary | uses a continuous UTC calendar and includes first-day PnL/drawdown |
| funding | charged only a flat stress buffer | charges published funding timestamps crossed by each long; stress uses 5x actual plus the declared buffer |
| spread stress | changed a reported spread but reused original executable books | shifts executable bid/ask books around the mid |
| latency stress | emitted a note only | moves buy and sell VWAP adversely and recomputes all-in cost |
| cost provenance | duplicated hard-coded 15/30 bps values | derives fee/slippage assumptions from `configs/execution-costs.json` and pins its SHA |
| funding history | requested only a recent page | paginates the exact inclusive study window and records coverage |
| historical funding mark price | trusted an optional response field that is empty in early records | keeps the published rate/time and pins a complete official 8h mark-price series; empty values use the exact UTC bucket open with explicit provenance |

A statistical PASS is downgraded to `INCONCLUSIVE` when the required funding
series or its complete request-window evidence is missing. A statistical
failure remains `REJECT`; incomplete data is never used to soften a failure.

The public funding-history endpoint and timestamp fields are documented by
Binance at <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#get-funding-rate-history>.

## Still unresolved

- The reconstructed point-in-time universe starts from today's top-100
  candidate pool. It reduces selection bias but cannot recover every contract
  that was listed and later delisted. It is not a complete historical listing
  master and cannot support a promotion claim.
- Account-specific fee and maintenance-margin values remain version-bound
  external evidence. The committed values are explicit research placeholders.
- Existing point-in-time studies ended `REJECT`, so no strategy is eligible for
  promotion. The fixed execution canary is intentionally `NOT_PROVEN_ALPHA` and
  may only be used to test the Demo order lifecycle under separate authority.
- The new preregistered PIT funding-shock dollar-neutral study also ended
  `REJECT`: OOS return -17.20%, Sharpe -1.236, max drawdown 33.92%, and stress
  return -33.29%. All frozen parameter neighbors and the 15-minute delayed
  stress run were negative. This hypothesis is closed, not a tuning target.
- The 2026-08-20 BTC/ETH weekly long-or-cash TSMOM study ended `REJECT` despite
  complete public data and positive baseline OOS return: OOS return +9.93%,
  Sharpe 0.305, max drawdown 48.61%, 2x-cost/5x-funding stress -45.83%, and
  one-day delayed stress -41.45%. Both frozen lookback neighbors lost money
  under stress and bootstrap confidence was 54.8%. The train/OOS decay and
  ETH drag make this a closed hypothesis, not a promotion candidate.
- The 2026-08-20 BTC/ETH volatility-scaled long/short TSMOM study consumed the
  Data Layer V1 curated snapshot
  `88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615`
  and ended `REJECT`: OOS return -11.05%, Sharpe -0.152, max drawdown 36.51%,
  adverse-funding stress -40.19%, delayed stress -34.73%, bootstrap confidence
  33.0%, and worst short adverse excursion 92.02%. This hypothesis is closed
  without parameter rescue.
- A corrected result must be a new preregistration; changing the old study in
  place after seeing its outcome would contaminate the research record.

This remediation does not authorize Demo entries or live trading.
