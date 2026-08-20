# Immutable Data Layer V1 binding

- study id: `study-2026-08-20-btceth-spot-perp-carry`
- binding date: 2026-08-20 UTC
- state after binding: `DATASET_BOUND_READY_FOR_RUNNER`
- capture/profile repository commit: `b357fcb5a49e8535cf5942c7ac6ef449ee4251bf`
- preregistration SHA-256 at capture:
  `5ad04bf41f6306f43d5df3f9d3af97d6fda055848b3ca0063455e829156f031d`
- source manifest SHA-256:
  `d9607f7d5747a06af4e23517942b2afd44ebd127fa1ac0e970718813e1c6e036`
- coverage: `[2020-01-01, 2026-08-20)` UTC
- venue/source scope: Binance public spot + USD-M endpoints;
  `UNVERIFIED_SINGLE_VENUE`

## Snapshot chain

- dataset: `btceth-spot-perp-carry`
- raw snapshot ID:
  `13d2df0eb1605aaf6f1337ffb92cf85c97cd4354cdf002be3166b3f8f38cdcb7`
- validated snapshot ID:
  `7901e6493671cfd6550076ae17c4d1c5bf94c94a0eb77573cff780a5c306a592`
- curated dataset/snapshot ID:
  `9601a8ff1cbfd52b75744d3380bf7b0961d289c11d3ef4641c5c4e42cd38aee8`
- snapshot-manifest SHA-256:
  `d0afd4e8e448859933cedfeb83bf8edc6fb185ed047c3e4439ec312f3e61c01d`
- schema ID:
  `31f35151bd5c9c6135a2ece74937aee0a5caa8f17c7964fea60fc6d4bae6c652`
- registry status: `PASS`
- quality verdict: `PASS`
- integrity replay verdict: `VERIFIED`
- rows/bytes: `43,615` / `6,625,903`

## Curated input-file SHA-256 pins

- `BTCUSDT.funding`:
  `2886a5b621e9207bf6cc1da9c3490abd6bb183b512f9a9b7b0f21269373cd35c`
- `BTCUSDT.mark_8h`:
  `f06bd79f001f83f4435bd8251d7f285c33fa56f657a3b1281c7c805dc701ccc4`
- `BTCUSDT.spot_8h`:
  `9c5f57f18c086c7c7b1d7d05af6727235a3909d5186def13104fae46603e2e6c`
- `ETHUSDT.funding`:
  `d414437905c5528d31e24eca77e6e751063827a2984a545b3c9b2ab2b07020ff`
- `ETHUSDT.mark_8h`:
  `9b7aec88870aebc9e8c8b9edff6d62025044834d94b834beb2028b658c2c30e1`
- `ETHUSDT.spot_8h`:
  `33ab5be99912e982f784b3bfe1c5c9cb7ba539568fd0997c8b8aec6e569c287b`
- `dataset_manifest`:
  `158025e742778807b155cdeab9953d436671175e37fddf794ae1b8e3931e30bb`

## Replay evidence and boundary

Two consecutive invocations of the existing Data Layer V1 `migrate-carry`
flow returned the exact same three snapshot IDs above. A direct registry replay
of the curated ID returned `VERIFIED / curated / PASS` and the same manifest,
schema, and file hashes.

This binding fills only identifiers allowed by the preregistration. It changes
no hypothesis, sample, signal, cost, stress, or gate. A formal runner must
require this exact curated ID, call
`verify_snapshot(..., minimum_stage="curated")`, compare every pin above, and
contain no exchange network client.
