# PIT Listing / Universe zero-cost feasibility

Status: `FULL_DYNAMIC_MARKET_PATH_EXHAUSTED`; fixed cohort follow-up is partial

Review date: `2026-08-24 UTC`

Canonical baseline: `3ed5b68b4610dbea6181f33f534514a919392095`

Scope: the single permitted pivot after Candidate B. No quote-volume, L2,
unlock/floating-supply, runtime, strategy, execution, database, or new platform
work is authorized or performed.

## Decision

```text
PIT_UNIVERSE=BLOCKED
CODE=DO_NOT_BUILD
DATA_ADMISSION_READY=NO
DATA_CAPABILITY_UNLOCKED=NO
STOP=YES
```

Canonical Price V1 has a reproducible monthly selection and an exception-only
lifecycle sidecar. It does not have a complete historical Listing/Universe
Master. Importing or expanding a parser cannot manufacture the missing historical
domain, publication times, or revision evidence.

## Existing zero-cost evidence

- [PRE2024 universe rule](../data/PRE2024_UNIVERSE_RULE_DRAFT.md) uses first
  available daily bar as a listing proxy and explicitly records survivor bias;
  archive availability cannot prove a complete historical contract domain.
- [EXPL-017 lifecycle sidecar](../data/expl-017-lifecycle-v1.json) binds 208
  Price V1 symbols: 196 reach the cutoff, 11 early terminals are confirmed, and
  one is unresolved.
- `AKROUSDT` remains `TERMINATED_UNCONFIRMED`. Binance's announcement, published
  2022-05-18 10:59 UTC, scheduled settlement for 2022-05-26 09:00 UTC, while the
  preserved official aggregate-trade archive contains trades through
  2022-05-27 06:59:13.686 UTC. No authoritative revision resolving the conflict
  was found. See the [official announcement](https://www.binance.com/en/support/announcement/detail/d1cb959677034c45b5149f1d998ae2b1).
- Canonical tests intentionally preserve the AKRO fail-closed classification;
  later missing bars are not allowed to infer a termination event.

The noncanonical `codex/symbol-lifecycle-audit` worktree at `660764d` contains a
larger Price V1 lifecycle audit. It remains tied to the same 208-symbol candidate
set, explicitly forbids a PIT universe rewrite, and fails on the same AKRO
conflict. It is not a general Listing/Universe Master and is not imported.

## Missing admission evidence

The free/public route does not currently prove:

1. the complete historical USD-M perpetual contract domain, including contracts
   absent from today's exchange metadata and absent from the acquired archive;
2. true listing effective and first-public times instead of first-bar proxies;
3. a versioned as-of instrument master with contract multipliers/specification
   changes and delisting revisions;
4. complete delisted/pre-launch coverage without survivor bias; or
5. a conflict-free lifecycle state for every required scheduled member.

Today's `exchangeInfo`, current archive object existence, and future missing bars
cannot supply those historical facts. Dropping AKRO or other unresolved names
would be post-hoc survivor filtering, not a fix.

## Minimum defensible outcome

Do not build a Universe Master parser, database, adapter, or registry artifact.
The existing sidecar remains valid only for its narrow, exception-only Price V1
use and does not unlock Candidate B or Tier 2.

Reopen only if a source provides historical, versioned contract membership with
listing/termination effective time, publication/availability time, contract
specifications, correction history, and independently auditable coverage. Until
then the zero-cost pivot is exhausted and later data routes are outside this
mission's authorized scope.

## Fixed-cohort follow-up

The later [PIT Data Foundation V1](PIT_DATA_FOUNDATION_V1.md) mission found a
route that does not contradict the full-market stop above. A Wayback capture of
the official Binance USD-M `exchangeInfo` endpoint freezes all 80 contracts
trading at one historical timestamp. All 80 bind to Price V1. Five have
confirmed official terminal evidence; an unresolved `TOMOUSDT` zero-volume tail
and the bar-open timestamp limitation force the global query window to end at
`2023-11-14T00:00:00Z`. The cohort is not selected from today's survivors or
later returns, and zero-volume padding is not treated as continued activity.

That evidence unlocks only a bounded, fixed `PARTIAL_PIT_COHORT_CANDIDATE`.
It does not reconstruct later listings, prove a complete dynamic
market domain, or remove the Price/Funding/OI numeric vintage blocker. The
correct follow-up result is `PARTIAL_PIT_UNIVERSE_UNLOCKED`, not a reversal of
the full-market or Candidate B data-admission stop. It also does not claim
coverage through 2023-12-31.
