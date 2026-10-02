from __future__ import annotations

import base64
from datetime import timedelta
from email import policy
from email.message import EmailMessage
import json

import pytest

from app.adapters.google_gmail import GoogleGmailProvider
from app.api.app import _hash_request
from app.api.live_runtime import LiveConfigurationError, LiveRuntimeConfig
from app.domain.enums import ActionState, ApprovalStatus, EmailIntent, RunState
from app.release.qualification import (
    deployment_fingerprint_from_env,
    promotion_public_key_fingerprint,
    promotion_public_key_from_private,
    sign_promotion_evidence,
    validate_gate_metrics,
    verify_promotion_evidence,
)
from tests.test_v09_api import login, make_api, mut_headers, start_run


def _approve(client, csrf, cp, key="approve"):
    r = client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": 600},
        headers=mut_headers(csrf, key),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _unresolved_unknown(client, runtime, gmail, csrf):
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    body = {"action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    r1 = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e1"))
    assert r1.json()["action_state"] == ActionState.UNKNOWN.value
    r2 = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e2"))
    # RC11 A01 supersedes RC4's unsafe continuation rule: a successful empty
    # lookup after an ambiguous provider call never becomes send authority.
    assert r2.json()["action_state"] == ActionState.UNKNOWN.value
    return cp, ap, body


def test_rc11_unresolved_unknown_never_reopens_a_resend_path_after_expiry(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp, ap, body = _unresolved_unknown(client, runtime, gmail, csrf)
    runtime.repo.now_fn.value += timedelta(seconds=901)
    gmail.configure("success")
    r = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e3"))
    assert r.status_code == 200, r.text
    assert r.json()["action_state"] == ActionState.UNKNOWN.value
    assert runtime.repo.get_action_by_id(cp["action_id"]).state == ActionState.UNKNOWN.value
    assert gmail.calls == 1
    assert len(gmail.messages) == 0


def test_rc11_fresh_approval_cannot_bypass_unresolved_original_effect(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp, ap, body = _unresolved_unknown(client, runtime, gmail, csrf)
    runtime.repo.now_fn.value += timedelta(seconds=901)
    gmail.configure("success")
    expired = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e3"))
    assert expired.status_code == 200
    assert expired.json()["action_state"] == ActionState.UNKNOWN.value
    reapprove = client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": 600},
        headers=mut_headers(csrf, "approve-2"),
    )
    assert reapprove.status_code == 409
    assert gmail.calls == 1
    assert len(gmail.messages) == 0


def test_rc4_stale_approve_request_without_approval_is_reclaimed(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    payload = {"run_id": cp["run_id"], "action_id": cp["action_id"], "ttl_seconds": 600}
    runtime.repo.claim_api_request(
        actor="judge", route="approve", idempotency_key="crashed-approve",
        request_hash=_hash_request(payload), resource_id=str(cp["run_id"]), lease_seconds=1,
    )
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": 600},
        headers=mut_headers(csrf, "crashed-approve"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["approval_id"] is not None


def test_rc4_stale_execute_before_effect_boundary_is_reclaimed(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    payload = {"run_id": cp["run_id"], "action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    runtime.repo.claim_api_request(
        actor="judge", route="execute", idempotency_key="crashed-exec",
        request_hash=_hash_request(payload), resource_id=str(cp["run_id"]), lease_seconds=1,
    )
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post(
        f"/api/runs/{cp['run_id']}/execute",
        json={"action_id": cp["action_id"], "approval_id": ap["approval_id"]},
        headers=mut_headers(csrf, "crashed-exec"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_state"] == RunState.COMPLETED_VERIFIED.value
    assert gmail.calls == 1


class _ThreadTransport:
    def __init__(self):
        self.resources = {}
        self.refs = {}
        self.searches = 0
    def thread_message_refs(self, *, thread_id): return list(self.refs.get(thread_id, []))
    def get_raw(self, *, message_id): return self.resources[message_id]
    def search(self, **kwargs): self.searches += 1; return []
    def send_raw(self, **kwargs): raise AssertionError("not used")


def _resource(mid, *, thread, sender, body, internal):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    msg["To"] = "proofops-test@example.com"
    msg["Subject"] = "Renewal"
    msg["Message-ID"] = f"<{mid}@example>"
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes(policy=policy.SMTP)).decode().rstrip("=")
    return {"id": mid, "threadId": thread, "internalDate": str(internal), "raw": raw, "labelIds": ["INBOX"]}


def test_rc4_empty_thread_binding_is_operator_assertion_not_gmail_proof():
    t = _ThreadTransport()
    p = GoogleGmailProvider(t, sender_email="proofops-test@example.com", contacts={"C":"proofops-test@example.com"}, renewal_threads={"R": ()})
    obs = p.latest_observation("C", renewal_id="R")
    assert obs.intent == EmailIntent.NONE
    assert obs.provenance == "operator_assertion"
    assert obs.sender_verified is False
    assert obs.source_ref.startswith("operator-asserted-no-prior-thread:")


def test_rc4_newest_outbound_does_not_hide_older_inbound_acceptance():
    t = _ThreadTransport()
    inbound = _resource("in", thread="T", sender="alice@acme.com", body="We accept the renewal", internal=1000)
    outbound = _resource("out", thread="T", sender="proofops-test@example.com", body="Thanks, received", internal=2000)
    t.resources = {"in": inbound, "out": outbound}
    t.refs["T"] = [{"id":"in","threadId":"T"},{"id":"out","threadId":"T"}]
    p = GoogleGmailProvider(t, sender_email="proofops-test@example.com", contacts={"C":"alice@acme.com"}, renewal_threads={"R": ("T",)})
    obs = p.latest_observation("C", renewal_id="R")
    assert obs.intent == EmailIntent.ACCEPTED
    assert obs.sender_verified is True
    assert obs.message_id == "<in@example>"


def _live_env():
    return {
        "PROOFOPS_DATABASE_URL":"postgresql+psycopg://u:p@db/proofops",
        "PROOFOPS_GOOGLE_TOKEN_FILE":"/tmp/token.json",
        "PROOFOPS_SHEETS_SPREADSHEET_ID":"sheet",
        "PROOFOPS_GMAIL_SENDER":"proofops-test@example.com",
        "PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS":"YES",
        "PROOFOPS_SYNTHETIC_DATASET":"YES",
        "PROOFOPS_QUALIFICATION_MODE":"YES",
        "PROOFOPS_CONTACTS_JSON":json.dumps({"SMOKE-C001":"proofops-test@example.com"}),
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS":"proofops-test@example.com",
        "PROOFOPS_RENEWAL_THREADS_JSON":json.dumps({"SMOKE-R1":[]}),
        "GEMINI_API_KEY":"x",
        "PROOFOPS_RECONCILIATION_GRACE_SECONDS":"10",
        "PROOFOPS_MIN_ABSENCE_CHECKS":"2",
        "PROOFOPS_APPROVAL_CONTINUATION_TTL_SECONDS":"900",
    }


def test_rc4_qualification_mode_cannot_allowlist_a_second_recipient():
    env = _live_env()
    env["PROOFOPS_ALLOWED_TEST_RECIPIENTS"] = "proofops-test@example.com,real.customer@example.com"
    env["PROOFOPS_CONTACTS_JSON"] = json.dumps({"SMOKE-C001":"real.customer@example.com"})
    with pytest.raises(LiveConfigurationError, match="only allowed recipient|every synthetic CRM contact"):
        LiveRuntimeConfig.from_env(env)


def test_rc4_live_continuation_ttl_is_bounded():
    env = _live_env(); env["PROOFOPS_APPROVAL_CONTINUATION_TTL_SECONDS"] = "99999"
    with pytest.raises(LiveConfigurationError, match="continuation TTL"):
        LiveRuntimeConfig.from_env(env)


def test_rc4_promotion_rejects_empty_or_weak_gate_metrics():
    with pytest.raises(ValueError):
        validate_gate_metrics("postgresql", {"metrics": {}})
    with pytest.raises(ValueError, match="95%"):
        validate_gate_metrics("gemini", {"metrics": {
            "scenario_count":25,"trials":2,"critical_scenario_count":10,"deterministic_containment_passed":50,"deterministic_containment_total":50,
            "critical_semantic_passed":20,"critical_semantic_total":20,"semantic_passed":47,"semantic_total":50,"semantic_rate":0.94,
        }})
    with pytest.raises(ValueError, match="exactly one physical"):
        validate_gate_metrics("deployed_e2e", {"metrics": {
            "completed_verified":True,"verified_gmail_evidence_rows":1,"physical_messages_for_logical_effect":2,
            "cold_restart":True,"actual_deployment_restart":True,"response_loss_injected":True,"unknown_observed":True,
            "reconciled_without_resend":True,"accepted_customer_zero_send":True,"repeat_goal_state":"DEFERRED",
        }})


def test_rc4_deployment_fingerprint_changes_with_security_and_artifact_identity():
    base = _live_env() | {
        "PROOFOPS_ALLOWED_ORIGIN":"https://good.example",
        "PROOFOPS_SECURE_COOKIE":"1",
        "PROOFOPS_SESSION_TTL_SECONDS":"3600",
        "PROOFOPS_MAX_REQUEST_BYTES":"65536",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST":"a"*64,
        "PROOFOPS_DEPLOY_RESTART_COMMAND_ID":"k8s-proofops",
        "PROOFOPS_DEPLOYED_BASE_URL":"https://good.example",
    }
    changed = dict(base); changed["PROOFOPS_SESSION_TTL_SECONDS"] = "7200"
    assert deployment_fingerprint_from_env(base) != deployment_fingerprint_from_env(changed)
    changed = dict(base); changed["PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST"] = "b"*64
    assert deployment_fingerprint_from_env(base) != deployment_fingerprint_from_env(changed)


def test_rc4_final_promotion_signature_is_independently_verifiable_and_tamper_evident():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    evidence = {"promotion_status":"READY_FOR_V1.0_TAG", "source_fingerprint":"a"*64}
    sig, public_pem, pin = sign_promotion_evidence(evidence, private_pem)
    assert pin == promotion_public_key_fingerprint(public_pem)
    pub2, pin2 = promotion_public_key_from_private(private_pem)
    assert pub2 == public_pem and pin2 == pin
    verify_promotion_evidence(evidence, signature_b64=sig, public_key_pem=public_pem, expected_public_key_sha256=pin)
    with pytest.raises(ValueError, match="invalid V1.0 promotion evidence signature"):
        verify_promotion_evidence(evidence | {"source_fingerprint":"b"*64}, signature_b64=sig, public_key_pem=public_pem, expected_public_key_sha256=pin)
