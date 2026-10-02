#!/usr/bin/env python3
"""Independently verify persisted ProofOps V1.0 promotion evidence."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release.qualification import verify_promotion_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", help="V1.0_PROMOTION_EVIDENCE.json")
    parser.add_argument("signature", help="V1.0_PROMOTION_EVIDENCE.sig")
    parser.add_argument("public_key", help="V1.0_PROMOTION_PUBLIC_KEY.pem")
    parser.add_argument("--public-key-sha256", default=os.environ.get("PROOFOPS_RELEASE_PUBLIC_KEY_SHA256"))
    args = parser.parse_args()
    if not args.public_key_sha256:
        print("--public-key-sha256 or PROOFOPS_RELEASE_PUBLIC_KEY_SHA256 is required", file=sys.stderr)
        return 2
    try:
        evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        signature = Path(args.signature).read_text(encoding="utf-8").strip()
        public_key = Path(args.public_key).read_bytes()
        verify_promotion_evidence(
            evidence,
            signature_b64=signature,
            public_key_pem=public_key,
            expected_public_key_sha256=args.public_key_sha256,
        )
    except Exception as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("PASS: V1.0 promotion evidence signature is valid")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
