# NT-GATE-1B v1.9 Authenticated Read-Only Preflight Delta

Protocol version: `1.9`

Base: `3422636679c71c73f4dbb10f3217dbcbd463c865`

Status: implementation candidate only; independent acceptance and Demo execution
are not included.

## Frozen boundary

`TRADING_SEMANTICS_DELTA = NONE`

The v1.8 order remains frozen: `ETHUSDT`, `BUY`, `LIMIT`, `GTX`, fresh best bid
multiplied by `0.99`, `MAX_NOTIONAL_USDT <= 25`, no amend, isolated margin
required, leverage `1x` required, auto-add margin off, Demo USD-M only, and no
Production fallback. This delta does not change the existing mutation front
door, `AuthorizationRecord`, artifact schema, or order lifecycle.

## Isolated capability

The dedicated entrypoint is:

```text
python scripts/run_gate_1b_v1_9_readonly_preflight.py --confirm-demo-only
```

It reuses the existing hidden interactive HMAC/Ed25519 prompt, rejects
credential-bearing environment variables, disables core dumps, prints no
credential or signature, writes no evidence, and accepts no caller-selected
origin, symbol, endpoint, or method.

The only origin is `https://demo-fapi.binance.com`. The complete authenticated
allowlist is:

- `GET /fapi/v1/symbolConfig` with exact `symbol=ETHUSDT`;
- `GET /fapi/v1/openOrders` with exact `symbol=ETHUSDT`;
- `GET /fapi/v3/positionRisk` with exact `symbol=ETHUSDT`.

The endpoints are documented by Binance as signed `USER_DATA` GET operations.
The first supplies margin type, leverage, and auto-add-margin state; the second
supplies current open orders for the symbol; the third supplies current
position state.

Every `POST`, `PUT`, and `DELETE` is rejected before the signed client. Every
non-allowlisted GET, non-Demo origin, missing or different symbol, and extra
parameter is also rejected before network access. The signed leaf has a GET-only
method and imports no authorization, intent, execution, or order-lifecycle
module. It has no retry and no Production fallback.

## Explicit safe blocks

`POSITION_RISK_CONTROL = UNRESOLVED_SAFE_BLOCK`

No inference is made from `positionControlSide=NONE` or from authenticated
position rows. The read-only result cannot authorize an order.

`MAX_NUM_ORDERS = AUTHENTICATED_STATE_AVAILABLE_NOT_EVALUATED`

The entrypoint can return the current `ETHUSDT` open-order count for a later
gate. This implementation does not claim the account satisfies the public
limit and does not change order authorization.

## Independent machine acceptance artifact

Independent acceptance of this v1.9 candidate uses a credential-free, offline
artifact verifier:

```text
python scripts/verify_gate_1b_v1_9_acceptance.py --artifact <owner-only-artifact-path>
```

The v1.9 artifact must carry `protocol_version = 1.9`, an independent reviewer
identity, a v1.9 PASS verdict, P0--P3 finding counts, the exact reviewed
candidate SHA, the matching `protocol_commit`, the SHA-256 of this protocol
document as tracked at that candidate, and a canonical artifact digest. The
verifier derives the candidate SHA and protocol bytes from local Git objects;
it does not read a working-tree protocol file as authority.

No final-accepted tag is an input to the v1.9 verifier. A v1.6 legacy artifact
has no v1.9 protocol version and therefore cannot satisfy this acceptance path.
The real artifact and any final certification ref are produced only by a fresh
independent reviewer after the candidate is committed; implementation and tests
may use synthetic fixtures only.

## Explicit non-changes

- Existing mutation/order path: unchanged.
- Existing v1.6 artifact verification path: preserved only for explicit v1.6
  runtime verification; it cannot replay into v1.9 acceptance.
- Real credentials: not used during implementation or tests.
- Authenticated account query: not sent during implementation or tests.
- Account mutation, Demo order, or Production access: not performed.

Next gate: `INDEPENDENT_GATE_1B_V1_9_DELTA_ACCEPTANCE`.
