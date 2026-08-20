# Run Manifest Template

> Create one manifest per formal run BEFORE executing it, then complete it
> after the run. A run without a manifest is not evidence.
> This template does not authorize live trading.

## Identity

- run id: `<run-id>`
- study id: `<study-id>`
- preregistration reference and sha256: `<path + sha256>`
- data checklist reference and sha256: `<path + sha256>`
- started (UTC): `<YYYY-MM-DD HH:MM UTC>`

## Code and environment

- repository sha256 (HEAD at start): `<sha256>`
- branch / worktree: `<branch>`
- python version: `<version>`
- runtime version (Freqtrade / image digest): `<version / digest>`
- dependency lock or environment hash: `<hash>`
- random seeds: `<seeds>`

## Data

- data version pin and snapshot id: `<version / snapshot>`
- checksum of inputs: `<sha256>`
- fetched at (UTC): `<timestamp>`
- coverage window used: `<start - end>`

## Configuration

- strategy class: `<class>`
- timeframe and pairs: `<timeframe> <pairs>`
- stake, leverage, margin mode: `<stake> <leverage> <margin>`
- cost model applied (exact version and verified values):
  `<costs/COST_MODEL_BASELINE.md @ <sha256>, values <...>>`
- parameters (all of them): `<parameters>`

## Execution

- command line: `<command>`
- started / finished (UTC): `<start> - <end>`
- host and runtime duration: `<host> <duration>`

## Outputs

- artifact paths and sha256:
  | artifact | sha256 |
  |---|---|
- result metrics summary (per `gate/EVALUATION_GATE.md`): `<summary>`

## Conclusion

- verdict: PASS / REJECT / INCONCLUSIVE
- exact gate rule that fired: `<rule>`
- verified results vs. unverified assumptions vs. limitations:
  `<separation>`

## Follow-ups

- next steps, and whether the study continues, is frozen, or is closed:
  `<next>`
