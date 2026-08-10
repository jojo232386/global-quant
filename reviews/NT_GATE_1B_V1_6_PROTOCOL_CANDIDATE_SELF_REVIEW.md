# NT-GATE-1B v1.6 Mutation Protocol Candidate Self-Review

Review date: `2026-08-10` (Asia/Shanghai)

Baseline: `88a82b238415927e9f802e9605bfa4c5ed7f6a31`

Branch: `codex/nt-gate1b-v1.6-protocol-candidate`

Candidate status: `CANDIDATE_NOT_FROZEN`

## Verdict

`PASS_READY_FOR_USER_PROTOCOL_APPROVAL`

This is an offline protocol-design verdict only. It does not freeze v1.6, authorize a
credential-bearing session, implement the runtime transport, or authorize any Demo or
production mutation.

## Scope reviewed

- the v1.5 Auth closeout and protocol/tag ancestry were treated as the completed baseline;
- the next natural version is v1.6 because local history already contains sequential
  v1.0-v1.5 protocol tags;
- the candidate freezes one bounded `ETHUSDT` create-query-cancel lifecycle;
- the pure offline contract helper covers account/symbol/filter/order validation, exact
  request guarding, durable attempt semantics, duplicate recovery, cleanup ownership, and
  sanitized summary validation;
- unit tests exercise happy paths and fail-closed counterexamples without credentials or
  network I/O.

No Auth Closeout, credential prompt, Binance authenticated request, Demo mutation, order,
cancel, position change, or production request was executed during this task.

## Candidate decisions

- origin: only `https://demo-fapi.binance.com`;
- symbol: only `ETHUSDT`;
- account: one-way, single-asset, existing `ISOLATED`, leverage `1`, auto-add margin off;
- account mismatch: STOP without setting correction;
- probe: one BUY LIMIT GTX, `positionSide=BOTH`, `reduceOnly=false`, ACK;
- price: fresh best bid discounted by 1%, rounded down on the exchange `minPrice` lattice;
- quantity: minimum filter-valid quantity rounded up on the `minQty` lattice, never above
  `10.00 USDT` notional;
- normal lifecycle: create, query NEW/zero fill, targeted cancel, terminal query, final
  global state;
- modify: forbidden;
- normal mutations: exactly two; hard ceiling four only to retain targeted cancel and
  exactly-owned reduce-only containment capacity;
- total HTTP ceiling: 31, with an 18-slot post-create reserve and 15-read post-create cap;
- mutation retry: zero; one aggregate read retry only after a durable exact failure proof;
- request timeout: 5 seconds; create deadline: 60 seconds; total runtime: 180 seconds;
- PASS requires zero fill, zero unexpected mutation/economic delta, and final global clean
  state.

## Reuse and deferred implementation boundary

The candidate reuses the existing Demo/production and credential-name guards, the Demo HTTP
construction pattern, and suitable pinned Nautilus typed order/account methods. It rejects
reuse of the TradingNode strategy path, old credential-prompt entrypoint, and old v1.4
runtime/evidence binding.

Before any later credential-bearing session, a thin implementation must still provide:

- an exact-field parser for raw `/fapi/v2/account` fields omitted by the pinned typed model;
- a mark-price adapter;
- mechanically proven 5-second timeout, redirects disabled, proxies disabled, exact-origin
  pinning, and signed-log suppression;
- the exact lifecycle scheduler, atomic local evidence writer, and independent hash-bound
  evidence verifier.

Failure to implement any item is a pre-credential STOP, not permission to weaken the
candidate.

## Evidence and credential assessment

The evidence contract binds protocol tag object/commit/bytes, runtime commit/tree/blob
bytes, loaded module paths, one-time authorization and durable intent, every write-ahead
request reservation, sanitized lifecycle/account artifacts, child exit, supervisor
attestation, manifest, verdict, and detached hashes.

Evidence remains local/ignored. API keys, secrets, private-key contents, account
identifiers, signed URLs, headers, raw responses, raw exceptions, signatures, and credential
hashes are forbidden. No credential value or identifier was requested, read, logged, or
written in this task.

## Findings closed during self-review

- dedicated one-way-mode proof was added and the insufficient account-config assumption was
  removed;
- failed reads can no longer become ownership-proof sources;
- second cancel requires a fresh successful query and excludes `PENDING_CANCEL`;
- cleanup budgets preserve both pre-close ownership and post-close global-state reads;
- LIMIT and contingency MARKET support plus filter cardinality are pre-create requirements;
- all quantity/notional/freshness/skew/timing Decimal arithmetic used by guards is insulated
  from process-global precision;
- summary HTTP counts and timing relationships are internally consistent;
- the document no longer claims that two caller-supplied hashes alone prove a raw snapshot
  and parsed filter contract correspond.

## Verification

- focused v1.6 offline tests: `57 passed`;
- complete regression suite: `276 passed`, with `53` pre-existing third-party or temporary
  cleanup warnings and no failure;
- changed-file Ruff lint and format: PASS;
- whitespace/diff check: PASS;
- credential-value scan: PASS;
- production/network access in this task: zero.

Three independent read-only agent reviews were bound to the final protocol/module/test
SHA-256 values and each reported `P0/P1/P2/P3 = 0/0/0/0`. Work Buddy approval is not a
mandatory condition for this candidate-design checkpoint and was not invented. A future
credential-bearing mutation runtime has a stricter mandatory independent-review gate.

## Findings

- `P0 = 0`
- `P1 = 0`
- `P2 = 0`
- `P3 = 0`

## User decision still required

- `OPTION_A` (recommended): approve the exact `ETHUSDT`, 10 USDT cap, BUY LIMIT GTX at 1%
  below fresh best bid candidate and accept the non-zero Demo accidental-fill risk plus the
  bounded exactly-owned containment path.
- `OPTION_B`: do not approve any real Demo order; retain zero mutation/fill risk and remain
  at the protocol-only checkpoint.

Approval of OPTION_A approves the protocol parameters only. It still does not authorize a
credential session or a Demo mutation run; those remain separate later gates.
