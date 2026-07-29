from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_scenario_matrix.py"
RUN_OFFLINE = ROOT / "scripts" / "run_offline.sh"


def canonical_business_payload(result: dict) -> dict:
    return {
        "matrix_business_hash": result["matrix_business_hash"],
        "scenario_results": [
            {
                "name": item["name"],
                "status": item["status"],
                "expected_orders": item["expected_orders"],
                "expected_fills": item["expected_fills"],
                "final_positions": item["final_positions"],
                "final_wallet": item["final_wallet"],
                "protection_state": item["protection_state"],
                "fail_closed": item["fail_closed"],
                "observed_events": item["observed_events"],
                "business_hash": item["business_hash"],
            }
            for item in result["scenario_results"]
        ],
    }


def test_three_repetitions_across_two_hash_seeds_are_business_identical(
    tmp_path,
) -> None:
    observed: list[dict] = []
    for repetition in range(3):
        for seed in ("1", "20260730"):
            output = tmp_path / f"result-{repetition}-{seed}.json"
            scenario_root = tmp_path / f"scenario-{repetition}-{seed}"
            environment = {
                **os.environ,
                "PYTHONHASHSEED": seed,
            }
            completed = subprocess.run(
                [
                    str(RUN_OFFLINE),
                    str(ROOT / ".venv" / "bin" / "python"),
                    str(RUNNER),
                    "--scenario-root",
                    str(scenario_root),
                    "--output",
                    str(output),
                    "--repetition",
                    str(repetition),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            observed.append(canonical_business_payload(json.loads(output.read_text())))

    expected = observed[0]
    assert all(item == expected for item in observed)
    digest = hashlib.sha256(
        json.dumps(
            expected["scenario_results"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    ).hexdigest()
    assert digest == expected["matrix_business_hash"]
