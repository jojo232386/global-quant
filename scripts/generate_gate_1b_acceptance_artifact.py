#!/usr/bin/env python3
"""Generate a Gate 1B artifact after an independent review is complete."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from verify_gate_1b_acceptance import (  # noqa: E402
    AcceptanceVerificationError,
    _trusted_expected_context,
)

from global_quant.gate1b.review_artifact import (  # noqa: E402
    ReviewArtifactError,
    load_review_artifact,
    validate_review_for_acceptance,
    write_acceptance_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--expected-protocol-version", required=True)
    parser.add_argument("--reviewer-identity", required=True)
    parser.add_argument("--verdict", required=True)
    parser.add_argument("--p0", required=True, type=int)
    parser.add_argument("--p1", required=True, type=int)
    parser.add_argument("--p2", required=True, type=int)
    parser.add_argument("--p3", required=True, type=int)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument(
        "--confirm-independent-review-complete",
        action="store_true",
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        expected = _trusted_expected_context(
            candidate=args.candidate,
            expected_protocol_version=args.expected_protocol_version,
        )
        findings = (args.p0, args.p1, args.p2, args.p3)
        if any(type(value) is not int or value < 0 for value in findings):
            raise ReviewArtifactError("REVIEW_FINDINGS_INVALID")
        if args.p0 != 0 or args.p1 != 0:
            raise ReviewArtifactError("REVIEW_NOT_FINAL_ACCEPTANCE_ELIGIBLE")
        if args.verdict != expected.pass_verdict:
            raise ReviewArtifactError("REVIEW_VERDICT_NOT_FINAL_ACCEPTANCE_ELIGIBLE")
        if not args.reviewer_identity.strip() or not args.reviewed_at.strip():
            raise ReviewArtifactError("REVIEW_IDENTITY_OR_TIME_INVALID")
        write_acceptance_artifact(
            args.output,
            expected=expected,
            reviewer=args.reviewer_identity,
            verdict=args.verdict,
            p0=args.p0,
            p1=args.p1,
            p2=args.p2,
            p3=args.p3,
            reviewed_at=args.reviewed_at,
            notes=tuple(args.note),
        )
        validate_review_for_acceptance(
            load_review_artifact(args.output),
            expected=expected,
        )
    except (AcceptanceVerificationError, ReviewArtifactError) as exc:
        print(json.dumps({"result": "REJECT", "reason": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "protocol_version": expected.protocol_version,
                "result": "ARTIFACT_CREATED",
                "reviewed_head": expected.reviewed_head,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
