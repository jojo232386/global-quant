# NT-GATE-1B v1.7 Narrow Demo Notional-Cap Delta

Protocol version: `1.7`

Status: `IMPLEMENTATION_CANDIDATE_AWAITING_INDEPENDENT_ACCEPTANCE`

Base reviewed artifact:
`1aaadcab9e95fc4b7ce6c12b7b7cd36d48042dd9`
(`nt-gate-1b-v1.6-final-accepted`)

## 1. Sole protocol delta

Gate 1B v1.7 inherits Gate 1B v1.6 unchanged except for this explicit cap:

```text
v1.6 MAX_NOTIONAL_USDT = 10
v1.7 MAX_NOTIONAL_USDT = 25
```

`25 USDT` is an absolute maximum, not a target order amount and not a value
which may be increased at runtime.

The runner must derive the smallest exchange-valid quantity from fresh public
filters and the frozen price rule. It may continue only when the resulting
notional is at most `25 USDT`. If exchange rules require more than `25 USDT`,
the result remains fail-closed as `NOTIONAL_CAP_EXCEEDED`.

## 2. Inherited frozen semantics

The following v1.6 semantics remain unchanged:

- symbol `ETHUSDT`;
- side `BUY`;
- order type `LIMIT`;
- time in force / post-only `GTX`;
- price derived from the latest best bid multiplied by `0.99`, then rounded
  down to the exchange tick from the declared filter origin;
- quantity is the smallest value satisfying `minQty`, `stepSize`, and
  `minNotional`;
- no amend;
- existing account state must already be `ISOLATED`, leverage `1x`, and
  auto-add margin off;
- no automatic account correction;
- Binance USD-M Futures Demo endpoint only, with no production fallback;
- all existing authorization, timeout, reconciliation, containment, evidence,
  and recovery behavior is unchanged.

Existing front-door paths and authorization/client-order namespaces are
retained unchanged because modifying them is outside this economic-cap delta.

## 3. Implementation-precondition market observation

Credential-free Binance USD-M Futures Demo metadata was fetched at
`2026-08-11T21:58:00Z` for `ETHUSDT`:

```text
status = TRADING
contract type = PERPETUAL
tick size = 0.01
step size = 0.001
min qty = 0.001
min notional = 20 USDT
price precision = 2
quantity precision = 3
best bid = 1882.17
best ask = 1882.51
mark price = 1882.51000000
derived GTX price = 1863.34
minimum legal quantity = 0.011
minimum legal notional = 20.49674 USDT
```

This observation proves only that the proposed `25 USDT` cap was feasible at
implementation time. Every later preflight must fetch and validate fresh
filters; future exchange-rule drift may make the cap insufficient again.

## 4. Authorization and acceptance boundary

This delta authorizes no credential access, authenticated request, account
mutation, Demo order, production connection, or cap increase beyond `25 USDT`.
It is not independently accepted until a separate Gate 1B v1.7 delta
acceptance binds its verdict to the final candidate commit.
