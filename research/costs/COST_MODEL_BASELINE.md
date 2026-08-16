# Conservative Cost Model Baseline

> `PLACEHOLDER_UNVERIFIED = TRUE`

Every number in this baseline is a conservative research default, not a
verified fact. Per the operating rules, dynamic facts (fees, funding,
spreads, venue rules) must be re-verified from authoritative sources on the
day of use and recorded in the run manifest. A formal run may use these
defaults only while they are marked as such; any strategy that survives only
at optimistic costs must be REJECTed.
This baseline does not authorize live trading.

## Coverage requirements

A formal backtest must model, or explicitly exclude with justification:

- exchange / broker fees (taker and maker)
- funding (crypto perpetuals, per interval and under stress)
- borrow cost (shorts: US equities, futures-margin where applicable)
- bid-ask spread (measured, not assumed)
- slippage vs. aggressive execution
- market impact at the intended order size
- latency (signal-to-order and venue round-trip)
- partial fills and unfillable cases
- cash / collateral drag
- liquidation and ADL risk (crypto futures with leverage)
- roll costs and basis (gold futures)
- ETF tracking error (gold / equity ETFs)
- taxes and withdrawal fees: out of scope, but must be stated per study

## Default conservative values

| market | item | conservative default | status |
|---|---|---|---|
| US equities | fee | 0 (free broker) | PLACEHOLDER_UNVERIFIED |
| US equities | spread | 5 bps min | PLACEHOLDER_UNVERIFIED |
| US equities | slippage / impact | 10 bps + 1 bps per 10k USD notional beyond 100k | PLACEHOLDER_UNVERIFIED |
| US equities | borrow (short) | 100 bps annualized | PLACEHOLDER_UNVERIFIED |
| Gold spot | spread | 10 bps | PLACEHOLDER_UNVERIFIED |
| Gold futures | fee + spread | 5 bps + 5 bps | PLACEHOLDER_UNVERIFIED |
| Gold futures | roll | 20 bps per roll | PLACEHOLDER_UNVERIFIED |
| Gold ETF | expense + tracking | 40 bps annualized + 10 bps tracking | PLACEHOLDER_UNVERIFIED |
| Crypto (Binance USD-M) | taker fee | 5 bps | PLACEHOLDER_UNVERIFIED |
| Crypto (Binance USD-M) | funding | 1 bps per 8h mean; 5x under stress | PLACEHOLDER_UNVERIFIED |
| Crypto (Binance USD-M) | spread | 2–5 bps, measured per study | PLACEHOLDER_UNVERIFIED |
| Crypto (Binance USD-M) | slippage | 10 bps conservative | PLACEHOLDER_UNVERIFIED |
| Crypto (Binance USD-M) | liquidation / ADL | liquidation price buffer per leverage; ADL flagged as tail risk | PLACEHOLDER_UNVERIFIED |

## Application rules

1. Every manifest states the exact cost model version (file sha256) and the
   verified values actually used.
2. Every formal run includes a cost stress: fees x2, slippage x2, latency x2,
   funding at 5x mean. If the edge dies under stress, the run is REJECT.
3. If a required cost cannot be verified for the study period, the item is
   UNKNOWN; the run must either exclude related claims or be marked
   INCONCLUSIVE. Fail-closed.
4. Dry-run fills are simulated by Freqtrade and do not prove real execution
   cost. No conclusion about live cost may cite dry-run fills as evidence.
