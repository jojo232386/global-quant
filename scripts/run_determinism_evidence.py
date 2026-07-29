from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_RUNNER = ROOT / "scripts" / "run_scenario_matrix.py"


def canonical_payload(result: dict) -> list[dict]:
    return [
        dict(item)
        for item in result["scenario_results"]
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    observed: list[list[dict]] = []
    run_files: list[str] = []
    for repetition in range(3):
        for seed in ("1", "20260730"):
            output = output_root / f"seed-{seed}-rep-{repetition + 1}.json"
            scenario_root = output_root / f"work-seed-{seed}-rep-{repetition + 1}"
            environment = {**os.environ, "PYTHONHASHSEED": seed}
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCENARIO_RUNNER),
                    "--scenario-root",
                    str(scenario_root),
                    "--output",
                    str(output),
                    "--repetition",
                    str(repetition + 1),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                sys.stderr.write(completed.stdout + completed.stderr)
                return completed.returncode
            result = json.loads(output.read_text(encoding="utf-8"))
            observed.append(canonical_payload(result))
            run_files.append(str(output.resolve()))

    expected = observed[0]
    identical = all(payload == expected for payload in observed)
    digest = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    summary = {
        "status": "PASS" if identical else "STOP",
        "hash_seeds": ["1", "20260730"],
        "repetitions": 3,
        "independent_processes": 6,
        "ledger_replay_hash": digest,
        "run_files": run_files,
    }
    (output_root / "determinism_summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
