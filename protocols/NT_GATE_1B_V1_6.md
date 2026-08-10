# NT-GATE-1B v1.6 Demo Mutation Protocol

Protocol version: `1.6`

Status at freeze: `FROZEN_OPTION_A_APPROVED`

Frozen: `2026-08-10` (Asia/Shanghai)

Candidate base commit: `88a82b238415927e9f802e9605bfa4c5ed7f6a31`

Candidate base tree: `dfeb5d5203ac75d6c9a001a3c924ad6568ba01cd`

Parent protocol: `nt-gate-1b-v1.5-protocol` at
`0c4a184c30ee2ee8bda8beaee35c1e19798eb5a9`

Parent Auth result:
`GATE1B_V1_5_AUTH_CLOSEOUT_RESULT = PASS_READY_FOR_DEMO_TRADING_AUTHORIZATION`

Protocol tag: `nt-gate-1b-v1.6-protocol`

Parent closed versions:

- `NT-GATE-1B v1.3`: permanently `STOP` at `nt-gate-1b-v1.3-stop`;
- `NT-GATE-1B v1.4`: permanently `STOP` at `nt-gate-1b-v1.4-stop`;
- `NT-GATE-1B v1.5`: `PROTOCOL_ONLY` at `nt-gate-1b-v1.5-protocol`, with Auth closeout
  `PASS_READY_FOR_DEMO_TRADING_AUTHORIZATION`.

User decision bound to this freeze: `OPTION_A_APPROVED`. The user explicitly approved the
bounded `ETHUSDT`, `10 USDT` maximum, BUY LIMIT GTX at the fresh best bid discounted by
1%, no modify, existing ISOLATED/1x/auto-add-off required, no account correction, and the
non-zero Demo accidental-fill risk plus the bounded exactly-owned containment path.

This freeze authorizes no credential access, authenticated request, Demo mutation,
order, cancel, fill, account-setting change, or production connection. A later
credential-bearing session requires separate explicit authorization. The thin runtime
implementation and offline tests are committed only after this frozen protocol commit
and annotated tag, without changing the tagged protocol bytes.

## 1. Objective and claim boundary

Prove the smallest useful Binance USDⓈ-M Futures Demo mutation lifecycle:

`fresh preflight -> create one passive order -> query -> cancel -> confirm -> final clean`

This is a REST protocol and request/evidence contract. It does not start the existing
`TradingNode`, run `FixedTargetStrategy`, test alpha, test profitability, validate the old
ten-scenario execution matrix, or certify full Gate 1B execution readiness.

The normal path has one accepted probe order, zero fills, and exactly two mutation
requests. Modify is not required: create, query, cancel, and terminal confirmation are
sufficient to prove the first bounded mutation checkpoint.

## 2. Activation and freeze boundary

The candidate becomes eligible for a runtime only after all of the following:

1. The user explicitly approves the economic and accidental-fill risk in section 20.
2. A final `protocols/NT_GATE_1B_V1_6.md` is committed with no unresolved option.
3. An annotated `nt-gate-1b-v1.6-protocol` tag points to that exact protocol commit.
4. The thin runtime implementation and offline tests are committed only after the frozen
   protocol commit/tag, without changing the tagged protocol bytes.
5. Offline protocol integrity, request-guard, quantity, fake-lifecycle, evidence-schema,
   complete regression, lint, format, and scope checks pass.
6. A later credential-bearing session receives separate explicit Demo mutation
   authorization.

No candidate commit or untagged protocol authorizes network execution.

### 2.1 Reuse and implementation boundary

The repository can reuse, unchanged, the Demo/production and credential-name guards in
`src/global_quant/gate1b/safety.py`, the HTTP API construction pattern in
`demo_preflight.build_demo_http_apis`, and the pinned Nautilus `1.230.0` typed methods for
new/query/cancel order, current position mode, symbol configuration, open regular/algo
orders, user trades, exchange information, and book ticker.

The existing `build_demo_node`, `FixedTargetStrategy`, Gate 1A ledger/coordinator,
`credential_prompt.run_prompted_preflight`, and v1.4 runner/evidence-path logic are not this
protocol's runtime: they load multiple symbols or strategy behavior, use larger order/retry
budgets, omit this account contract, or bind the wrong protocol/version.

A later approved implementation therefore requires a thin REST runner plus two bounded
extensions, not a new execution architecture:

1. a typed mark-price adapter plus an exact-field `/fapi/v2/account` response parser which
   preserves and validates `canTrade`, `multiAssetsMargin`, balances, and every position;
   the pinned Nautilus account struct omits critical raw fields and is not sufficient for
   this proof; and
2. a transport wrapper which proves a `5 s` timeout, redirects disabled, system/environment
   proxies disabled, exact-origin pinning, and suppression of signed URLs/raw responses at
   DEBUG/TRACE.

If either property cannot be mechanically enforced, implementation stops before a
credential-bearing session. This candidate contains only offline contract helpers; it does
not claim that the current adapter already satisfies those missing transport properties.

## 3. Exact environment contract

The only permitted venue is Binance USDⓈ-M Futures Demo:

- environment: `BinanceEnvironment.DEMO`;
- account type: `BinanceAccountType.USDT_FUTURES`;
- scheme: `https`;
- host: `demo-fapi.binance.com`;
- port: `443`;
- origin: `https://demo-fapi.binance.com`;
- REST only; no WebSocket is required for this checkpoint.

The runner must reject before DNS, socket, or HTTP creation:

- `fapi.binance.com`, any Binance production host, Spot, COIN-M, legacy Testnet, or any
  unlisted host;
- a custom base URL, proxy, redirect, alternate port, or scheme downgrade;
- any request method/path not in section 12;
- any endpoint resolved differently from the pinned adapter contract.

Redirect following is disabled. The request audit must record every contacted origin and
prove `production_contacted = false`.

## 4. Credential boundary

The v1.5 human-operated boundary remains unchanged, with one necessary process-lifecycle
clarification for mutation evidence:

- the user launches a credential-free supervisor in a real Terminal session that the agent
  does not control, observe, attach to, record, or inspect;
- that supervisor starts a child with no credential values in the child environment; the
  child itself performs the existing non-echoing prompt and guarded owner-only Ed25519 file
  validation, so the supervisor never receives a key or secret;
- no credential value or identifier enters chat, agent context, command arguments, parent
  environment, shell history, Git, logs, evidence, tracebacks, screenshots, or hashes;
- the child writes only a sanitized pre-exit bundle and then terminates; the credential-free
  supervisor uses `waitpid`/equivalent to attest the child exit, verifies no child remains,
  and only then writes the process-exit attestation, final hashes, and verdict;
- the agent may inspect only the supervisor-finalized sanitized evidence after both steps;
- both processes must run above DEBUG/TRACE and must not retain signed URLs, headers, raw
  responses, or raw exceptions;
- process exit is part of credential cleanup because clearing a Python variable is not
  secure erasure.

The child cannot self-attest that it has already exited. Missing supervisor attestation,
exposure, or failed cleanup is `STOP_CREDENTIAL_ISOLATION`.

## 5. Account-state preconditions

All conditions are read-only assertions immediately before durable intent:

- Futures trading permission is enabled;
- one-way mode: `dualSidePosition = false`;
- single-asset mode: `multiAssetsMargin = false`;
- every USDⓈ-M position is zero;
- global regular open orders are empty;
- global algo open orders are empty;
- `ETHUSDT.marginType = ISOLATED`;
- `ETHUSDT.leverage = 1`;
- `ETHUSDT.isAutoAddMargin = false`;
- server-time skew is at most `5000 ms` in absolute value;
- `GET /fapi/v2/account` proves the virtual Demo USDT wallet is positive;
- the same response proves `availableBalance >= limit_price * quantity` after derivation.

The mode proof is the dedicated typed `GET /fapi/v1/positionSide/dual` response. The
single-asset, permission, balance, and global-position proof is an exact allowlist parse of
the raw `GET /fapi/v2/account` response; absent or discarded `multiAssetsMargin` or
`positions` fields are a hard STOP. `GET /fapi/v1/symbolConfig` separately proves the
symbol-level margin, leverage, and auto-add settings. The current Nautilus account model
must not be treated as proof merely because its decoder accepted a response.

The margin, leverage, position, multi-assets, and auto-add settings are never corrected by
this protocol. Any mismatch stops before order creation. A desired account-setting change
requires a separate versioned protocol and separate authorization.

## 6. Symbol policy

The only symbol is `ETHUSDT`. There is no runtime symbol argument or fallback.

The choice is based on the v1.5 Auth result which already verified `ETHUSDT` as available,
and on minimizing quantity-granularity risk under the new `10 USDT` cap. `BTCUSDT` remains
out of scope even though Auth also verified it.

The fresh exchange-information snapshot must prove:

- `symbol = ETHUSDT`;
- `status = TRADING`;
- `contractType = PERPETUAL`;
- `quoteAsset = USDT` and `marginAsset = USDT`;
- `LIMIT` and `MARKET` are supported;
- `GTX` is supported;
- the raw filter array contains exactly one usable `PRICE_FILTER`, `LOT_SIZE`,
  `MARKET_LOT_SIZE`, `MIN_NOTIONAL`, and `PERCENT_PRICE` contract;
- duplicate/conflicting required filters and any applicable LIMIT or MARKET filter not
  understood by the frozen parser are absent.

If the venue replaces `MIN_NOTIONAL` with another contract, returns conflicting filters,
or introduces an applicable LIMIT or MARKET filter which the frozen validator cannot
interpret, the runner stops before mutation. It does not rewrite the protocol from live
data.

## 7. Price, quantity, and rounding policy

All values originate from fresh Demo public data and are parsed directly from decimal
strings into `Decimal`. Float arithmetic and display-precision fields are forbidden.

### Fresh inputs

- best bid and best ask: `GET /fapi/v1/ticker/bookTicker?symbol=ETHUSDT`;
- mark price: `GET /fapi/v1/premiumIndex?symbol=ETHUSDT`;
- filters: the same-session `GET /fapi/v1/exchangeInfo` snapshot.

The book and mark responses must complete no more than `1000 ms` before the create request,
with a positive spread and a per-request round trip no greater than `1000 ms`. Their
monotonic observation time is bound into the derivation proof; the pre-I/O create guard
adds time spent persisting the intent/ledger and recomputes age at reservation. Stale,
missing, crossed, non-finite, or non-positive data stops the run.

### Frozen price formula

1. `raw_price = best_bid * 0.99` (a `100 bps` passive discount).
2. `limit_price` is rounded down to the greatest valid PRICE_FILTER lattice point:
   `minPrice + n * tickSize`, where `n` is a non-negative integer.
3. Prove `minPrice <= limit_price <= maxPrice`.
4. Prove `limit_price < bestAsk`.
5. For the frozen BUY side, prove
   `limit_price <= markPrice * PERCENT_PRICE.multiplierUp`.

`multiplierDown` is retained in evidence. Under the official frozen BUY rule it constrains
SELL rather than BUY; it is not silently repurposed as a new lower bound. Any official
contract drift stops the run and requires a new protocol version. This side-specific rule,
and the `minPrice + n*tickSize` / `minQty + n*stepSize` lattices below, were rechecked
against the official USDⓈ-M common definition on `2026-08-10`; they are deliberate rather
than inferred from Spot or another venue.

### Frozen quantity formula

1. `required = max(LOT_SIZE.minQty, MIN_NOTIONAL.notional / limit_price)`.
2. `quantity` is rounded up to the least valid LOT_SIZE lattice point:
   `minQty + n * stepSize`, where `n` is a non-negative integer.
3. Prove `minQty <= quantity <= maxQty` and exact lattice alignment.
4. Prove `limit_price * quantity >= MIN_NOTIONAL.notional`.
5. Prove `limit_price * quantity <= 10.00 USDT`.

The runner must record every input and intermediate Decimal. It must not add a buffer,
round to display precision, enlarge quantity to force acceptance, raise the cap, change
the symbol, change the price offset, or retry with altered parameters. If the smallest
valid quantity is above the cap, the safe result is `INCONCLUSIVE_FILTERS_ABOVE_CAP` with
zero accepted order, not a larger trade.

The full sanitized exchange-information snapshot has its own artifact SHA-256. The parsed
LIMIT-filter fields have a second canonical contract SHA-256 recomputed from their exact
Decimal strings and cardinalities. Both hashes are bound into the derivation/intent. The
offline object helper does not by itself prove that two caller-supplied hashes describe the
same bytes: before mutation, the future runner/verifier must independently hash the retained
snapshot, parse that hash-bound artifact, recompute the canonical filter contract, and prove
the relationship. Any mismatch is STOP.

Before create, the runner also snapshots exactly one `MARKET_LOT_SIZE` and the same exactly
one `MIN_NOTIONAL` contract. For a contingency close it refreshes exchange information and
mark price, requires the refreshed applicable-filter hash to equal the durable intent's
filter-snapshot hash, and proves all of the following without rounding or enlarging:

1. `owned_residual` is positive and no greater than the probe quantity;
2. `MARKET_LOT_SIZE.minQty <= owned_residual <= MARKET_LOT_SIZE.maxQty`;
3. `(owned_residual - MARKET_LOT_SIZE.minQty) % MARKET_LOT_SIZE.stepSize == 0`;
4. the mark-price observation time is bound into the proof and its age, recomputed again at
   the emergency-close request reservation, is at most `1000 ms`;
5. `fresh_mark_price * owned_residual >= MIN_NOTIONAL.notional`, following the official
   USDⓈ-M rule that MARKET notional uses mark price; and
6. there is exactly one understood applicable market-size/notional filter and no unknown or
   conflicting applicable MARKET filter; the canonical parsed MARKET contract hash must
   match the proof in addition to the full snapshot hash.

The exact residual is either valid unchanged or is not automatically closeable. The runner
must not claim that every partial fill is closeable, round a residual, or add quantity.
Failure is `BLOCKED_CLEANUP_UNPROVEN` and requires human Demo inspection.

## 8. Exact order contract

The sole normal-path request body, excluding timestamp/signature, is:

- `symbol = ETHUSDT`;
- `side = BUY`;
- `type = LIMIT`;
- `timeInForce = GTX`;
- `quantity =` section 7 result;
- `price =` section 7 result;
- `positionSide = BOTH`;
- `reduceOnly = false`;
- `newOrderRespType = ACK`;
- `recvWindow = 5000`;
- `newClientOrderId =` section 9 result.

The following fields are absent from the create payload: `priceMatch`, `closePosition`,
`stopPrice`, `activationPrice`, `callbackRate`, `workingType`, `priceProtect`,
`goodTillDate`, `selfTradePreventionMode`, and modify fields.

`selfTradePreventionMode` is deliberately not sent. This is a frozen, recorded decision,
not a reliance on an undocumented implicit default: `GTX` is a post-only time-in-force
which cannot cross the spread at acceptance, so self-trade prevention cannot fire at
acceptance regardless of the account-level default. The protocol does not depend on any
account-level `selfTradePreventionMode` default being any particular value at acceptance.
A resting fill risk remains and is handled solely by section 14 containment; any fill
makes the run non-PASS regardless of self-trade semantics. The future runner contract
and evidence must record this frozen decision and must not add a `selfTradePreventionMode`
field to the probe or contingency payloads.

`GTX` proves only that the order cannot take liquidity at acceptance. It does not guarantee
that a resting order will remain unfilled. A later market move can produce a partial or full
fill; section 14 contains that risk.

## 9. Durable intent and duplicate protection

Each later explicit runtime authorization creates one non-secret ID of the form
`g1b16-{16 lowercase hex}` in an owner-only local authorization manifest. The model may not
invent a replacement ID to obtain another attempt. Before credentials, the runner scans
all retained v1.6 attempt records for that ID. Any existing intent/consumed record makes
the authorization recovery-only or consumed; a new nonce cannot bypass it.

Before `POST /fapi/v1/order`, the runner atomically writes and `fsync`s an owner-only intent
record containing the authorization ID, protocol binding, runtime binding, session nonce,
exact order derivation inputs and payload, full filter-snapshot hash, and request budget. The
protocol binding includes the annotated tag object, peeled protocol commit, proposed tag
name, protocol version, and SHA-256 of the exact tagged protocol bytes. No credential is
present. Persisting this intent consumes the sole create attempt even if the process crashes
before a response. Its `intent_sha256` is recomputed from canonical sorted JSON covering
those fields; the order is recomputed from the hash-bound book, mark, and parsed filter
inputs, rather than accepted as an arbitrary prebuilt payload.

Every ownership proof is bound to the SHA-256 of the exact persisted GET reservation which
produced it. An order-open proof requires a same-client-ID `GET /fapi/v1/order`. A residual
position proof requires persisted post-create reservations for the terminal order, relevant
user trades, full account/positions, refreshed exchange information, and fresh mark price.
After crash recovery those reads must be performed again; caller-injected status booleans or
proof objects without the source request digests grant no mutation permission.

The session nonce is exactly 16 lowercase hexadecimal characters generated locally from
at least 64 bits of operating-system randomness. The client order ID is:

`g1b16-{runtime_commit_first_10}-{session_nonce_16}-01`

It is exactly 36 allowed characters, non-secret, deterministic for recovery, and retained
in sanitized evidence.

Before the first intent for the authorization, query this exact ID and classify the lookup
as `CONFIRMED_NOT_FOUND`, `FOUND`, or `UNKNOWN`:

- venue-confirmed not found plus clean global state and no prior attempt record: create once;
- `NEW` or `PARTIALLY_FILLED`: recovery/cleanup only, never create;
- any non-zero `executedQty`, `PARTIALLY_FILLED`, or `FILLED`: reconcile fill/position and
  cleanup only, never create;
- a zero-fill terminal state: the session is consumed, never create;
- timeout, ambiguous not-found, unknown response/status, or unclean global state: stop and
  reconcile, never create.

An ambiguous create response is never re-POSTed. Recovery queries the same client ID. A
new nonce is not generated to escape a duplicate or failed session. Automatic reruns are
forbidden; another economic attempt requires new explicit authorization.

## 10. Frozen lifecycle

The only normal lifecycle is:

1. Validate clean, committed runtime and frozen protocol binding.
2. Validate the exact Demo endpoint and empty parent credential environment.
3. Validate the one-time authorization, absence of prior attempt records, server time,
   account/config/balance, symbol/filters, global positions/orders, and explicit duplicate
   lookup outcome.
4. Last, fetch the fresh book and mark, derive the exact order, revalidate available
   balance, and require both responses to remain within the `1000 ms` freshness bound.
5. Atomically persist and `fsync` the exact durable intent and client order ID.
6. Reserve the create slot in the write-ahead request ledger, atomically persist and
   `fsync` the canonical request reservation, then send exactly one `POST /fapi/v1/order`.
7. Query the same client order ID. Prove every returned field matches the frozen request,
   status is `NEW`, and `executedQty = 0`.
8. Immediately reserve/fsync then send one targeted `DELETE /fapi/v1/order` using the same
   symbol and client order ID. There is no intentional dwell.
9. Query once for the terminal observation; use the sole read retry only after a safe read
   failure. PASS requires `CANCELED` and `executedQty = 0`.
10. Query final global positions/balance, regular open orders, algo open orders, ETH
    order/trade history, symbol configuration, and dedicated position mode.
11. The credential child atomically writes a sanitized pre-exit bundle and terminates.
12. The credential-free supervisor waits for child exit, verifies no child remains, writes
    process-exit attestation, recomputes the complete manifest/hashes, and emits the verdict.

There is no modify step, second probe, second symbol, strategy loop, WebSocket, funding
wait, or background process.

## 11. Time, request, retry, and mutation budgets

- credential-bearing network phase: at most `180 seconds` after hidden input completes;
- create forbidden after elapsed `60 seconds`, preserving `120 seconds` for cleanup;
- single HTTP request timeout: `5 seconds`;
- signed `recvWindow`: `5000 ms`;
- accepted-to-first-cancel request: at most `3 seconds`;
- total HTTP attempts, including reads and mutations: at most `31`;
- before create, at least `18` post-create HTTP slots must remain after counting the create;
- post-create read attempts: at most `15`, separately accounted so reads cannot consume the
  three hard mutation-cleanup slots (two targeted cancels and one owned-position close);
- read-only retry: at most `1` retry per logical read and at most `1` aggregate retry;
- create retry: `0`;
- emergency-close retry: `0`;
- modify requests: `0`;
- account-setting mutation requests: `0`;
- probe create requests: at most `1`;
- targeted cancel requests: normally `1`, hard maximum `2`;
- contingency reduce-only close requests: at most `1`;
- normal mutation requests: exactly `2` (`create + cancel`);
- absolute mutation-request ceiling: `4`;
- normal order submissions: exactly `1`;
- absolute order-submission ceiling including contingency close: `2`.

The second cancel is not a blind transport retry. It is authorized only after a read query
proves the exact owned order is still `NEW` or `PARTIALLY_FILLED` after an ambiguous first
cancel response. Before every mutation, the next immutable counter state is written and
`fsync`ed; every read attempt is reserved the same way. Each canonical reservation records
the method, path, purpose, exact non-signature parameters, monotonic elapsed time, retry
index, next lifecycle stage, counters, and a recomputable request digest. An ambiguous
response never rolls a slot back. Recovery reconstructs stage, retry usage, deadlines, and
all budgets from those records; caller-supplied stage flags are not trusted. No request is
sent after any hard request, retry, mutation, or runtime budget expires.

A read reservation is not an ownership source. After an allowlisted response is parsed and
validated, the runner explicitly promotes that exact reservation digest to a successful
source. A failed or ambiguous read removes the pending/source digest before minting a
same-logical-request retry token. Therefore a failed response cannot authorize cancel or
emergency close.

The exact lifecycle scheduler additionally rejects any unneeded or out-of-stage read. The
offline parameter guard separately caps post-create reads at 15 so even a caller error cannot
exhaust the three mutation-cleanup slots; the future fake-HTTP runner test must prove that
repeated allowlisted reads cannot block an exact owned cancel.

The `3 s` accepted-to-first-cancel value is a PASS deadline, not permission to abandon an
owned order. If it is exceeded, the run becomes STOP but the exact targeted cleanup remains
authorized within the `180 s` hard runtime and request/mutation ceilings. The create deadline
and hard runtime never authorize a new order; after the hard runtime, only a new
human-operated recovery session may continue cleanup.

The exact post-create reserve is:

| Branch component | Slots | Frozen use |
| --- | ---: | --- |
| Normal lifecycle | 9 | probe query, cancel, terminal query, final regular/algo orders, user trades, account/positions, symbol config, position mode |
| Ambiguous cancel contingency | 2 | owned-order query, query-proven second targeted cancel |
| Owned-fill containment contingency | 6 | pre-close account/position ownership proof, refreshed exchange info, fresh mark, reduce-only close, close query, post-close user-trade reconciliation |
| Aggregate read retry | 1 | one failed read only |
| **Hard reserve** | **18** | all components above |

The normal PASS pre-create schedule has exactly eleven logical reads: time, position mode,
symbol config, account/positions/balance, regular orders, algo orders, exchange info,
duplicate order lookup, relevant user trades, fresh book, and fresh mark. With the sole read
retry, create, and the 18-slot reserve, the absolute bound is `31`. The `120 s` cleanup
window is greater than `18 * 5 s`, leaving time for durable writes and local validation.
Raising the read ceiling does not authorize another order or mutation. A normal PASS has
exactly `21` HTTP attempts plus zero or one proven read retry.

## 12. Exact REST allowlist

Only the following method/path pairs may reach the exact Demo origin:

### Read-only

- `GET /fapi/v1/time`;
- `GET /fapi/v1/exchangeInfo`;
- `GET /fapi/v1/ticker/bookTicker`;
- `GET /fapi/v1/premiumIndex`;
- `GET /fapi/v1/positionSide/dual`;
- `GET /fapi/v1/symbolConfig`;
- `GET /fapi/v1/openOrders`;
- `GET /fapi/v1/openAlgoOrders`;
- `GET /fapi/v1/order`;
- `GET /fapi/v1/userTrades`;
- `GET /fapi/v2/account`.

### Mutating

- `POST /fapi/v1/order`: one probe create, or one separately classified contingency
  reduce-only close;
- `DELETE /fapi/v1/order`: targeted cancel of the exact owned client order ID.

Before signing, every read parameter map is exact: time/exchange-info have no business
parameters; book/mark use only `ETHUSDT`; global account, position-mode, and open-order reads
omit a symbol; symbol/order/trade reads use only the frozen symbol/client ID plus
`recvWindow`.
Timestamp and signature are transport fields and are never retained.

`PUT`, batch orders, cancel-all, auto-cancel-all, leverage, margin-type, position-mode,
multi-assets, transfers, deposits, withdrawals, and every other method/path are forbidden.
The request guard must authorize method, path, purpose, stage, ownership proof, and budget
before I/O, not merely count them afterward.

## 13. Expected and unexpected mutation semantics

Normal expected mutation requests are exactly:

1. create the one owned passive probe order;
2. cancel that same owned probe order.

Expected visible state transitions are only `NEW -> CANCELED`. Expected economic events
are zero fills, zero fee, zero funding, and zero position delta.

One contingency reduce-only close and one proven-still-open cancel are narrowly authorized
cleanup requests. Their use prevents PASS and is reported separately; they are not
reclassified as a successful normal lifecycle.

Unexpected mutation includes:

- any fill, fee, funding, or position change;
- any unknown/external order, trade, or account-state change;
- any account-setting change;
- any method/path/host outside section 12;
- a second probe create, changed client ID, modified parameter, or cap expansion;
- any mutation that lacks a durable owned intent and request-guard authorization.

`unexpected_mutations = 0` is mandatory for PASS. The protocol also records the limitation
that snapshots cannot disprove a concurrent actor which completes an invisible round trip;
exclusive use of the Demo account during the bounded session is a human precondition.

## 14. Cleanup and abort contract

Cleanup targets only the exact client order ID and positions cryptographically/economically
mapped to its fills. Cancel-all and unknown-position closure are forbidden.

### Order still open

Query by client ID, send the targeted cancel, and query terminal state. After an ambiguous
cancel response, a second cancel is permitted only if a fresh query proves the order still
open. A missing or contradictory response is not treated as success.

### Partial or full unexpected fill

1. Mark the run `STOP_UNEXPECTED_FILL`; it can never PASS.
2. Cancel the owned remainder if a query proves it open.
3. Reconcile order, trades, executed quantity, and the full account position.
4. Only if the residual `ETHUSDT` long equals the proven owned fill, no other activity is
   present, the probe is terminal with zero open remainder, and current MARKET filters
   prove the exact quantity closeable, permit one
   `SELL MARKET`, `positionSide=BOTH`, `reduceOnly=true` contingency order. Its complete
   pre-signing payload is exact: owned residual quantity serialized from Decimal,
   `newOrderRespType=ACK`, `recvWindow=5000`, no price/TIF/optional fields, and client ID
   `g1b16c-{runtime_commit_first_8}-{session_nonce_16}-1`.
5. Query final global state. Even successful containment retains the STOP verdict.

The contingency close has zero POST retry. An ambiguous response is reconciled only by
querying its deterministic close client ID and global position/trades; it is never re-POSTed.

If quantity ownership, closeability, order status, response semantics, or final cleanup
cannot be proven, do not guess or enlarge the close. Record
`BLOCKED_CLEANUP_UNPROVEN`, preserve complete sanitized evidence, and require human Demo
inspection. The final verdict cannot PASS while any order or position may remain.

Timeout, process crash, or `SIGKILL` does not prove cleanup. Recovery requires the durable
intent plus a new human-operated credential session using the same client ID. No signal
handler is claimed to contain `SIGKILL`.

## 15. Fail-closed conditions

Stop before mutation for any of:

- endpoint, environment, redirect, proxy, or allowlist mismatch;
- dirty/uncommitted runtime, protocol-tag failure, or runtime/evidence binding failure;
- credential isolation or logging failure;
- account mode, margin type, leverage, auto-add, permission, balance, or clean-state
  mismatch;
- symbol, status, contract, asset, order-type, TIF, or filter drift;
- stale/invalid book, mark, server time, or Decimal derivation;
- minimum valid order above `10 USDT`;
- duplicate/durable-intent ambiguity;
- request, timeout, retry, mutation, or order-count budget exhaustion.

Stop and clean up after mutation for any of:

- response fields do not match the frozen request;
- unknown, rejected, partial, filled, expired, delayed, duplicate, or contradictory state;
- unexpected position, order, trade, fee, funding, or mutation;
- cancellation or final global state cannot be proven;
- evidence, hash, or credential cleanup fails.

The runner never alters protocol parameters to turn a failure into PASS.

## 16. Evidence contract and retention

Evidence remains local and ignored under a unique, non-overwriting directory such as:

`evidence/runtime/gate1b-v1.6-mutation-{runtime_sha12}/{session_nonce}/`

Minimum artifacts:

- `intent.json`: durable pre-create intent and budgets;
- `authorization.json`: non-secret one-time authorization ID and consumed/recovery state;
- `request-ledger.json`: write-ahead/fsynced request reservations, lifecycle stages,
  retry/deadline fields, counters, and request digests;
- `preflight.json`: sanitized environment/account/symbol/filter/book/mark state;
- `requests.jsonl`: ordered allowlisted request attempts and sanitized outcomes;
- `lifecycle.jsonl`: state transitions and timestamps;
- `final-account.json`: global final positions/orders/config/balance summary;
- `manifest.json`: artifact sizes and SHA-256 values;
- `child-pre-exit.json`: sanitized child completion/cleanup state without a PASS claim;
- `process-exit.json`: credential-free supervisor attestation after child termination;
- `verdict.json`: supervisor-finalized machine verdict and finding counts;
- detached SHA-256 files for manifest and verdict.

The evidence must record at least:

- protocol version/status, annotated tag object, peeled protocol commit, protocol hash;
- one-time authorization ID, prior-attempt scan, durable intent hash, and every persisted
  pre-I/O request-ledger state/source-request digest;
- runtime commit, tree, branch, tracked-clean status, pre/post HEAD equality, committed source
  blob hashes, dependency versions, and exact runner invocation mode;
- exact Demo origin and all contacted origins, with `production_contacted`;
- server time, local midpoint, skew, request round trips, and freshness ages;
- exact symbol/account policy results;
- full exchange filter snapshot/artifact hash plus independently recomputed canonical parsed
  LIMIT and MARKET filter-contract hashes;
- best bid/ask, mark price, every Decimal derivation step, rounded price/quantity, and
  resulting notional;
- frozen order fields and deterministic client order ID;
- request method/path/purpose, monotonic and server timestamps, timeout/retry counters,
  expected/contingency/unexpected mutation counts, and total order count;
- lifecycle response allowlist fields, status sequence, executed quantity, and cleanup;
- initial/final global positions, regular orders, algo orders, relevant order/trade history,
  wallet delta, fee, funding, and symbol configuration;
- credential redaction, child exit status, supervisor process-exit attestation, final verdict,
  P0/P1/P2/P3, and hashes.

The client order ID may be retained because it is protocol-generated and non-secret. Venue
order/trade IDs are retained only as domain-separated SHA-256 representations, for example
`SHA256("binance-demo-order-id\\0" || decimal_id)`. API keys, account aliases/identifiers,
signatures, signed query strings, credential hashes, headers, raw responses, raw exceptions,
and PEM material are never retained.

Every retained response is reconstructed from an explicit field allowlist. Only the
credential-free supervisor may issue the final verdict after the child has exited. A final
secret scan and hash recomputation must pass before evidence review. The offline
`validate_lifecycle_pass()` helper validates only a sanitized summary contract; it is not
the final arbiter. The future verifier independently parses hash-bound artifacts and
recomputes booleans, counters, parameters, transitions, deltas, and verdict.

## 17. Committed-runtime binding

Runtime evidence cannot claim an older commit while executing working-tree code.

Before credentials and after final evidence, the runner must use Git commands compatible
with linked worktrees to prove:

- `HEAD^{commit}` and `HEAD^{tree}`;
- tracked worktree clean;
- `git ls-files --others --exclude-standard` is empty, so an untracked source, script, or
  import-shadow file cannot enter the runtime (ignored evidence retention remains allowed);
- annotated protocol tag exists and peels to the frozen commit;
- protocol tag is an ancestor of runtime commit;
- current protocol bytes equal tagged bytes;
- every runtime source/config file byte equals its committed blob;
- the entrypoint and every loaded project module resolve through recorded `__file__` paths,
  are tracked by `git ls-files --error-unmatch`, and equal their `HEAD` blobs;
- pre-run and post-run commit/tree are unchanged.

The old `runner._git_commit()` file-reading shortcut and v1.4 config/evidence hash are not
valid v1.6 binding mechanisms and must not be reused.

## 18. Machine-verifiable acceptance criteria

PASS requires all of the following:

1. The exact Demo origin was the only contacted origin; production was never contacted.
2. Clean committed runtime, tree, protocol tag/commit/bytes, and evidence are bound.
3. Credentials never entered agent context, Git, evidence, or logs; process cleanup passed.
4. Fresh account state satisfied one-way, single-asset, ISOLATED, 1x, auto-add off, globally
   flat, and globally no regular/algo orders.
5. Mutation requests equal exactly two and never exceed the hard ceiling.
6. The sole probe order exactly matches every frozen field.
7. Price and quantity derivation is Decimal-reproducible.
8. Every applicable exchange filter is proven satisfied and venue ACK matches.
9. Lifecycle is auditable as exact owned `NEW -> CANCELED`, with `executedQty = 0`.
10. Read retry is within one; create, close, and blind mutation retry are zero.
11. Targeted cleanup and terminal query succeeded within all time limits.
12. Final global positions, regular orders, and algo orders are empty, with no protocol-
    attributable fee, funding, wallet, or position delta.
13. Unexpected mutation, fill, fee, funding, and position delta all equal zero.
14. Artifact, manifest, and verdict hashes are present and independently reproducible.
15. Every failure path fails closed and does not change a parameter or create a second
    probe order.
16. Offline request-guard, fake lifecycle, evidence-schema, regression, lint, format,
    credential scan, and Git scope checks pass.
17. The independent runtime reviewer reports `P0=0` and `P1=0`.

Profit, PnL, strategy behavior, market prediction, and production readiness are not
acceptance criteria.

## 19. Verdict and review semantics

- `PASS_CREATE_QUERY_CANCEL_ZERO_FILL`: every section 18 item passes.
- `INCONCLUSIVE`: limited to a pre-create external Demo outage/throttle/read-unavailability
  or `INCONCLUSIVE_FILTERS_ABOVE_CAP`, with zero mutation requests and a proven globally
  clean account. Any response to the probe POST, including a venue rejection, is `STOP`.
- `STOP`: any safety, isolation, contract, state, fill, evidence, or engineering failure.
- `BLOCKED_CLEANUP_UNPROVEN`: an order/position may remain or cleanup cannot be proven.

The candidate-design checkpoint may pass with complete offline evidence and Codex
self-review when Work Buddy is unavailable. The future credential-bearing mutation runtime
has a stricter rule: independent read-only review is mandatory for runtime PASS. If that
reviewer is unavailable, stop at `READY_FOR_INDEPENDENT_REVIEW`; do not invent reviewer
approval or apply the v1.5 protocol-only fallback to a mutating run.

## 20. User approval options

### OPTION_A — recommended bounded mutation

Approve the exact candidate: `ETHUSDT`, `10 USDT` maximum, BUY LIMIT GTX at the fresh best
bid discounted by 1%, no modify, existing ISOLATED/1x/auto-add-off required, and no account
correction. The benefit is actual proof of create/query/cancel with minimum scope. The
downside is a non-zero accidental Demo fill risk after the order rests. The protocol permits
only bounded owned-position containment, and any fill makes the run non-PASS.

### OPTION_B — no fill or close risk

Do not authorize any real Demo order. The benefit is zero intentional mutation and zero
order-fill risk. The downside is that create/query/cancel cannot be proven and the project
remains at the protocol-only checkpoint.

Changing symbol, cap, margin/leverage correction, modify, or intentional fill is not an
inline option; it requires revising and re-reviewing this candidate before freeze.

## 21. Sole next action

The user has approved `OPTION_A`. This protocol is now frozen and annotated-tagged. The
sole next action is to implement and offline-test the thin Demo lifecycle runner that
serializes this frozen protocol, then stop at the credential-bearing Demo gate. Do not
enter a credential-bearing or mutating session without another explicit, separate
authorization.

The thin runner and its offline tests are committed only after this frozen protocol
commit/tag, without changing the tagged protocol bytes. The runner must reuse the existing
credential isolation, Demo endpoint guards, HTTP APIs, time/clock, Binance response
parsing, symbol/filter parsing, safety guards, runtime binding, evidence machinery, and
cleanup primitives; it must not rebuild a credential system, HTTP stack, execution
engine, or strategy engine.

## 22. Primary references checked for this candidate

- Binance USDⓈ-M Futures General Info and Demo origin:
  `https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info`
- Binance USDⓈ-M Futures REST Trade API for new/query/cancel order:
  `https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade`
- Binance USDⓈ-M Futures REST Account API for account and symbol configuration:
  `https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account`
- Binance USDⓈ-M Futures exchange information and filter definitions:
  `https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/common-definition`
- filter semantics rechecked: `2026-08-10` (Asia/Shanghai);
- Pinned local adapter/runtime: `nautilus-trader==1.230.0`.

## 23. Freeze record and determinism clarifications

This protocol is frozen as `FROZEN_OPTION_A_APPROVED` after the user explicitly approved
`OPTION_A`. Two determinism clarifications were resolved at freeze and are now part of
the frozen contract; they are not a redesign of v1.6 and introduce no new time-window
strategy and no new order field.

### selfTradePreventionMode policy — frozen value: `field not sent`

The probe create payload and the contingency reduce-only close payload do not include a
`selfTradePreventionMode` field. This is a deliberately frozen, recorded decision. It does
not rely on an undocumented implicit default: `GTX` is post-only and cannot cross the
spread at acceptance, so self-trade prevention cannot fire at acceptance regardless of the
account-level default. The protocol does not depend on any account-level
`selfTradePreventionMode` default being any particular value at acceptance. Resting fill
risk is handled solely by section 14 containment, and any fill makes the run non-PASS
independent of self-trade semantics. The runner contract and evidence must record this
frozen decision and must not add a `selfTradePreventionMode` field to either payload.

### recvWindow policy — frozen value: `5000 ms`

The signed `recvWindow` is frozen at `5000 ms`, reusing the v1.5 Auth-closeout verified
SIGNED-request value. It is applied identically to the probe create payload, the
contingency reduce-only close payload, and every read query parameter map. The frozen
value is recorded in the offline contract helper as `RECEIVE_WINDOW_MS = 5000` and is
bound into evidence via the canonical request reservation digests. v1.6 introduces no
new time-window strategy.

### Reviewer identity boundary

The implementer of this freeze and thin runner is Work Buddy acting as implementer, not
as an independent reviewer. Work Buddy's self-review may serve as a self-audit only. It
does not constitute the independent read-only review required before a credential-bearing
Demo mutation runtime PASS. If the repository requires independent review before a
credential-bearing run, the implementer stops at
`PASS_READY_FOR_CREDENTIAL_BEARING_DEMO_RUN_PENDING_INDEPENDENT_REVIEW` and waits for an
independent reviewer (Codex quota restored, or another independent reviewer). Work Buddy
must not fabricate independent reviewer approval.
