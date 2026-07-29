from __future__ import annotations

import argparse
import json
from pathlib import Path

from global_quant.gate1a.arbiter import GateArbiter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--require-workbuddy",
        action="store_true",
    )
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    verdict = GateArbiter(
        require_workbuddy=args.require_workbuddy,
    ).decide(manifest)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(verdict, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

