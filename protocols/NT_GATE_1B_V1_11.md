# NT-GATE-1B v1.11 Signed Read-Only Request Correctness Delta

Protocol version: `1.11`

Base: `e345e74d7d45ad5e1c00eb48fe1b25cb88f40736`

Status: implementation candidate only; independent acceptance and authenticated
Demo execution are not included.

## Confirmed root cause and sole runtime delta

The accepted v1.10 read-only leaf called the Nautilus
`BinanceHttpClient.sign_request` method with only `symbol=ETHUSDT`. That signer
signs the parameters supplied by its caller and appends `signature`; it does not
generate the mandatory Binance `timestamp`. The resulting three signed GET
requests therefore transmitted `symbol` and `signature` but no `timestamp`.

`SIGNED_REQUEST_CORRECTNESS_DELTA = add one validated millisecond timestamp to
the signed query parameters before HMAC signing and transmission`

The timestamp is a 13-digit millisecond integer from the credential-bound
client clock. A missing, empty, non-integer, seconds-resolution, boolean, or
out-of-range value fails closed locally before network dispatch. `recvWindow`
remains omitted, so the Binance default applies.

## Frozen trading and capability boundary

`TRADING_SEMANTICS_DELTA = NONE`

The order remains `ETHUSDT`, `BUY`, `LIMIT`, `GTX`, fresh best bid multiplied by
`0.99`, `MAX_NOTIONAL_USDT <= 25`, isolated margin, leverage `1x`, auto-add
margin off, and Demo USD-M only.

`READ_ONLY_ENDPOINT_SCOPE_DELTA = NONE`

The exact allowlist remains:

- `GET /fapi/v1/symbolConfig` with exact `symbol=ETHUSDT`;
- `GET /fapi/v1/openOrders` with exact `symbol=ETHUSDT`;
- `GET /fapi/v3/positionRisk` with exact `symbol=ETHUSDT`.

The origin remains exactly `https://demo-fapi.binance.com`. `POST`, `PUT`,
`DELETE`, non-allowlisted GETs, non-`ETHUSDT` symbols, redirects, proxies, and
Production origins remain blocked before dispatch.

`MUTATION_CAPABILITY_DELTA = NONE`

`DIAGNOSTIC_SEMANTICS_DELTA = NONE`

There is no automatic retry. Authorization, intent, execution, mutation, and
order-lifecycle modules remain unreachable from the read-only leaf.

## Candidate verification boundary

Synthetic tests must capture the final outbound request object without network
access and prove for all three endpoints:

- method, Demo origin, path, and query placement are exact;
- `symbol`, 13-digit `timestamp`, and `signature` are present and non-empty;
- `X-MBX-APIKEY` is present;
- the independently computed deterministic HMAC matches;
- the signed parameter set equals the transmitted parameter set;
- malformed required values fail closed before dispatch;
- existing method, endpoint, symbol, Production, retry, and redaction boundaries
  remain unchanged.

No real credential, authenticated Binance request, account mutation, Demo
order, Production access, final tag, final audit ref, or final acceptance
artifact may be created by this implementation task.

Next gate: `FRESH_INDEPENDENT_GATE_1B_V1_11_ACCEPTANCE`.
