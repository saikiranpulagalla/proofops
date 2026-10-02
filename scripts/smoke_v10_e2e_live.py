#!/usr/bin/env python3
"""ProofOps V1.0 real deployed end-to-end qualification.

This gate executes the dangerous initial outreach entirely through the deployed HTTPS
application. The deployed runtime must be in qualification mode with the qualification
fault ``gmail_commit_then_lose_first_response`` enabled: Gmail commits the real message,
the server deliberately loses the first response and hides one reconciliation lookup,
ProofOps must expose UNKNOWN, then reconcile the original message without resending.

The gate also proves a deployed restart by requiring the shared PostgreSQL deployment
generation to advance, verifies release/runtime/environment/deployment attestation from
the restarted service, and checks a second pre-existing ACCEPTED renewal causes zero
outbound action.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.cookiejar
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.live_runtime import LiveRuntimeConfig, build_live_runtime
from app.domain.enums import ActionState, EmailIntent, RunState
from app.release.qualification import (
    deployment_fingerprint_from_env,
    release_identity,
    runtime_environment_fingerprint,
    runtime_payload_fingerprint,
    safe_database_identity,
    write_receipt,
)
from app.security.canonicalization import normalize_email
from app.version import ALEMBIC_HEAD, __version__


def _normalize_digest(value: str) -> str:
    raw = str(value or "").strip().casefold()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError("expected a SHA-256 digest")
    return raw


def _http_json(opener, method: str, url: str, *, body=None, headers=None, timeout=30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed, dict(exc.headers.items())


def _deployed_login(base_url: str, password: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, payload, _ = _http_json(opener, "POST", base_url + "/api/session", body={"password": password})
    if status != 200:
        raise RuntimeError(f"deployed login failed: {status} {payload}")
    return opener, str(payload["csrf_token"])


def _deployed_mutation(opener, base_url: str, csrf: str, path: str, body: dict, key: str):
    return _http_json(
        opener,
        "POST",
        base_url + path,
        body=body,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": key, "Origin": base_url},
    )


def _provider_ready(opener, base_url: str) -> dict:
    status, payload, _ = _http_json(opener, "GET", base_url + "/ready/providers", timeout=60)
    if status != 200 or payload.get("status") != "ready":
        raise RuntimeError(f"deployed provider readiness failed: {status} {payload}")
    return payload


def _attestation(opener, base_url: str) -> dict:
    status, payload, _ = _http_json(opener, "GET", base_url + "/api/qualification/attestation")
    if status != 200:
        raise RuntimeError(f"deployed qualification attestation failed: {status} {payload}")
    return payload


def _assert_attestation(
    value: dict, *, expected_revision: str | None = None, require_revision_change_from: str | None = None
) -> None:
    identity = release_identity()
    if value.get("version") != __version__:
        raise RuntimeError(f"deployed version mismatch: {value.get('version')!r} != {__version__!r}")
    if value.get("alembic_head") != ALEMBIC_HEAD:
        raise RuntimeError(f"deployed Alembic head mismatch: {value.get('alembic_head')!r} != {ALEMBIC_HEAD!r}")
    if value.get("source_fingerprint") != identity["release_source_fingerprint"]:
        raise RuntimeError("deployed release-source fingerprint does not match qualification source")
    if value.get("runtime_payload_fingerprint") != identity["runtime_payload_fingerprint"]:
        raise RuntimeError("deployed runtime payload does not match frozen release payload")
    if value.get("runtime_payload_fingerprint") != runtime_payload_fingerprint():
        raise RuntimeError("qualification checkout runtime payload differs from frozen release payload")
    if value.get("environment_fingerprint") != runtime_environment_fingerprint():
        raise RuntimeError("deployed runtime environment fingerprint does not match qualification environment")
    if value.get("deployment_fingerprint") != deployment_fingerprint_from_env():
        raise RuntimeError("deployed configuration fingerprint does not match qualification configuration")
    revision = str(value.get("deployment_revision") or "").strip()
    if not revision:
        raise RuntimeError("deployed attestation has no externally supplied deployment revision")
    if expected_revision is not None and revision != expected_revision:
        raise RuntimeError("deployment revision changed unexpectedly")
    if require_revision_change_from is not None and revision == require_revision_change_from:
        raise RuntimeError("deployment restart did not change the deployment revision")


def _poll_new_revision(opener, base_url: str, old_revision: str, *, timeout_seconds: int = 180) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = _attestation(opener, base_url)
            revision = str(value.get("deployment_revision") or "").strip()
            if revision and revision != old_revision:
                return value
            last = value
        except Exception as exc:
            last = repr(exc)
        time.sleep(3)
    raise RuntimeError(f"deployment revision did not change after restart: {last!r}")


def main() -> int:
    if os.environ.get("PROOFOPS_ALLOW_LIVE_E2E_SMOKE") != "YES":
        print("Refusing live end-to-end effects. Set PROOFOPS_ALLOW_LIVE_E2E_SMOKE=YES explicitly.", file=sys.stderr)
        return 2

    required = {
        "PROOFOPS_DEPLOY_RESTART_COMMAND": os.environ.get("PROOFOPS_DEPLOY_RESTART_COMMAND", "").strip(),
        "PROOFOPS_DEPLOY_RESTART_COMMAND_ID": os.environ.get("PROOFOPS_DEPLOY_RESTART_COMMAND_ID", "").strip(),
        "PROOFOPS_DEPLOYED_BASE_URL": os.environ.get("PROOFOPS_DEPLOYED_BASE_URL", "").strip().rstrip("/"),
        "PROOFOPS_UI_PASSWORD": os.environ.get("PROOFOPS_UI_PASSWORD", "").strip(),
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": os.environ.get("PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST", "").strip(),
        "PROOFOPS_DEPLOYMENT_REVISION": os.environ.get("PROOFOPS_DEPLOYMENT_REVISION", "").strip(),
        "PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID": os.environ.get("PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"missing final E2E deployment prerequisites: {missing}", file=sys.stderr)
        return 2
    base_url = required["PROOFOPS_DEPLOYED_BASE_URL"]
    if not base_url.startswith("https://"):
        print("PROOFOPS_DEPLOYED_BASE_URL must be HTTPS for final qualification", file=sys.stderr)
        return 2

    config = LiveRuntimeConfig.from_env()
    if not config.qualification_mode:
        raise SystemExit("final E2E requires PROOFOPS_QUALIFICATION_MODE=YES")
    if config.qualification_fault_mode != "gmail_commit_then_lose_first_response":
        raise SystemExit("final E2E requires PROOFOPS_QUALIFICATION_FAULT_MODE=gmail_commit_then_lose_first_response")

    customer_id = os.environ.get("PROOFOPS_E2E_CUSTOMER_ID", "").strip()
    accepted_customer_id = required["PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID"]
    if not customer_id or customer_id not in config.contacts or accepted_customer_id not in config.contacts:
        print("both E2E customer IDs must name configured synthetic customers", file=sys.stderr)
        return 2

    # Build a direct, read-only verification view over the same real providers/DB. All
    # consequential API operations below go through the deployed HTTPS service.
    runtime = build_live_runtime(config)
    customer = runtime.workflow.crm.get_customer(customer_id)
    accepted_customer = runtime.workflow.crm.get_customer(accepted_customer_id)
    if customer is None or accepted_customer is None:
        raise SystemExit("E2E synthetic CRM customer does not exist")
    if normalize_email(customer.contact_email or "") != config.gmail_sender or normalize_email(accepted_customer.contact_email or "") != config.gmail_sender:
        raise SystemExit("final qualification is self-send-only: both CRM contacts must equal authenticated Gmail mailbox")
    if customer.renewal_id not in config.renewal_threads or config.renewal_threads[customer.renewal_id]:
        raise SystemExit("initial-outreach E2E requires an explicit empty thread binding for the fresh synthetic renewal")
    accepted_threads = config.renewal_threads.get(accepted_customer.renewal_id)
    if not accepted_threads:
        raise SystemExit("accepted-customer E2E requires a non-empty authoritative Gmail thread binding")
    accepted_obs = runtime.workflow.gmail_read.latest_observation(accepted_customer.customer_id, renewal_id=accepted_customer.renewal_id)
    if accepted_obs.intent != EmailIntent.ACCEPTED or not accepted_obs.sender_verified:
        raise SystemExit(f"accepted-customer authoritative Gmail evidence is not verified ACCEPTED: {accepted_obs}")
    if customer.renewal_status.strip().casefold() != "pending" or customer.do_not_contact:
        raise SystemExit("initial E2E customer must be pending and contactable")
    now = datetime.now(timezone.utc)
    if customer.renewal_date is None or not (now <= customer.renewal_date.astimezone(timezone.utc) <= now + timedelta(days=45)):
        raise SystemExit("E2E renewal_date must be current and within the 45-day outreach window")
    logical_key = f"{customer.renewal_id}:initial_outreach"
    if runtime.repo.get_action(logical_key) is not None:
        raise SystemExit("E2E renewal already has a ProofOps logical action; use a fresh synthetic renewal")

    # Prove the currently deployed process and artifact before touching the customer flow.
    opener, csrf = _deployed_login(base_url, required["PROOFOPS_UI_PASSWORD"])
    _provider_ready(opener, base_url)
    before_attestation = _attestation(opener, base_url)
    _assert_attestation(before_attestation)
    revision_before = str(before_attestation["deployment_revision"])

    unique = uuid.uuid4().hex
    deadline = (now + timedelta(days=7)).isoformat()
    create_body = {
        "goal_text": f"Send verified synthetic renewal outreach for {customer.company}",
        "company": customer.company,
        "deadline": deadline,
    }
    status, cp, _ = _deployed_mutation(opener, base_url, csrf, "/api/runs", create_body, f"{unique}-create")
    if status != 201 or cp.get("run_state") != RunState.WAITING_APPROVAL.value or not cp.get("action_id"):
        raise SystemExit(f"deployed E2E create did not reach approval boundary: {status} {cp}")

    status, ap, _ = _deployed_mutation(
        opener, base_url, csrf, f"/api/runs/{cp['run_id']}/approve",
        {"action_id": cp["action_id"], "ttl_seconds": 600}, f"{unique}-approve",
    )
    if status != 200 or not ap.get("approval_id"):
        raise SystemExit(f"deployed E2E approval failed: {status} {ap}")
    execute_body = {"action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    exec_key = f"{unique}-execute"
    status, first_body, first_headers = _deployed_mutation(
        opener, base_url, csrf, f"/api/runs/{cp['run_id']}/execute", execute_body, exec_key,
    )
    if status != 200 or first_body.get("run_state") != RunState.UNKNOWN.value or first_body.get("action_state") != ActionState.UNKNOWN.value:
        raise SystemExit(f"deployed qualification fault did not surface UNKNOWN: {status} {first_body}")

    status, replay_body, replay_headers = _deployed_mutation(
        opener, base_url, csrf, f"/api/runs/{cp['run_id']}/execute", execute_body, exec_key,
    )
    replay_flag = next((v for k, v in replay_headers.items() if k.casefold() == "idempotent-replay"), None)
    if status != 200 or str(replay_flag).casefold() != "true" or replay_body != first_body:
        raise SystemExit("deployed exact execute replay was not served from durable idempotency state")

    current = first_body
    for attempt in range(1, 9):
        if current.get("run_state") == RunState.COMPLETED_VERIFIED.value:
            break
        if current.get("action_state") == ActionState.RETRYABLE.value:
            raise SystemExit("E2E reached RETRYABLE; committed Gmail message should reconcile")
        if current.get("run_state") not in {RunState.UNKNOWN.value, RunState.RECONCILING.value, RunState.EXECUTING.value}:
            raise SystemExit(f"E2E entered unexpected state before completion: {current}")
        time.sleep(5)
        status, current, _ = _deployed_mutation(
            opener, base_url, csrf, f"/api/runs/{cp['run_id']}/resume", {}, f"{unique}-resume-{attempt}",
        )
        if status != 200:
            raise SystemExit(f"deployed E2E resume failed: {status} {current}")
    if current.get("run_state") != RunState.COMPLETED_VERIFIED.value or current.get("action_state") != ActionState.CONFIRMED.value:
        raise SystemExit(f"deployed E2E did not reach verified completion: {current}")

    evidence = runtime.repo.evidence_for_run(cp["run_id"])
    sent = [e for e in evidence if e.evidence_type == "gmail_sent_message" and e.verified]
    if len(sent) != 1:
        raise SystemExit(f"E2E expected exactly one verified Gmail effect evidence row, found {len(sent)}")
    effect = runtime.repo.latest_attempt(cp["action_id"])
    if effect is None:
        raise SystemExit("E2E confirmed action has no effect attempt")
    matches = runtime.workflow.gmail_read.find_by_message_id(effect.external_key)
    if len(matches) != 1:
        raise SystemExit(f"E2E expected exactly one physical Gmail message for deterministic key, found {len(matches)}")

    restart = subprocess.run(
        shlex.split(required["PROOFOPS_DEPLOY_RESTART_COMMAND"]), text=True, capture_output=True, timeout=120
    )
    if restart.returncode != 0:
        raise SystemExit(f"deployment restart command failed: {restart.stderr or restart.stdout}")
    # Worker starts are diagnostic-only.  Qualification accepts only a new immutable
    # revision supplied by the deployment infrastructure after the rollout command.
    _poll_new_revision(opener, base_url, revision_before)

    opener, deployed_csrf = _deployed_login(base_url, required["PROOFOPS_UI_PASSWORD"])
    _provider_ready(opener, base_url)
    after_attestation = _attestation(opener, base_url)
    _assert_attestation(after_attestation, require_revision_change_from=revision_before)
    if after_attestation["source_fingerprint"] != before_attestation["source_fingerprint"]:
        raise SystemExit("source fingerprint changed across restart")
    if after_attestation["runtime_payload_fingerprint"] != before_attestation["runtime_payload_fingerprint"]:
        raise SystemExit("runtime payload fingerprint changed across restart")
    if after_attestation["environment_fingerprint"] != before_attestation["environment_fingerprint"]:
        raise SystemExit("runtime environment fingerprint changed across restart")
    if after_attestation["deployment_fingerprint"] != before_attestation["deployment_fingerprint"]:
        raise SystemExit("deployment configuration fingerprint changed across restart")

    status, view, _ = _http_json(opener, "GET", base_url + f"/api/runs/{cp['run_id']}")
    if status != 200 or view.get("run", {}).get("state") != RunState.COMPLETED_VERIFIED.value:
        raise SystemExit(f"actual deployment restart did not recover completed run: {status} {view}")

    status, repeat_cp, _ = _deployed_mutation(
        opener, base_url, deployed_csrf, "/api/runs", create_body, f"{unique}-repeat-create"
    )
    if status != 201 or repeat_cp.get("run_state") != RunState.DEFERRED.value or repeat_cp.get("action_id") is not None:
        raise SystemExit(f"repeat goal did not converge to DEFERRED/no-action after restart: {status} {repeat_cp}")
    matches_after = runtime.workflow.gmail_read.find_by_message_id(effect.external_key)
    if len(matches_after) != 1:
        raise SystemExit("repeat submission changed physical Gmail effect count")

    accepted_deadline = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    accepted_body = {
        "goal_text": f"Handle synthetic accepted renewal for {accepted_customer.company}",
        "company": accepted_customer.company,
        "deadline": accepted_deadline,
    }
    status, accepted_cp, _ = _deployed_mutation(
        opener, base_url, deployed_csrf, "/api/runs", accepted_body, f"{unique}-accepted-create"
    )
    if status != 201 or accepted_cp.get("run_state") != RunState.BLOCKED_NEEDS_HUMAN.value or accepted_cp.get("action_id") is not None:
        raise SystemExit(f"existing ACCEPTED customer did not block with zero action: {status} {accepted_cp}")
    if runtime.repo.get_action(f"{accepted_customer.renewal_id}:initial_outreach") is not None:
        raise SystemExit("accepted-customer scenario created an outbound logical action")

    write_receipt(
        gate="deployed_e2e",
        target={
            "database": safe_database_identity(config.database_url),
            "spreadsheet_id": config.spreadsheet_id,
            "sheet_name": config.sheet_name,
            "gmail_sender": config.gmail_sender,
            "gemini_model": config.gemini_model,
            "customer_id": customer.customer_id,
            "renewal_id": customer.renewal_id,
            "accepted_customer_id": accepted_customer.customer_id,
        },
        metrics={
            "completed_verified": True,
            "verified_gmail_evidence_rows": len(sent),
            "physical_messages_for_logical_effect": len(matches_after),
            "cold_restart": True,
            "actual_deployment_restart": True,
            "deployment_revision_changed": str(after_attestation["deployment_revision"]) != revision_before,
            "runtime_payload_attested": after_attestation["runtime_payload_fingerprint"] == release_identity()["runtime_payload_fingerprint"],
            "deployed_api_initial_effect": True,
            "deployed_source_attested": after_attestation["source_fingerprint"] == release_identity()["release_source_fingerprint"],
            "deployed_environment_attested": after_attestation["environment_fingerprint"] == runtime_environment_fingerprint(),
            "deployed_configuration_attested": after_attestation["deployment_fingerprint"] == deployment_fingerprint_from_env(),
            "response_loss_injected": True,
            "unknown_observed": True,
            "reconciled_without_resend": len(matches_after) == 1,
            "accepted_customer_zero_send": True,
            "repeat_goal_state": repeat_cp["run_state"],
        },
        details={
            "run_id": cp["run_id"],
            "repeat_run_id": repeat_cp["run_id"],
            "accepted_run_id": accepted_cp["run_id"],
            "boot_id_before": before_attestation.get("boot_id"),
            "boot_id_after": after_attestation.get("boot_id"),
            "deployment_revision_before": revision_before,
            "deployment_revision_after": after_attestation["deployment_revision"],
        },
    )
    print("PASS: deployed real-provider E2E response-loss/reconciliation/restart qualification")
    print(f"run_id={cp['run_id']} repeat_run_id={repeat_cp['run_id']} accepted_run_id={accepted_cp['run_id']}")
    print("physical_messages=1 UNKNOWN=observed reconciliation=no-resend deployment_revision_changed=PASS runtime_payload_attested=PASS accepted_zero_send=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
