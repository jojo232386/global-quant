# Data Layer V1 binding amendment

> Added after preregistration commit `4270f5a` and before runner
> implementation or any formal result. This amendment only strengthens input
> provenance. It changes no signal, parameter, cost, sample, or evaluation
> rule.

## Mandatory curated input

- data layer: existing GMAQ Data Layer V1 (`gmaq_data`); no alternate loader or
  direct Binance research input is permitted.
- dataset: `btceth-weekly-tsmom`
- curated snapshot / dataset ID:
  `88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615`
- snapshot manifest SHA-256:
  `002a47ae05e7bebb3859e4c5cbcaa450fcc505e86316f50165070df8283cf6b5`
- dataset manifest file SHA-256:
  `71a8ec43a0d527b099cd00005ca2a4862626ed16e6f1dcca5a7a68abe40c14aa`
- schema ID:
  `92a61adfe86ce40a0664103d738c10989ac60dfaac63beb3cca64afb9434d462`
- validated parent snapshot ID:
  `b96f99695323653ae201fe2e860c42721a44169f5419950065452ad91427efb8`
- verified state at binding: stage `curated`, quality `PASS`, integrity
  `VERIFIED`, registry integrity `VERIFIED`, quarantine count `0`.
- source window: `[2020-01-01, 2026-08-20)` UTC; source fetched at
  `2026-08-20T06:56:27Z`.
- source independence remains `UNVERIFIED_SINGLE_VENUE`; this is Binance
  source-specific integrity, not cross-venue price confirmation.

## Consumer contract

Before reading any market row, the runner must:

1. accept only a full `--dataset-id`; it may not accept a raw data directory;
2. resolve the configured V1 data root and call
   `verify_snapshot(..., expected_dataset="btceth-weekly-tsmom",
   minimum_stage="curated")`;
3. require exact stage `curated`, quality `PASS`, integrity `VERIFIED`, the
   dataset ID and manifest SHA above, the declared window, and all seven
   registered file role/SHA pairs;
4. read files only from the verified `artifact_path` and registered relative
   paths returned by Data Layer V1;
5. fail closed before calculation on any registry, manifest, file, role, SHA,
   parent, schema, window, stage, or quality mismatch.

The formal `results.json`, run manifest, verdict, and data checklist must bind:

- curated dataset ID;
- snapshot manifest SHA-256;
- schema ID and validated parent snapshot ID;
- dataset manifest file SHA-256 and every consumed file role/SHA;
- `integrity_verdict=VERIFIED`, `quality_verdict=PASS`, and the source window.

## Prohibited paths

- `gmaq-fetch-tsmom` or any direct Binance request may create source material
  for Data Layer V1, but its output cannot be consumed directly by this formal
  research runner.
- No temporary download directory, legacy manifest, unregistered file,
  raw/validated snapshot, quarantine artifact, or caller-claimed SHA is a
  formal input.
- No Data Layer V2, second registry, DuckDB, Parquet, second source, or new
  governance framework is introduced.

If this curated snapshot lacks a required field, the study stops before a
result. The only allowed remedy is to use the existing Data Layer V1
raw → validated → curated path to create and bind a new curated snapshot under
a disclosed amendment; the runner itself never fetches or migrates data.
