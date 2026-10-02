#!/usr/bin/env python3
"""Generate an Ed25519 release-signing keypair outside the source tree."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release.qualification import promotion_public_key_fingerprint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("private_key", help="destination PEM for the private key")
    parser.add_argument("public_key", help="destination PEM for the public key")
    args = parser.parse_args()
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        print("cryptography is required; install the release extra", file=sys.stderr)
        return 2
    priv_path = Path(args.private_key).expanduser().resolve()
    pub_path = Path(args.public_key).expanduser().resolve()
    if priv_path.exists() or pub_path.exists():
        print("refusing to overwrite an existing release key", file=sys.stderr)
        return 2
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(private_pem)
    try:
        priv_path.chmod(0o600)
    except OSError:
        pass
    pub_path.write_bytes(public_pem)
    print(f"private_key={priv_path}")
    print(f"public_key={pub_path}")
    print(f"public_key_sha256={promotion_public_key_fingerprint(public_pem)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
