from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.fake_business import FakeCRM, FakeGmailRead
from app.adapters.fake_gmail import FakeGmailProvider
from app.api.app import ApiRuntime, ApiSettings, create_app
from app.domain.models import CustomerRecord
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import RenewalWorkflowV08
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.value = NOW
    def __call__(self): return self.value


def make_api(tmp_path, *, gmail_mode="success", allowed_origin="http://testserver"):
    engine = make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    create_schema(engine)
    clock = Clock()
    repo = LedgerRepository(make_session_factory(engine), now_fn=clock)
    crm = FakeCRM([CustomerRecord(
        customer_id="C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="R2026-C001", renewal_status="pending",
        renewal_date=NOW + timedelta(days=14), version=1,
    )])
    gmail_read = FakeGmailRead()
    gmail_effect = FakeGmailProvider()
    gmail_effect.configure(gmail_mode, visibility_delay_searches=1 if gmail_mode == "timeout_after_success" else 0)
    approvals = ApprovalService(repo, now_fn=clock)
    effects = EffectExecutor(repo, gmail_effect, reconciliation_grace_seconds=0, min_absence_checks=2, now_fn=clock)
    consequential = ConsequentialActionService(repo, effects, approvals)
    workflow = RenewalWorkflowV08(
        repo=repo, crm=crm, gmail_read=gmail_read,
        planning=PlanningService(repo, BoundedRulePlanner()),
        consequential=consequential, approvals=approvals,
        coordinator=RunCoordinator(repo), now_fn=clock,
    )
    runtime = ApiRuntime(workflow=workflow, repo=repo, approvals=approvals)
    settings = ApiSettings(
        secret_key="0123456789abcdef0123456789abcdef",
        demo_password="secret-demo-password",
        actor="judge",
        session_ttl_seconds=3600,
        allowed_origin=allowed_origin,
    )
    app = create_app(runtime=runtime, settings=settings)
    return TestClient(app), runtime, gmail_effect


def login(client):
    response = client.post("/api/session", json={"password": "secret-demo-password"})
    assert response.status_code == 200
    return response.json()["csrf_token"]


@pytest.mark.parametrize("password", ["é" * 15, "🔒" * 20, "नमस्ते" * 12])
def test_rc11_unicode_invalid_password_is_controlled_auth_failure(tmp_path, password):
    """A08: compare_digest must not turn valid Unicode input into an HTTP 500."""
    client, *_ = make_api(tmp_path)
    response = client.post("/api/session", json={"password": password})
    assert response.status_code == 401


def mut_headers(csrf, key="k1", **extra):
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, **extra}


def start_run(client, csrf, key="create-1"):
    response = client.post(
        "/api/runs",
        json={"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": (NOW + timedelta(days=7)).isoformat()},
        headers=mut_headers(csrf, key),
    )
    assert response.status_code == 201, response.text
    return response


def test_mutation_requires_auth_and_csrf(tmp_path):
    client, *_ = make_api(tmp_path)
    r = client.post("/api/runs", json={"goal_text":"x","company":"ACME"}, headers={"Idempotency-Key":"x"})
    assert r.status_code == 401
    csrf = login(client)
    r = client.post("/api/runs", json={"goal_text":"x","company":"ACME"}, headers={"Idempotency-Key":"x"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "CSRF_FAILED"


def test_cross_origin_mutation_is_rejected(tmp_path):
    client, *_ = make_api(tmp_path)
    csrf = login(client)
    r = client.post(
        "/api/runs", json={"goal_text":"x","company":"ACME"},
        headers=mut_headers(csrf, "x", Origin="https://evil.example"),
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ORIGIN_REJECTED"


def test_tampered_session_cookie_is_rejected(tmp_path):
    client, *_ = make_api(tmp_path)
    login(client)
    token = client.cookies.get("proofops_session")
    client.cookies.set("proofops_session", token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert client.get("/api/session").status_code == 401


def test_create_run_idempotent_replay_does_not_create_second_run(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    first = start_run(client, csrf, "same-create")
    second = start_run(client, csrf, "same-create")
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replay"] == "true"
    assert len(runtime.repo.list_runs_for_actor("judge")) == 1


def test_idempotency_key_cannot_be_reused_for_different_body(tmp_path):
    client, *_ = make_api(tmp_path)
    csrf = login(client)
    start_run(client, csrf, "same-key")
    r = client.post(
        "/api/runs", json={"goal_text":"different","company":"ACME"},
        headers=mut_headers(csrf, "same-key"),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_in_progress_fails_closed_instead_of_reexecuting(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text":"Handle ACME renewal outreach","company":"ACME","deadline":None}
    from app.api.app import _hash_request
    runtime.repo.claim_api_request(actor="judge", route="create_run", idempotency_key="stuck", request_hash=_hash_request(payload))
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "stuck"))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REQUEST_OUTCOME_UNKNOWN"
    assert runtime.repo.list_runs_for_actor("judge") == []


def test_run_owner_prevents_idor(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    foreign = runtime.repo.create_run("foreign", owner_actor="other-user")
    r = client.get(f"/api/runs/{foreign.id}")
    assert r.status_code == 404
    r = client.post(f"/api/runs/{foreign.id}/resume", json={}, headers=mut_headers(csrf, "resume-foreign"))
    assert r.status_code == 404


def test_approval_actor_is_server_session_not_request_controlled(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    r = client.post(f"/api/runs/{cp['run_id']}/approve", json={"action_id":cp["action_id"],"ttl_seconds":600}, headers=mut_headers(csrf,"approve-1"))
    assert r.status_code == 200
    approval = runtime.repo.get_approval(r.json()["approval_id"])
    assert approval.actor == "judge"


def test_duplicate_approval_post_replays_same_approval(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    path=f"/api/runs/{cp['run_id']}/approve"
    body={"action_id":cp["action_id"],"ttl_seconds":600}
    first=client.post(path,json=body,headers=mut_headers(csrf,"approve-same"))
    second=client.post(path,json=body,headers=mut_headers(csrf,"approve-same"))
    assert first.status_code == second.status_code == 200
    assert first.json()==second.json()
    assert len(runtime.repo.approvals_for_run(cp["run_id"])) == 1


def test_duplicate_execute_post_returns_same_response_and_one_email(tmp_path):
    client, _, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = client.post(f"/api/runs/{cp['run_id']}/approve", json={"action_id":cp["action_id"]}, headers=mut_headers(csrf,"a1")).json()
    path=f"/api/runs/{cp['run_id']}/execute"
    body={"action_id":cp["action_id"],"approval_id":ap["approval_id"]}
    first=client.post(path,json=body,headers=mut_headers(csrf,"exec-same"))
    second=client.post(path,json=body,headers=mut_headers(csrf,"exec-same"))
    assert first.status_code == second.status_code == 200
    assert first.json()==second.json()
    assert second.headers["Idempotent-Replay"] == "true"
    assert len(gmail.messages) == 1


def test_new_execute_request_on_unknown_routes_to_reconciliation_not_resend(tmp_path):
    client, _, gmail = make_api(tmp_path, gmail_mode="timeout_after_success")
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = client.post(f"/api/runs/{cp['run_id']}/approve",json={"action_id":cp["action_id"]},headers=mut_headers(csrf,"a1")).json()
    body={"action_id":cp["action_id"],"approval_id":ap["approval_id"]}
    first=client.post(f"/api/runs/{cp['run_id']}/execute",json=body,headers=mut_headers(csrf,"exec-1"))
    assert first.status_code == 200
    assert first.json()["action_state"] == "UNKNOWN"
    assert len(gmail.messages) == 1
    second=client.post(f"/api/runs/{cp['run_id']}/execute",json=body,headers=mut_headers(csrf,"exec-2"))
    assert second.status_code == 200
    assert second.json()["run_state"] == "COMPLETED_VERIFIED"
    assert len(gmail.messages) == 1


def test_reject_cancels_run_and_action_without_send(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp=start_run(client,csrf).json()
    ap=client.post(f"/api/runs/{cp['run_id']}/approve",json={"action_id":cp["action_id"]},headers=mut_headers(csrf,"a1")).json()
    r=client.post(f"/api/runs/{cp['run_id']}/reject",json={"action_id":cp["action_id"],"approval_id":ap["approval_id"]},headers=mut_headers(csrf,"reject-1"))
    assert r.status_code==200
    assert r.json()["run_state"]=="CANCELED"
    assert runtime.repo.get_action_by_id(cp["action_id"]).state=="CANCELED"
    assert len(gmail.messages)==0


def test_run_view_exposes_evidence_and_audit_but_not_approval_nonce(tmp_path):
    client, _, _ = make_api(tmp_path)
    csrf=login(client)
    cp=start_run(client,csrf).json()
    client.post(f"/api/runs/{cp['run_id']}/approve",json={"action_id":cp["action_id"]},headers=mut_headers(csrf,"a1"))
    r=client.get(f"/api/runs/{cp['run_id']}")
    assert r.status_code==200
    body=r.json()
    assert body["timeline"]
    assert body["evidence"]
    assert "nonce" not in body["approvals"][0]


def test_security_headers_and_no_store_are_present(tmp_path):
    client, *_ = make_api(tmp_path)
    r=client.get("/")
    assert r.headers["X-Frame-Options"]=="DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    csrf=login(client)
    r=client.get("/api/session")
    assert r.headers["Cache-Control"]=="no-store"


def test_api_requires_idempotency_key_for_mutations(tmp_path):
    client,*_=make_api(tmp_path)
    csrf=login(client)
    r=client.post("/api/runs",json={"goal_text":"x","company":"ACME"},headers={"X-CSRF-Token":csrf})
    assert r.status_code==400
    assert r.json()["detail"]["code"]=="IDEMPOTENCY_KEY_REQUIRED"


def test_ready_requires_runtime(tmp_path):
    from app.api.app import create_app
    client = TestClient(create_app(runtime=None, settings=ApiSettings(
        secret_key="0123456789abcdef0123456789abcdef",
        demo_password="unused-password",
        actor="nobody",
        login_enabled=False,
    )))
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["reason"] == "runtime-unconfigured"


def test_ready_checks_database(tmp_path):
    client, *_ = make_api(tmp_path)
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_large_request_is_rejected_before_handler(tmp_path):
    client, *_ = make_api(tmp_path)
    big = "x" * 70000
    r = client.post("/api/session", content=big, headers={"Content-Type":"application/json"})
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "REQUEST_TOO_LARGE"


def test_unconfigured_app_does_not_expose_known_login(tmp_path):
    from app.api.app import create_app
    client = TestClient(create_app(runtime=None, settings=ApiSettings(
        secret_key="0123456789abcdef0123456789abcdef",
        demo_password="not-usable",
        actor="unconfigured",
        login_enabled=False,
    )))
    r = client.post("/api/session", json={"password":"not-usable"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "LOGIN_DISABLED"


def test_session_signer_expires_sessions():
    from app.api.security import SessionSigner, SessionError
    now = [1000]
    signer = SessionSigner("0123456789abcdef0123456789abcdef", ttl_seconds=5, now_fn=lambda: now[0])
    token, _ = signer.issue("judge")
    signer.verify(token)
    now[0] = 1006
    import pytest
    with pytest.raises(SessionError, match="expired"):
        signer.verify(token)


def test_ui_has_no_inline_script_and_csp_forbids_inline(tmp_path):
    client, *_ = make_api(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert '<script src="/ui/app.js" defer></script>' in r.text
    assert "<script>" not in r.text
    assert "'unsafe-inline'" not in r.headers["Content-Security-Policy"]
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_runtime_without_explicit_settings_requires_strong_env(monkeypatch, tmp_path):
    import pytest
    client, runtime, _ = make_api(tmp_path)
    del client
    monkeypatch.delenv("PROOFOPS_SESSION_SECRET", raising=False)
    monkeypatch.delenv("PROOFOPS_DEMO_PASSWORD", raising=False)
    from app.api.app import create_app
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(runtime=runtime, settings=None)


def test_two_clients_same_actor_replay_same_idempotency_result(tmp_path):
    client1, runtime, _ = make_api(tmp_path)
    # A second browser instance talking to the same server/app state.
    client2 = TestClient(client1.app)
    csrf1 = login(client1)
    csrf2 = login(client2)
    body={"goal_text":"Handle ACME renewal outreach","company":"ACME","deadline":(NOW+timedelta(days=7)).isoformat()}
    r1=client1.post("/api/runs",json=body,headers=mut_headers(csrf1,"shared-key"))
    r2=client2.post("/api/runs",json=body,headers=mut_headers(csrf2,"shared-key"))
    assert r1.status_code==201
    assert r2.status_code==201
    assert r1.json()==r2.json()
    assert len(runtime.repo.list_runs_for_actor("judge"))==1


def test_https_origin_requires_secure_cookie(tmp_path):
    import pytest
    client, runtime, _ = make_api(tmp_path)
    del client
    with pytest.raises(RuntimeError, match="secure cookies"):
        create_app(runtime=runtime, settings=ApiSettings(
            secret_key="0123456789abcdef0123456789abcdef",
            demo_password="strong-password-123",
            actor="judge",
            allowed_origin="https://proofops.example",
            secure_cookie=False,
        ))
