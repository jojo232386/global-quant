from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from global_quant.gate1a.scenarios import run_all_scenarios


def canonical_result(raw: dict) -> dict:
    return dict(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repetition", required=True, type=int)
    args = parser.parse_args()

    results = [
        result.to_dict()
        for result in run_all_scenarios(Path(args.scenario_root))
    ]
    canonical = [canonical_result(result) for result in results]
    matrix_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    payload = {
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "STOP",
        "repetition": args.repetition,
        "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        "matrix_business_hash": matrix_hash,
        "scenario_results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
