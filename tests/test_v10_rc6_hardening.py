from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from app.api.app import _hash_request
from app.domain.enums import ApprovalStatus, RunState
from app.engine.workflow import MaterialStateChanged
from app.release.qualification import release_identity, runtime_payload_fingerprint, source_tree_fingerprint
from scripts.smoke_v10_e2e_live import _assert_attestation
from tests.test_v09_api import NOW, login, make_api, mut_headers, start_run

ROOT = Path(__file__).resolve().parents[1]


def _approve_api(client, csrf, cp, *, key: str, ttl: int):
    return client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": ttl},
        headers=mut_headers(csrf, key),
    )


def test_rc6_explicit_user_no_contact_goal_cannot_create_send_action(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    r = client.post(
        "/api/runs",
        json={
            "goal_text": "Do not contact ACME. Do not send any email.",
            "company": "ACME",
            "deadline": (NOW + timedelta(days=7)).isoformat(),
        },
        headers=mut_headers(csrf, "goal-no-contact"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_state"] == RunState.BLOCKED_NEEDS_HUMAN.value
    assert body["action_id"] is None
    assert runtime.repo.actions_for_run(body["run_id"]) == []
    assert len(gmail.messages) == 0


def test_rc6_explicit_goal_subject_conflict_blocks_before_action(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    r = client.post(
        "/api/runs",
        json={"goal_text": "Handle BETA's renewal", "company": "ACME"},
        headers=mut_headers(csrf, "goal-subject-conflict"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_state"] == RunState.BLOCKED_NEEDS_HUMAN.value
    assert "GOAL_SUBJECT_CONFLICT" in body["reasons"]
    assert runtime.repo.actions_for_run(body["run_id"]) == []
    assert len(gmail.messages) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"renewal_date": NOW + timedelta(days=120)},
        {"renewal_date": NOW - timedelta(days=1)},
        {"renewal_date": None},
        {"company": "NOT-ACME"},
    ],
)
def test_rc6_same_version_business_drift_invalidates_approval(tmp_path, changes):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf, key=f"drift-create-{hash(str(changes))}").json()
    ap = _approve_api(client, csrf, cp, key=f"drift-approve-{hash(str(changes))}", ttl=600)
    assert ap.status_code == 200, ap.text
    approval_id = ap.json()["approval_id"]

    crm = runtime.workflow.crm
    original = crm.customers[0]
    crm.customers[0] = original.model_copy(update={**changes, "version": original.version})

    with pytest.raises(MaterialStateChanged):
        runtime.workflow.execute(
            run_id=cp["run_id"], action_id=cp["action_id"],
            approval_id=approval_id, actor="judge",
        )
    assert len(gmail.messages) == 0
    assert runtime.repo.get_run(cp["run_id"]).state == RunState.BLOCKED_NEEDS_HUMAN.value
    assert runtime.repo.get_approval(approval_id).status in {
        ApprovalStatus.REVOKED.value, ApprovalStatus.EXPIRED.value,
    }


def test_rc6_different_approval_ttl_cannot_reuse_existing_authorization(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf, key="ttl-create").json()
    long = _approve_api(client, csrf, cp, key="ttl-long", ttl=3600)
    assert long.status_code == 200, long.text
    long_id = long.json()["approval_id"]

    short = _approve_api(client, csrf, cp, key="ttl-short", ttl=30)
    assert short.status_code == 409, short.text
    approvals = runtime.repo.approvals_for_run(cp["run_id"])
    assert [a.id for a in approvals] == [long_id]
    assert approvals[0].api_request_id is not None


def test_rc6_stale_approval_request_cannot_recover_another_requests_approval(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf, key="causal-create").json()
    old_payload = {"run_id": cp["run_id"], "action_id": cp["action_id"], "ttl_seconds": 30}
    old_record, created = runtime.repo.claim_api_request(
        actor="judge", route="approve", idempotency_key="old-approval",
        request_hash=_hash_request(old_payload), resource_id=str(cp["run_id"]), lease_seconds=1,
    )
    assert created

    newer = _approve_api(client, csrf, cp, key="new-approval", ttl=3600)
    assert newer.status_code == 200, newer.text
    new_id = newer.json()["approval_id"]
    assert runtime.repo.get_approval(new_id).api_request_id != old_record.id

    runtime.repo.now_fn.value += timedelta(seconds=2)
    recovered = _approve_api(client, csrf, cp, key="old-approval", ttl=30)
    assert recovered.status_code == 409, recovered.text
    assert recovered.headers.get("Idempotent-Recovery") is None
    assert runtime.repo.get_approval(new_id).api_request_id != old_record.id


def test_rc6_shared_deployment_generation_prevents_multiworker_boot_false_positive(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFOPS_QUALIFICATION_MODE", "YES")
    monkeypatch.setenv("PROOFOPS_DEPLOYMENT_REVISION", "revision-a")
    client1, _, _ = make_api(tmp_path)
    login(client1)
    first = client1.get("/api/qualification/attestation").json()
    assert first["deployment_generation"] == 1

    # A second worker starts against the same deployment DB/config. Every worker now
    # reports the shared latest generation, so merely landing on a different worker
    # cannot look like a later restart generation.
    client2, _, _ = make_api(tmp_path)
    login(client2)
    second = client2.get("/api/qualification/attestation").json()
    first_after = client1.get("/api/qualification/attestation").json()
    assert second["deployment_generation"] == 2
    assert first_after["deployment_generation"] == 2
    assert second["boot_id"] != first["boot_id"]
    # RC6 generation remains useful worker diagnostics, but cannot satisfy RC7's
    # deployment-restart condition: both workers expose the same infrastructure revision.
    with pytest.raises(RuntimeError, match="did not change"):
        _assert_attestation(first_after, require_revision_change_from="revision-a")


def test_rc6_runtime_payload_fingerprint_is_stable_across_docker_like_layout(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    for dirname in ("app", "scripts", "alembic"):
        shutil.copytree(ROOT / dirname, runtime_root / dirname)
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(ROOT / name, runtime_root / name)

    assert runtime_payload_fingerprint(ROOT) == runtime_payload_fingerprint(runtime_root)
    # Release-source identity deliberately includes tests/deployment files and therefore
    # is distinct from the stripped runtime payload identity.
    assert source_tree_fingerprint(ROOT) != source_tree_fingerprint(runtime_root)



def test_rc6_original_demo_goal_does_not_become_blanket_no_contact(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    r = client.post(
        "/api/runs",
        json={
            "goal_text": "Make sure ACME's renewal is handled by Friday without contacting them unnecessarily.",
            "company": "ACME",
            "deadline": (NOW + timedelta(days=7)).isoformat(),
        },
        headers=mut_headers(csrf, "goal-demo-safe"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_state"] == RunState.WAITING_APPROVAL.value
    assert body["action_id"] is not None
    assert len(gmail.messages) == 0


def test_rc6_frozen_release_identity_validates_stripped_runtime_payload(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    for dirname in ("app", "scripts", "alembic"):
        shutil.copytree(ROOT / dirname, runtime_root / dirname)
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(ROOT / name, runtime_root / name)

    frozen = release_identity(ROOT)
    (runtime_root / "release_identity.json").write_text(__import__("json").dumps(frozen, sort_keys=True), encoding="utf-8")
    observed = release_identity(runtime_root)
    assert observed == frozen
    assert observed["runtime_payload_fingerprint"] == runtime_payload_fingerprint(runtime_root)
    assert source_tree_fingerprint(runtime_root) != frozen["release_source_fingerprint"]

def test_rc6_login_throttle_is_shared_and_survives_clock_advance(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    for _ in range(5):
        r = client.post("/api/session", json={"password": "wrong-password-value"})
        assert r.status_code == 401
    blocked = client.post("/api/session", json={"password": "secret-demo-password"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "LOGIN_RATE_LIMITED"

    runtime.repo.now_fn.value += timedelta(seconds=301)
    ok = client.post("/api/session", json={"password": "secret-demo-password"})
    assert ok.status_code == 200, ok.text
