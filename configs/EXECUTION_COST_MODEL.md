# Execution Cost Model

> `PLACEHOLDER_UNVERIFIED = TRUE`

This document defines how GMAQ models real execution cost for the single
canary pair. Dry-run fills and top-1 book reads do not prove real cost; every
formal claim must use the model and evidence described here, with values
re-verified on the day of use. This document does not authorize live trading.

## Why a dedicated model exists

The committed configuration uses market orders with top-1 order book pricing.
That combination cannot prove what a market order would actually pay. This
model replaces the assumption with a measurable walk of the order book plus
explicit stress cases.

## 1. Market-order fill model

- Take a depth snapshot (public REST, limit 100) on each side.
- Walk levels from best price outward until the order notional is filled.
- Compute the volume-weighted average fill price and the slippage in basis
  points against best bid/ask and against the mid price.
- If the book cannot fill the order within the captured depth, the result is
  `UNFILLABLE_WITHIN_SNAPSHOT` — treated as a hard cost failure, never
  interpolated away.

## 2. Cost components

| component | measurement | treatment |
|---|---|---|
| taker fee | placeholder VIP0 0.05% until authenticated account check | add to both sides |
| spread | bookTicker bid/ask | half-spread as round-trip baseline |
| slippage | book-walk VWAP vs best | reported at multiple sizes |
| market impact | depth consumed by the walk | implied by slippage; no separate assumption |
| latency | order-to-fill delay | stressed by a configurable price drift per second |
| partial fill | book depth limits | `UNFILLABLE_WITHIN_SNAPSHOT` or per-level fill report |
| funding carry | current funding rate x holding time | per interval and per 24h |
| liquidation | mark vs liquidation distance | computed for 1x/2x/3x with placeholder MMR 0.5% |
| ADL | no public queue API | flagged qualitatively; stressed via liquidation distance |

## 3. Stress methodology

Every snapshot report includes stress scenarios:

1. Depth stress: available quantity scaled to 50% and 10% of the snapshot.
2. Spread stress: spread doubled and quintupled.
3. Latency stress: price drift of 2 bps/s for 1s and 5s.
4. Funding stress: funding at the adjusted cap/floor when available,
   otherwise 5x the current rate.
5. Fee stress: taker fee doubled.

Any strategy that survives only at baseline values and fails under stress is
`REJECT` per `research/gate/EVALUATION_GATE.md`.

## 4. Evidence requirements

- Snapshots must be same-day and timestamped UTC; stale snapshots (older
  than the study's allowed window) are invalid evidence.
- `scripts/gmaq-liquidity` produces the machine-readable snapshot report;
  the report is the only accepted shape for cost evidence.
- Each research run manifest records which snapshot was applied and its
  sha256.

## 5. Boundaries

- This model covers the canary pair only. Other pairs and venues require
  their own snapshots and re-verification.
- MMR used for liquidation math is a placeholder (0.5%) until verified on
  the account; it is marked in every report.
- Capacity beyond the reported sizes is unknown until the book walk shows
  degradation; claims about capacity require the larger-size walks.
