#!/usr/bin/env python3
"""ProofOps V1.0 release preflight.

This command performs non-network qualification only. It never sends Gmail, mutates
Sheets, calls Gemini, or writes to PostgreSQL. Those gates have separate opt-in smoke
scripts. Use --require-live to make missing live prerequisites a non-zero result.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.live_runtime import LiveConfigurationError, LiveRuntimeConfig
from app.version import ALEMBIC_HEAD, RELEASE_LABEL, __version__
from app.release.qualification import (
    deployment_artifact_digest_from_env,
    required_live_gate_diagnostics,
    validate_required_live_qualification,
)


def item(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = [
        item("release_identity", "PASS", f"{RELEASE_LABEL} / package {__version__}"),
        item("alembic_head", "PASS", ALEMBIC_HEAD),
    ]
    required_files = [
        "Dockerfile", "deploy.env.example", "scripts/smoke_v10_sheets_live.py",
        "scripts/smoke_v10_gmail_live.py", "scripts/smoke_v10_gemini_live.py",
        "scripts/smoke_v10_postgres_live.py", "scripts/smoke_v10_e2e_live.py",
        "scripts/promote_v10.py", "scripts/verify_v10_promotion.py", "scripts/generate_v10_release_key.py",
    ]
    missing = [p for p in required_files if not (ROOT / p).exists()]
    results.append(item("release_files", "PASS" if not missing else "FAIL", "all present" if not missing else f"missing: {missing}"))

    optional = {
        "psycopg": "external PostgreSQL",
        "googleapiclient": "Google Sheets/Gmail",
        "google.auth": "Google OAuth",
        "google.genai": "Gemini",
        "cryptography": "persistent promotion signature verification",
    }
    for module, purpose in optional.items():
        results.append(item(f"dependency:{module}", "PASS" if importlib.util.find_spec(module) else "PENDING", purpose))

    live_env = (os.environ.get("PROOFOPS_RUNTIME") or "").strip().casefold() == "live"
    if live_env:
        try:
            cfg = LiveRuntimeConfig.from_env()
        except LiveConfigurationError as exc:
            results.append(item("live_configuration", "FAIL", str(exc)))
        else:
            token_exists = Path(cfg.token_file).is_file()
            results.append(item("live_configuration", "PASS", "required live environment fields are valid"))
            results.append(item("oauth_token_file", "PASS" if token_exists else "FAIL", cfg.token_file))
            if cfg.qualification_mode:
                deployment_required = {
                    "PROOFOPS_DEPLOY_RESTART_COMMAND": os.environ.get("PROOFOPS_DEPLOY_RESTART_COMMAND", "").strip(),
                    "PROOFOPS_DEPLOY_RESTART_COMMAND_ID": os.environ.get("PROOFOPS_DEPLOY_RESTART_COMMAND_ID", "").strip(),
                    "PROOFOPS_DEPLOYED_BASE_URL": os.environ.get("PROOFOPS_DEPLOYED_BASE_URL", "").strip(),
                    "PROOFOPS_UI_PASSWORD": os.environ.get("PROOFOPS_UI_PASSWORD", "").strip(),
                }
                missing_deploy = sorted(k for k, v in deployment_required.items() if not v)
                if deployment_required["PROOFOPS_DEPLOYED_BASE_URL"] and not deployment_required["PROOFOPS_DEPLOYED_BASE_URL"].startswith("https://"):
                    missing_deploy.append("PROOFOPS_DEPLOYED_BASE_URL:https")
                try:
                    digest = deployment_artifact_digest_from_env()
                except ValueError as exc:
                    results.append(item("deployment_artifact_digest", "FAIL", str(exc)))
                else:
                    results.append(item("deployment_artifact_digest", "PASS" if digest else "FAIL", digest or "missing"))
                results.append(item(
                    "deployment_qualification",
                    "PASS" if not missing_deploy and cfg.qualification_fault_mode == "gmail_commit_then_lose_first_response" else "FAIL",
                    "restart/deployed HTTPS/runtime-attestation prerequisites present" if not missing_deploy and cfg.qualification_fault_mode == "gmail_commit_then_lose_first_response"
                    else f"missing/invalid: {missing_deploy}; fault={cfg.qualification_fault_mode!r}",
                ))
    else:
        results.append(item("live_configuration", "PENDING", "set PROOFOPS_RUNTIME=live and release environment variables"))

    # Readiness is intentionally not evidence.  A live-required invocation must
    # validate the one configured signed five-gate receipt bundle.
    if args.require_live:
        try:
            receipts = validate_required_live_qualification()
        except ValueError as exc:
            results.append(item("live_qualification_evidence", "FAIL", str(exc)))
            for gate in required_live_gate_diagnostics():
                results.append({"name": f"live_gate:{gate['gate']}", "status": "PASS" if gate["valid"] else "FAIL", "detail": gate["reason"], **gate})
        else:
            results.append(item("live_qualification_evidence", "PASS", f"validated gates: {','.join(receipts)}"))
            for gate in required_live_gate_diagnostics():
                results.append({"name": f"live_gate:{gate['gate']}", "status": "PASS", "detail": "validated", **gate})

    failed = [r for r in results if r["status"] == "FAIL"]
    pending = [r for r in results if r["status"] == "PENDING"]
    if args.json:
        print(json.dumps({"results": results, "failed": len(failed), "pending": len(pending)}, indent=2))
    else:
        for r in results:
            print(f"{r['status']:<7} {r['name']}: {r['detail']}")
        print(f"summary: failed={len(failed)} pending={len(pending)}")
    if failed or (args.require_live and pending):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
