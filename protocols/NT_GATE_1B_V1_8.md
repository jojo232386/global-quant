# NT-GATE-1B v1.8 Minimal Preflight Deferral Delta

Protocol version: `1.8`

Base: `ce9165cc25596c1d303e5bf09587dc79bd09ee69`

Status: implementation candidate only; independent acceptance is not included.

## Frozen boundary

`TRADING_SEMANTICS_DELTA = NONE`

The v1.7 order remains frozen: `ETHUSDT`, `BUY`, `LIMIT`, `GTX`, fresh best bid
multiplied by `0.99`, `MAX_NOTIONAL_USDT = 25`, no amend, Demo USD-M only,
and no Production fallback. Account configuration and mutation protections are
unchanged.

## Public Demo snapshot

The credential-free snapshot used Binance USD-M Futures Demo public GET
responses only. No authenticated or account endpoint was called.

- Snapshot UTC: `2026-08-11T23:30:02.891Z`
- Symbol: `ETHUSDT`
- Status / contract: `TRADING` / `PERPETUAL`
- Quote / margin asset: `USDT` / `USDT`
- Best bid / ask: `1880.18` / `1880.19`
- Mark price: `1880.19000000`
- `PRICE_FILTER`: `minPrice=39.86`, `maxPrice=306177`, `tickSize=0.01`
- `LOT_SIZE`: `minQty=0.001`, `maxQty=10000`, `stepSize=0.001`
- `MARKET_LOT_SIZE`: `minQty=0.001`, `maxQty=10000`, `stepSize=0.001`
- `MAX_NUM_ORDERS`: `limit=10000`
- `MIN_NOTIONAL`: `notional=20`
- `PERCENT_PRICE`: `multiplierDown=0.9500`, `multiplierUp=1.0500`,
  `multiplierDecimal=4`
- `POSITION_RISK_CONTROL`: `positionControlSide=NONE`

The existing order derivation produced `price=1861.37`, `quantity=0.011`, and
`notional=20.47507`. Therefore the current minimum legal probe remains within
the frozen `25 USDT` cap. The cap was not increased and the symbol was not
changed.

## Filter admission delta

Supported static filters retain their existing strict parsing and validation.
Credential-free preparation now classifies every extra filter while preserving
its complete canonical public metadata:

- `MAX_NUM_ORDERS` with the documented positive integer `limit` is
  `AUTHENTICATED_CHECK_REQUIRED`. The public limit does not prove the account's
  current unclosed-order count.
- `POSITION_RISK_CONTROL` is `UNRESOLVED_EXCHANGE_RULE`. No inference is made
  from `positionControlSide=NONE` because an authoritative public semantic was
  not found.
- Any other future extra filter is `UNRESOLVED_EXCHANGE_RULE`; it is never
  silently ignored.

The credential-free result can mechanically express:

```text
PREPARATION_READY = TRUE
AUTHENTICATED_CHECK_REQUIRED = [MAX_NUM_ORDERS]
UNRESOLVED_EXCHANGE_RULES = [POSITION_RISK_CONTROL]
ORDER_AUTHORIZATION_READY = FALSE
```

Credential-free evidence is structurally incapable of authorizing an order.
Its authorization assertion always denies. In addition, the existing
`SymbolState`, `LimitOrderFilters`, `MarketCloseFilters`, order derivation, and
mutation path still fail closed when an uninterpreted applicable filter is
present. The protection was not removed or converted to ignored.

## Explicit non-changes

- Independent-review artifact schema: unchanged.
- Real acceptance artifact: not created.
- Authenticated account preflight: not performed.
- Account query or mutation: not performed.
- Demo or Production order: not sent.

Next gate: `INDEPENDENT_GATE_1B_V1_8_DELTA_ACCEPTANCE`.
