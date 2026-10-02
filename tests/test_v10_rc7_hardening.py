from __future__ import annotations

import pytest
from datetime import timedelta

from app.engine.goal_intent import GoalSubjectState, compile_goal_intent
from app.api.app import _hash_request
from app.domain.enums import ActionState
from tests.test_v09_api import login, make_api, mut_headers, start_run
from scripts import smoke_v10_e2e_live as e2e


@pytest.mark.parametrize("goal", [
    "Handle beta's renewal",
    "Handle the renewal for BETA",
    "Follow up with BETA about renewal",
    "Check BETA’s renewal.",
    "Contact beta about the renewal",
])
def test_rc7_goal_subject_variants_conflict_with_structured_company(goal):
    intent = compile_goal_intent(goal, "ACME")
    assert intent.subject_state == GoalSubjectState.CONFLICTS_WITH_STRUCTURED_SUBJECT
    assert "SUBJECT_CONFLICT" in intent.constraints


def test_rc7_goal_subject_ambiguity_fails_closed():
    intent = compile_goal_intent("Handle ACME and BETA renewals", "ACME")
    assert intent.subject_state == GoalSubjectState.AMBIGUOUS_SUBJECT
    assert "AMBIGUOUS_GOAL_SUBJECT" in intent.constraints


def test_rc7_generic_goal_remains_no_subject_assertion():
    intent = compile_goal_intent("Handle this renewal", "ACME")
    assert intent.subject_state == GoalSubjectState.NO_SUBJECT_ASSERTION


def test_rc7_without_contacting_unnecessarily_is_not_a_prohibition():
    intent = compile_goal_intent("Make sure ACME's renewal is handled without contacting them unnecessarily.", "ACME")
    assert intent.subject_state == GoalSubjectState.MATCHES_STRUCTURED_SUBJECT
    assert not intent.forbid_external_contact


def test_rc7_explicit_no_contact_is_preserved():
    intent = compile_goal_intent("Do not contact ACME.", "ACME")
    assert intent.forbid_external_contact


def _disable_frozen_identity_for_construction(monkeypatch):
    # RC7 source is intentionally unfrozen while this kill suite is being authored.
    # Production retains strict identity verification; this only isolates API semantics.
    import app.api.app as api_app
    monkeypatch.setattr(api_app, "release_identity", lambda: {
        "release_source_fingerprint": "0" * 64,
        "runtime_payload_fingerprint": "1" * 64,
    })


def _approve_api(client, csrf, cp, *, key: str, ttl: int):
    return client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": ttl},
        headers=mut_headers(csrf, key),
    )


def test_rc7_execute_recovery_requires_exact_effect_request_provenance(tmp_path, monkeypatch):
    _disable_frozen_identity_for_construction(monkeypatch)
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf, key="rc7-execute-create").json()
    approval = _approve_api(client, csrf, cp, key="rc7-execute-approve", ttl=600).json()
    payload = {"run_id": cp["run_id"], "action_id": cp["action_id"], "approval_id": approval["approval_id"]}
    stale_b, created = runtime.repo.claim_api_request(
        actor="judge", route="execute", idempotency_key="rc7-execute-b",
        request_hash=_hash_request(payload), resource_id=str(cp["run_id"]), lease_seconds=1,
    )
    assert created
    # A performs the actual authorization/effect transaction.  B existed first but
    # must never appropriate A's effect during stale recovery.
    result = runtime.workflow.execute(
        run_id=cp["run_id"], action_id=cp["action_id"], approval_id=approval["approval_id"],
        actor="judge", api_request_id=None,
    )
    assert result.action_state == ActionState.CONFIRMED
    assert runtime.repo.effect_for_api_request(stale_b.id, action_id=cp["action_id"]) is None
    runtime.repo.now_fn.value += timedelta(seconds=2)
    replay = client.post(f"/api/runs/{cp['run_id']}/execute", json=payload | {"action_id": cp["action_id"], "approval_id": approval["approval_id"]}, headers=mut_headers(csrf, "rc7-execute-b"))
    assert replay.status_code == 409
    assert replay.headers.get("Idempotent-Recovery") is None


def test_rc7_reject_recovery_requires_exact_revocation_request_provenance(tmp_path, monkeypatch):
    _disable_frozen_identity_for_construction(monkeypatch)
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf, key="rc7-reject-create").json()
    approval = _approve_api(client, csrf, cp, key="rc7-reject-approve", ttl=600).json()
    payload = {"run_id": cp["run_id"], "action_id": cp["action_id"], "approval_id": approval["approval_id"]}
    stale_b, created = runtime.repo.claim_api_request(
        actor="judge", route="reject", idempotency_key="rc7-reject-b",
        request_hash=_hash_request(payload), resource_id=str(cp["run_id"]), lease_seconds=1,
    )
    assert created
    rejected = client.post(
        f"/api/runs/{cp['run_id']}/reject", json={"action_id": cp["action_id"], "approval_id": approval["approval_id"]},
        headers=mut_headers(csrf, "rc7-reject-a"),
    )
    assert rejected.status_code == 200
    assert runtime.repo.get_approval(approval["approval_id"]).rejected_api_request_id != stale_b.id
    runtime.repo.now_fn.value += timedelta(seconds=2)
    replay = client.post(
        f"/api/runs/{cp['run_id']}/reject", json={"action_id": cp["action_id"], "approval_id": approval["approval_id"]},
        headers=mut_headers(csrf, "rc7-reject-b"),
    )
    assert replay.status_code == 409
    assert replay.headers.get("Idempotent-Recovery") is None


def _attestation(revision: str, *, runtime: str = "runtime-a", source: str = "source-a", config: str = "config-a"):
    return {
        "version": "test", "alembic_head": "test", "boot_id": "different-worker",
        "deployment_generation": 999, "deployment_revision": revision,
        "source_fingerprint": source, "runtime_payload_fingerprint": runtime,
        "environment_fingerprint": "environment-a", "deployment_fingerprint": config,
    }


def _patch_e2e_identity(monkeypatch):
    monkeypatch.setattr(e2e, "__version__", "test")
    monkeypatch.setattr(e2e, "ALEMBIC_HEAD", "test")
    monkeypatch.setattr(e2e, "release_identity", lambda: {"release_source_fingerprint": "source-a", "runtime_payload_fingerprint": "runtime-a"})
    monkeypatch.setattr(e2e, "runtime_payload_fingerprint", lambda: "runtime-a")
    monkeypatch.setattr(e2e, "runtime_environment_fingerprint", lambda: "environment-a")
    monkeypatch.setattr(e2e, "deployment_fingerprint_from_env", lambda: "config-a")


def test_rc7_worker_boot_or_generation_change_is_not_restart_proof(monkeypatch):
    _patch_e2e_identity(monkeypatch)
    with pytest.raises(RuntimeError, match="did not change"):
        e2e._assert_attestation(_attestation("revision-a"), require_revision_change_from="revision-a")


def test_rc7_new_revision_with_stable_identities_is_eligible_restart_proof(monkeypatch):
    _patch_e2e_identity(monkeypatch)
    e2e._assert_attestation(_attestation("revision-b"), require_revision_change_from="revision-a")


@pytest.mark.parametrize("field, value", [("runtime", "wrong-runtime"), ("source", "wrong-source"), ("config", "wrong-config")])
def test_rc7_new_revision_with_identity_drift_fails(monkeypatch, field, value):
    _patch_e2e_identity(monkeypatch)
    kwargs = {field: value}
    with pytest.raises(RuntimeError):
        e2e._assert_attestation(_attestation("revision-b", **kwargs), require_revision_change_from="revision-a")
