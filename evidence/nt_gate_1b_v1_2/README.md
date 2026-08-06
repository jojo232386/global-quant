INCONCLUSIVE | MISSING_DEMO_CREDENTIALS | no account query, connection, or order

# NT-GATE-1B v1.2 Curated Evidence

This directory is the sanitized, committed subset of local runtime evidence.
The full local runtime directory remains ignored by Git.

## Identity

- Protocol commit: `35e849d`
- Protocol tag: `nt-gate-1b-v1.2-protocol`
- Tested commit: `c163b1588073559403e3009f3063066d66773620`
- Verdict: `INCONCLUSIVE`
- Reason: `MISSING_DEMO_CREDENTIALS`

## Contents

- `gate_1b_verdict.json`: machine-readable decision and scope boundaries.
- `gate_1b_verdict.json.sha256`: detached verdict checksum.
- `commands.jsonl`: logged canonical build, boundary, test, and verdict runs.
- `build_only.json`: offline node-build evidence.
- `missing_credentials_preflight.json`: fail-before-network evidence.
- `public_probe.json`: credential-free public Demo endpoint observation.
- `logs/final-full-tests.log`: final `192 passed` test output.
- `logs/machine-verdict.log`: canonical arbiter output.
- `MANIFEST.sha256`: checksums for this curated set, excluding the manifest.

No Demo account was authenticated or queried. No order, fill, fee, funding
event, position, or balance change occurred. These files do not prove Binance
execution behavior, alpha, profitability, or live readiness.
