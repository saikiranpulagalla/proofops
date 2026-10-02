#!/usr/bin/env python3
"""One-shot ProofOps V1.0 live promotion qualification.

This command does not change application source or tag a release. It runs all five live
qualification gates against one RC build/deployment, binds their machine-readable PASS
receipts to a single qualification run id, and emits a promotion evidence manifest.

It is deliberately fail-closed: if any gate fails, times out, omits a receipt, or emits a
receipt for a different RC/run, no promotion evidence manifest is written.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release.qualification import (
    build_promotion_evidence, promotion_public_key_from_private, sign_promotion_evidence,
    validate_receipt_bundle, verify_promotion_evidence,
)


def _value(cli: str | None, env_name: str, *, required: bool = True) -> str:
    value = (cli or os.environ.get(env_name) or "").strip()
    if required and not value:
        raise ValueError(f"{env_name} is required for V1.0 promotion qualification")
    return value


def _run(cmd: list[str], *, env: dict[str, str], label: str, timeout: int) -> None:
    print(f"\n=== {label} ===")
    proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n")
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {proc.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-live", action="store_true", help="actually execute all live provider gates")
    parser.add_argument("--google-token", help="OAuth token JSON; defaults to PROOFOPS_GOOGLE_TOKEN_FILE")
    parser.add_argument("--spreadsheet-id", help="synthetic CRM spreadsheet; defaults to PROOFOPS_SHEETS_SPREADSHEET_ID")
    parser.add_argument("--customer-id", help="SMOKE-/DEMO- customer used for Sheets + E2E")
    parser.add_argument("--renewal-id", help="SMOKE-/DEMO- renewal used for Sheets qualification")
    parser.add_argument("--accepted-customer-id", help="second synthetic customer with authoritative ACCEPTED Gmail evidence")
    parser.add_argument("--sheet-name", help="defaults to PROOFOPS_SHEETS_SHEET_NAME or Customers")
    parser.add_argument("--output-dir", default="qualification/v1.0", help="directory for receipts + promotion evidence")
    parser.add_argument("--timeout-seconds", type=int, default=600, help="per-gate timeout")
    parser.add_argument("--deployment-artifact-digest", help="sha256 digest of the exact deployed image/artifact")
    parser.add_argument("--release-signing-key", help="external Ed25519 private key PEM path")
    parser.add_argument("--release-public-key-sha256", help="pinned SHA-256 fingerprint of the Ed25519 public key")
    args = parser.parse_args()

    if not args.execute_live:
        print("Refusing live promotion. Re-run with --execute-live after configuring the dedicated synthetic deployment.", file=sys.stderr)
        return 2
    if args.timeout_seconds < 30:
        print("--timeout-seconds must be at least 30", file=sys.stderr)
        return 2

    try:
        google_token = _value(args.google_token, "PROOFOPS_GOOGLE_TOKEN_FILE")
        spreadsheet_id = _value(args.spreadsheet_id, "PROOFOPS_SHEETS_SPREADSHEET_ID")
        customer_id = _value(args.customer_id, "PROOFOPS_E2E_CUSTOMER_ID")
        renewal_id = _value(args.renewal_id, "PROOFOPS_QUALIFICATION_RENEWAL_ID")
        accepted_customer_id = _value(args.accepted_customer_id, "PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID")
        sheet_name = _value(args.sheet_name, "PROOFOPS_SHEETS_SHEET_NAME", required=False) or "Customers"
        artifact_digest = _value(args.deployment_artifact_digest, "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST")
        release_key_path = _value(args.release_signing_key, "PROOFOPS_RELEASE_SIGNING_KEY_FILE")
        expected_public_key_sha256 = _value(args.release_public_key_sha256, "PROOFOPS_RELEASE_PUBLIC_KEY_SHA256")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if (
        not customer_id.startswith(("SMOKE-", "DEMO-"))
        or not accepted_customer_id.startswith(("SMOKE-", "DEMO-"))
        or not renewal_id.startswith(("SMOKE-", "DEMO-"))
    ):
        print("promotion customer/accepted-customer/renewal IDs must use SMOKE- or DEMO- prefixes", file=sys.stderr)
        return 2

    try:
        _value(None, "PROOFOPS_DEPLOY_RESTART_COMMAND")
        _value(None, "PROOFOPS_DEPLOY_RESTART_COMMAND_ID")
        deployed_base_url = _value(None, "PROOFOPS_DEPLOYED_BASE_URL")
        _value(None, "PROOFOPS_UI_PASSWORD")
        if not deployed_base_url.startswith("https://"):
            raise ValueError("PROOFOPS_DEPLOYED_BASE_URL must be HTTPS")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = (ROOT / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    receipt_dir = out / "receipts"
    # Never mix receipts from an older qualification attempt into a new promotion.
    if receipt_dir.exists():
        shutil.rmtree(receipt_dir)
    receipt_dir.mkdir(parents=True)
    evidence_path = out / "V1.0_PROMOTION_EVIDENCE.json"
    signature_path = out / "V1.0_PROMOTION_EVIDENCE.sig"
    public_key_path = out / "V1.0_PROMOTION_PUBLIC_KEY.pem"
    for path in (evidence_path, signature_path, public_key_path):
        if path.exists():
            path.unlink()

    run_id = f"v1-{uuid.uuid4()}"
    signing_key = secrets.token_urlsafe(48)
    env = os.environ.copy()
    env.update({
        "PROOFOPS_RUNTIME": "live",
        "PROOFOPS_QUALIFICATION_MODE": "YES",
        "PROOFOPS_QUALIFICATION_RUN_ID": run_id,
        "PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(receipt_dir),
        "PROOFOPS_QUALIFICATION_SIGNING_KEY": signing_key,
        "PROOFOPS_GOOGLE_TOKEN_FILE": google_token,
        "PROOFOPS_SHEETS_SPREADSHEET_ID": spreadsheet_id,
        "PROOFOPS_SHEETS_SHEET_NAME": sheet_name,
        "PROOFOPS_E2E_CUSTOMER_ID": customer_id,
        "PROOFOPS_QUALIFICATION_RENEWAL_ID": renewal_id,
        "PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID": accepted_customer_id,
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": artifact_digest,
        "PROOFOPS_QUALIFICATION_FAULT_MODE": "gmail_commit_then_lose_first_response",
        "PROOFOPS_ALLOW_LIVE_POSTGRES_SMOKE": "YES",
        "PROOFOPS_ALLOW_LIVE_SHEETS_SMOKE": "YES",
        "PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE": "YES",
        "PROOFOPS_ALLOW_LIVE_GEMINI_SMOKE": "YES",
        "PROOFOPS_ALLOW_LIVE_E2E_SMOKE": "YES",
    })

    py = sys.executable
    commands = [
        ("release preflight", [py, "scripts/qualify_v10.py", "--require-live"]),
        ("PostgreSQL schema + concurrency", [py, "scripts/smoke_v10_postgres_live.py"]),
        ("Google Sheets synthetic", [py, "scripts/smoke_v10_sheets_live.py", google_token, spreadsheet_id, customer_id, renewal_id, sheet_name]),
        ("Gmail transport + business evidence", [py, "scripts/smoke_v10_gmail_live.py", google_token]),
        ("Gemini semantics + containment", [py, "scripts/smoke_v10_gemini_live.py"]),
        ("real-provider end-to-end", [py, "scripts/smoke_v10_e2e_live.py"]),
    ]

    try:
        for label, cmd in commands:
            _run(cmd, env=env, label=label, timeout=args.timeout_seconds)
        receipts = validate_receipt_bundle(receipt_dir, expected_run_id=run_id, signing_key=signing_key, env=env)
        promotion = build_promotion_evidence(receipts)
        private_pem = Path(release_key_path).expanduser().read_bytes()
        public_pem, public_pin = promotion_public_key_from_private(private_pem)
        normalized_expected = expected_public_key_sha256.casefold().removeprefix("sha256:")
        if public_pin != normalized_expected:
            raise ValueError("release signing public-key fingerprint does not match PROOFOPS_RELEASE_PUBLIC_KEY_SHA256")
        promotion["release_public_key_sha256"] = public_pin
        signature_b64, public_pem2, observed_pin = sign_promotion_evidence(promotion, private_pem)
        if public_pem2 != public_pem or observed_pin != public_pin:
            raise ValueError("release signing key identity changed during promotion")
        verify_promotion_evidence(
            promotion, signature_b64=signature_b64, public_key_pem=public_pem,
            expected_public_key_sha256=public_pin,
        )
        tmp = evidence_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(promotion, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, evidence_path)
        sig_tmp = signature_path.with_suffix(".sig.tmp")
        sig_tmp.write_text(signature_b64 + "\n", encoding="utf-8")
        os.replace(sig_tmp, signature_path)
        pub_tmp = public_key_path.with_suffix(".pem.tmp")
        pub_tmp.write_bytes(public_pem)
        os.replace(pub_tmp, public_key_path)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        # A partial receipt set is useful for debugging, but must never look promoted.
        for path in (evidence_path, signature_path, public_key_path):
            if path.exists():
                path.unlink()
        print(f"PROMOTION BLOCKED: {exc}", file=sys.stderr)
        return 1

    print("\nPASS: all five V1.0 live gates are bound to one qualification run")
    print(f"qualification_run_id={run_id}")
    print(f"promotion_evidence={evidence_path}")
    print(f"promotion_signature={signature_path}")
    print(f"promotion_public_key={public_key_path}")
    print("READY_FOR_V1.0_TAG — tag the exact already-qualified RC source; do not rebuild it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
