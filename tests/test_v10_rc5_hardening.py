from __future__ import annotations

import json
import sys
from datetime import timedelta

import pytest

from app.api.app import _hash_request
from app.api.live_runtime import QualificationCommitThenLoseFirstResponse
from app.domain.enums import ActionState, ApprovalStatus, RunState
from app.ports.gmail import GmailOutcomeUnknown
from app.release.qualification import validate_gate_metrics
from scripts.smoke_v10_e2e_live import _assert_attestation
from tests.test_v09_api import NOW, login, make_api, mut_headers, start_run


def _approve(client, cp, csrf, *, key="rc5-approve", ttl=600):
    r = client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": ttl},
        headers=mut_headers(csrf, key),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_rc5_first_execution_crash_still_has_durable_effect_attempt(tmp_path, monkeypatch):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, cp, csrf)

    def crash_after_atomic_start(*, action_id: int, effect_id: int):
        assert runtime.repo.get_effect(effect_id) is not None
        raise RuntimeError("simulated worker death after atomic effect start")

    monkeypatch.setattr(runtime.workflow.consequential.effects, "execute_started_email", crash_after_atomic_start)
    with pytest.raises(RuntimeError, match="worker death"):
        runtime.workflow.execute(
            run_id=cp["run_id"], action_id=cp["action_id"], approval_id=ap["approval_id"], actor="judge"
        )

    action = runtime.repo.get_action_by_id(cp["action_id"])
    effects = runtime.repo.effects_for_action(action.id)
    approval = runtime.repo.get_approval(ap["approval_id"])
    assert action.state == ActionState.EXECUTING.value
    assert approval.status == ApprovalStatus.CLAIMED.value
    assert len(effects) == 1 and effects[0].state == ActionState.EXECUTING.value
    assert len(gmail.messages) == 0


def test_rc5_legacy_prepared_without_effect_resumes_once_within_continuation_window(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, cp, csrf)
    action = runtime.repo.get_action_by_id(cp["action_id"])
    runtime.approvals.claim(
        approval_id=ap["approval_id"], run_id=cp["run_id"], action_id=action.id,
        payload_hash=action.payload_hash, actor="judge",
    )
    assert runtime.repo.get_action_by_id(action.id).state == ActionState.PREPARED.value
    assert runtime.repo.effects_for_action(action.id) == []

    result = runtime.workflow.resume(run_id=cp["run_id"])
    assert result.run_state == RunState.COMPLETED_VERIFIED
    assert result.action_state == ActionState.CONFIRMED
    assert len(gmail.messages) == 1


def test_rc5_legacy_prepared_without_effect_deadline_expiry_terminalizes_without_send(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, cp, csrf)
    action = runtime.repo.get_action_by_id(cp["action_id"])
    runtime.approvals.claim(
        approval_id=ap["approval_id"], run_id=cp["run_id"], action_id=action.id,
        payload_hash=action.payload_hash, actor="judge",
    )
    runtime.repo.now_fn.value = NOW + timedelta(days=8)
    with pytest.raises(Exception, match="GOAL_DEADLINE_EXPIRED_BEFORE_EFFECT"):
        runtime.workflow.resume(run_id=cp["run_id"])
    assert runtime.repo.get_run(cp["run_id"]).state == RunState.FAILED.value
    assert runtime.repo.get_action_by_id(action.id).state == ActionState.INVALIDATED.value
    assert runtime.repo.get_approval(ap["approval_id"]).status == ApprovalStatus.EXPIRED.value
    assert runtime.repo.effects_for_action(action.id) == []
    assert len(gmail.messages) == 0


def test_rc5_stale_create_recovery_resumes_created_run_to_real_checkpoint(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": None}
    record, _ = runtime.repo.claim_api_request(
        actor="judge", route="create_run", idempotency_key="partial-created",
        request_hash=_hash_request(payload), lease_seconds=1,
    )
    init = json.dumps({"company": "ACME", "deadline": None}, sort_keys=True, separators=(",", ":"))
    run = runtime.repo.create_run(
        payload["goal_text"], owner_actor="judge", api_request_id=record.id, initial_request_json=init,
    )
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "partial-created"))
    assert r.status_code == 202, r.text
    assert r.headers["Idempotent-Recovery"] == "true"
    assert r.json()["run_id"] == run.id
    assert r.json()["run_state"] == RunState.WAITING_APPROVAL.value
    assert r.json()["action_id"] is not None
    assert len(runtime.repo.list_runs_for_actor("judge")) == 1


def test_rc5_legacy_created_run_binds_exact_retried_initialization_once(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": None}
    record, _ = runtime.repo.claim_api_request(
        actor="judge", route="create_run", idempotency_key="legacy-created",
        request_hash=_hash_request(payload), lease_seconds=1,
    )
    run = runtime.repo.create_run(payload["goal_text"], owner_actor="judge", api_request_id=record.id)
    assert run.initial_request_json is None
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "legacy-created"))
    assert r.status_code == 202, r.text
    persisted = runtime.repo.get_run(run.id)
    assert json.loads(persisted.initial_request_json) == {"company": "ACME", "deadline": None}
    assert persisted.state == RunState.WAITING_APPROVAL.value


def test_rc5_qualification_attestation_is_authenticated_and_has_fresh_boot_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFOPS_QUALIFICATION_MODE", "YES")
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    client1, _, _ = make_api(tmp_path / "a")
    client2, _, _ = make_api(tmp_path / "b")
    assert client1.get("/api/qualification/attestation").status_code == 401
    login(client1); login(client2)
    a = client1.get("/api/qualification/attestation")
    b = client2.get("/api/qualification/attestation")
    assert a.status_code == b.status_code == 200
    pa, pb = a.json(), b.json()
    assert pa["boot_id"] != pb["boot_id"]
    for key in ("source_fingerprint", "environment_fingerprint", "deployment_fingerprint"):
        assert len(pa[key]) == 64
        assert pa[key] == pb[key]


def test_rc5_qualification_attestation_hidden_outside_qualification_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("PROOFOPS_QUALIFICATION_MODE", raising=False)
    client, _, _ = make_api(tmp_path)
    login(client)
    assert client.get("/api/qualification/attestation").status_code == 404


def test_rc5_commit_then_lose_fault_injects_once_and_then_reconciles():
    class Delegate:
        def __init__(self): self.sends = 0; self.finds = 0
        def send(self, **kwargs): self.sends += 1; return "provider-ref"
        def find_by_message_id(self, message_id): self.finds += 1; return ["found"]
    delegate = Delegate()
    wrapped = QualificationCommitThenLoseFirstResponse(delegate)
    with pytest.raises(GmailOutcomeUnknown):
        wrapped.send(message_id="<x@y>", to="t@example.com", subject="s", body="b")
    assert delegate.sends == 1
    assert wrapped.find_by_message_id("<x@y>") == []
    assert wrapped.find_by_message_id("<x@y>") == ["found"]
    assert delegate.finds == 1
    assert wrapped.send(message_id="<z@y>", to="t@example.com", subject="s", body="b") == "provider-ref"
    assert delegate.sends == 2


def test_rc5_e2e_promotion_metrics_require_revision_artifact_and_deployed_api_proof():
    metrics = {
        "completed_verified": True, "verified_gmail_evidence_rows": 1,
        "physical_messages_for_logical_effect": 1, "cold_restart": True,
        # RC6 contract: a worker-generation change implied restart.  Unsafe: a second
        # worker/autoscale event can do that without deployment replacement.  RC7
        # requires an externally supplied immutable deployment revision change.
        "actual_deployment_restart": True, "deployment_revision_changed": True,
        "runtime_payload_attested": True, "deployed_api_initial_effect": True,
        "deployed_source_attested": True, "deployed_environment_attested": True,
        "deployed_configuration_attested": True, "response_loss_injected": True,
        "unknown_observed": True, "reconciled_without_resend": True,
        "accepted_customer_zero_send": True, "repeat_goal_state": "DEFERRED",
    }
    validate_gate_metrics("deployed_e2e", {"metrics": metrics})
    for key in ("deployment_revision_changed", "runtime_payload_attested", "deployed_api_initial_effect"):
        bad = dict(metrics); bad[key] = False
        with pytest.raises(ValueError):
            validate_gate_metrics("deployed_e2e", {"metrics": bad})


def test_rc5_attestation_helper_rejects_unchanged_deployment_revision(monkeypatch):
    from app.version import ALEMBIC_HEAD, __version__
    from app.release.qualification import (
        deployment_fingerprint_from_env, release_identity, runtime_environment_fingerprint, runtime_payload_fingerprint,
    )
    value = {
        "version": __version__, "alembic_head": ALEMBIC_HEAD, "boot_id": "worker-a",
        "deployment_generation": 7,  # Diagnostic worker-start counter only.
        "deployment_revision": "deploy-revision-a",
        "source_fingerprint": release_identity()["release_source_fingerprint"],
        "runtime_payload_fingerprint": runtime_payload_fingerprint(),
        "environment_fingerprint": runtime_environment_fingerprint(),
        "deployment_fingerprint": deployment_fingerprint_from_env(),
    }
    with pytest.raises(RuntimeError, match="did not change"):
        _assert_attestation(value, require_revision_change_from="deploy-revision-a")

