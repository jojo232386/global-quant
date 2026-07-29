from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from global_quant.gate1a.arbiter import GateArbiter


def verify_manifest_checksum(
    manifest_path: Path,
    checksum_path: Path,
    *,
    content: bytes | None = None,
) -> str:
    try:
        checksum_fields = checksum_path.read_text(
            encoding="ascii",
        ).split()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest checksum sidecar is unreadable: {exc}") from exc
    if not checksum_fields or len(checksum_fields[0]) != 64:
        raise ValueError("manifest checksum sidecar is invalid")
    expected = checksum_fields[0]
    observed = hashlib.sha256(
        manifest_path.read_bytes() if content is None else content,
    ).hexdigest()
    if observed != expected:
        raise ValueError("manifest checksum mismatch")
    return observed


def write_verdict(path: Path, verdict: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(verdict, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-checksum")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--require-workbuddy",
        action="store_true",
    )
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    checksum_path = (
        Path(args.manifest_checksum).resolve()
        if args.manifest_checksum
        else Path(f"{manifest_path}.sha256")
    )
    output = Path(args.output)
    try:
        content = manifest_path.read_bytes()
        manifest_sha256 = verify_manifest_checksum(
            manifest_path,
            checksum_path,
            content=content,
        )
        manifest = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        write_verdict(
            output,
            {
                "verdict": "STOP",
                "manifest": str(manifest_path),
                "manifest_checksum": str(checksum_path),
                "failures": [str(exc)],
            },
        )
        return 1
    verdict = GateArbiter(
        require_workbuddy=args.require_workbuddy,
    ).decide(manifest)
    verdict["manifest_sha256"] = manifest_sha256
    verdict["manifest_checksum"] = str(checksum_path)
    write_verdict(output, verdict)
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
