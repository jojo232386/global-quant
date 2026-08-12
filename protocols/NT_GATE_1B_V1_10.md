# NT-GATE-1B v1.10 Redacted Read-Only Diagnostics Delta

Protocol version: `1.10`

Base: `4bf099ee1bf3947b019a5b3df27bc120845d4bec`

Status: rework candidate only; independent acceptance and Demo execution are
not included.

## Frozen trading and capability boundary

`TRADING_SEMANTICS_DELTA = NONE`

The accepted v1.9 order remains frozen: `ETHUSDT`, `BUY`, `LIMIT`, `GTX`, fresh
best bid multiplied by `0.99`, `MAX_NOTIONAL_USDT <= 25`, no amend, isolated
margin required, leverage `1x` required, auto-add margin off, Demo USD-M only,
and no Production fallback.

`READ_ONLY_CAPABILITY_DELTA = NONE`

The exact authenticated allowlist remains:

- `GET /fapi/v1/symbolConfig` with exact `symbol=ETHUSDT`;
- `GET /fapi/v1/openOrders` with exact `symbol=ETHUSDT`;
- `GET /fapi/v3/positionRisk` with exact `symbol=ETHUSDT`.

The origin remains exactly `https://demo-fapi.binance.com`. Mutation lifecycle,
authorization, intent, execution, and order modules remain unreachable from the
read-only leaf.

`MUTATION_CAPABILITY_DELTA = NONE`

## Frozen diagnostic behavior

`DIAGNOSTIC_SEMANTICS_DELTA = NONE`

The v1.10 diagnostic behavior already present in the diagnostic candidate is
unchanged by this acceptance rework. Failure output is a strict allowlist of
structured fields. It never retains or
renders a raw exception, credential, signature, response message, header, URL,
or caller-selected text. Binance code `-1021` maps only to
`TIMESTAMP_OR_CLOCK_SKEW`; `-1022` maps only to `SIGNATURE_INVALID`. An unknown
numeric Binance code is retained numerically under `BINANCE_API_ERROR` without
guessing its meaning. There is no automatic retry.

The diagnostic delta does not add an endpoint, method, origin, parameter,
credential source, account capability, mutation path, or Production fallback.

## Version-aware independent machine acceptance

The candidate Git tree contains one active declaration at:

```text
protocols/NT_GATE_1B_ACTIVE_ACCEPTANCE.json
```

The declaration names exactly this protocol version, the artifact schema, the
PASS verdict, and this tracked protocol document. It is intentionally a single
active declaration, not a registry of selectable historical versions.

After completing a PASS review, the independent Reviewer generates the
owner-only artifact with:

```text
python scripts/generate_gate_1b_acceptance_artifact.py \
  --confirm-independent-review-complete \
  --expected-protocol-version 1.10 \
  --candidate <exact-40-character-reviewed-SHA> \
  --reviewer-identity <independent-reviewer> \
  --verdict PASS_GATE1B_V1_10_READ_ONLY_DIAGNOSTICS \
  --p0 0 --p1 0 --p2 <count> --p3 <count> \
  --reviewed-at <ISO-8601-time> \
  --output <new-owner-only-artifact-path>
```

The generator refuses to overwrite an existing artifact and uses the same
trusted expected-context constructor as the verifier. The independent Reviewer
then invokes:

```text
python scripts/verify_gate_1b_acceptance.py \
  --expected-protocol-version 1.10 \
  --candidate <exact-40-character-reviewed-SHA> \
  --artifact <owner-only-artifact-path>
```

The verifier constructs `TRUSTED_EXPECTED_CONTEXT` without reading the
artifact. Its trust inputs are the Reviewer-specified protocol version and
exact candidate SHA, plus the active declaration and protocol bytes read from
that candidate's Git tree. The protocol identity is the SHA-256 of those
tracked protocol bytes.

The v1.10 artifact must carry:

- `artifact_schema_version = "1"`;
- `protocol_version = 1.10`;
- the exact `reviewed_head` and matching `protocol_commit`;
- independent `reviewer_identity`, `reviewed_at`, and verdict;
- exact P0, P1, P2, and P3 counts;
- `protocol_sha256` as the protocol content identity;
- `artifact_sha256` over every canonical artifact field except itself.

Acceptance continues only when artifact schema, protocol version, candidate
SHA, protocol identity, PASS verdict, and canonical digest all match the
trusted expected context, with `P0 == 0` and `P1 == 0`.

An artifact cannot select its own expected context. A v1.9 artifact cannot
satisfy the v1.10 declaration; a v1.10 artifact cannot satisfy the explicit
legacy v1.9 context. Missing, different, or undeclared versions fail closed.
The historical v1.9 verifier remains available only for its exact legacy v1.9
artifact and candidate context.

No final-accepted tag or audit ref is an input to the verifier. The sequence is
candidate, independent review, artifact generation, artifact verification, and
only then final certification.

## Explicit non-changes

- Real acceptance artifact: not created by this implementation.
- Final-accepted tag or audit ref: not created by this implementation.
- Existing v1.6 and v1.9 historical evidence: not rewritten.
- Read-only HTTP and credential transports: unchanged by the acceptance rework.
- Mutation front door and execution lifecycle: unchanged.
- Real credentials or authenticated account requests: not used by the rework.
- Demo or Production order: not sent.

Next gate: `FRESH_INDEPENDENT_GATE_1B_V1_10_REWORK_ACCEPTANCE`.
