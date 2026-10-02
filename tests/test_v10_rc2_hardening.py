from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app.adapters.google_gmail import GoogleGmailProvider
from app.adapters.identity_bound_gmail import CRMIdentityBoundGmailProvider
from app.api.app import ApiSettings, _hash_request, create_app
from app.api.live_runtime import LiveConfigurationError, LiveRuntimeConfig
from app.domain.enums import ActionState, ApprovalStatus, RunState
from app.domain.models import CustomerRecord
from app.persistence.repository import ActionExecutionError
from app.ports.gmail import GmailSearchUnavailable
from app.ports.protocols import ProviderConflict
from tests.test_v09_api import NOW, login, make_api, mut_headers, start_run


def _approve(client, csrf, cp, key="approve"):
    r = client.post(
        f"/api/runs/{cp['run_id']}/approve",
        json={"action_id": cp["action_id"], "ttl_seconds": 600},
        headers=mut_headers(csrf, key),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_rc2_reject_claimed_is_truthful_too_late(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    action = runtime.repo.get_action_by_id(cp["action_id"])
    runtime.approvals.claim(
        approval_id=ap["approval_id"], run_id=cp["run_id"], action_id=action.id,
        payload_hash=action.payload_hash, actor="judge",
    )
    r = client.post(
        f"/api/runs/{cp['run_id']}/reject",
        json={"action_id": action.id, "approval_id": ap["approval_id"]},
        headers=mut_headers(csrf, "reject-too-late"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TOO_LATE_TO_CANCEL"
    assert runtime.repo.get_run(cp["run_id"]).state == RunState.EXECUTING.value
    assert runtime.repo.get_action_by_id(action.id).state == ActionState.PREPARED.value


def test_rc2_reject_confirmed_reports_terminal_not_canceled(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    ex = client.post(
        f"/api/runs/{cp['run_id']}/execute",
        json={"action_id": cp["action_id"], "approval_id": ap["approval_id"]},
        headers=mut_headers(csrf, "exec"),
    )
    assert ex.json()["run_state"] == RunState.COMPLETED_VERIFIED.value
    r = client.post(
        f"/api/runs/{cp['run_id']}/reject",
        json={"action_id": cp["action_id"], "approval_id": ap["approval_id"]},
        headers=mut_headers(csrf, "reject-after"),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ALREADY_TERMINAL"
    assert r.json()["run_state"] == RunState.COMPLETED_VERIFIED.value
    assert r.json()["action_state"] == ActionState.CONFIRMED.value


def test_rc11_unknown_effect_cannot_continue_claimed_authorization(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    body = {"action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    first = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e1"))
    assert first.json()["action_state"] == ActionState.UNKNOWN.value
    second = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e2"))
    assert second.json()["action_state"] == ActionState.UNKNOWN.value
    assert runtime.repo.get_approval(ap["approval_id"]).status == ApprovalStatus.CLAIMED.value
    gmail.configure("success")
    third = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e3"))
    assert third.status_code == 200, third.text
    assert third.json()["action_state"] == ActionState.UNKNOWN.value
    assert gmail.calls == 1
    assert len(gmail.messages) == 0


def test_rc11_unresolved_effect_never_resends_even_when_ledger_payload_is_tampered(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    body = {"action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e1"))
    client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e2"))
    # UNKNOWN is a no-send barrier: no later execute request may invoke the provider.
    action = runtime.repo.get_action_by_id(cp["action_id"])
    original = action.payload_hash
    with runtime.repo.session_factory() as s:
        from app.persistence.models import ActionORM
        row = s.get(ActionORM, action.id)
        row.payload_hash = "0" * 64
        s.commit()
    gmail.configure("success")
    r = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "e3"))
    assert r.status_code == 200
    assert r.json()["action_state"] == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 0
    # restore only to keep the assertion explicit; test database is disposable.
    assert original != "0" * 64


def test_rc2_logout_revokes_saved_cookie(tmp_path):
    client, _, _ = make_api(tmp_path)
    csrf = login(client)
    old_cookie = client.cookies.get("proofops_session")
    r = client.delete("/api/session", headers={"X-CSRF-Token": csrf, "Idempotency-Key": "logout"})
    assert r.status_code == 200
    client.cookies.set("proofops_session", old_cookie)
    assert client.get("/api/session").status_code == 401


def test_rc2_stale_idempotency_recovers_durable_run_without_replay(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": None}
    record, _ = runtime.repo.claim_api_request(
        actor="judge", route="create_run", idempotency_key="crashed",
        request_hash=_hash_request(payload), lease_seconds=1,
    )
    run = runtime.repo.create_run(payload["goal_text"], owner_actor="judge", api_request_id=record.id)
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "crashed"))
    assert r.status_code == 202, r.text
    assert r.headers["Idempotent-Recovery"] == "true"
    assert r.json()["run_id"] == run.id
    assert len(runtime.repo.list_runs_for_actor("judge")) == 1


def test_rc2_live_in_progress_idempotency_still_does_not_replay(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": None}
    runtime.repo.claim_api_request(
        actor="judge", route="create_run", idempotency_key="live",
        request_hash=_hash_request(payload), lease_seconds=60,
    )
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "live"))
    assert r.status_code == 409
    assert runtime.repo.list_runs_for_actor("judge") == []


def test_rc2_stale_business_operation_without_action_can_transfer(tmp_path):
    _, runtime, _ = make_api(tmp_path)
    r1 = runtime.repo.create_run("one", owner_actor="judge")
    runtime.repo.transition_run_state(r1.id, RunState.RESOLVING, expected=RunState.CREATED)
    runtime.repo.transition_run_state(r1.id, RunState.PLANNING, expected=RunState.RESOLVING)
    runtime.repo.claim_business_operation(operation_key="renewal:R", run_id=r1.id, lease_seconds=1)
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r2 = runtime.repo.create_run("two", owner_actor="judge")
    runtime.repo.transition_run_state(r2.id, RunState.RESOLVING, expected=RunState.CREATED)
    op, acquired = runtime.repo.claim_business_operation(operation_key="renewal:R", run_id=r2.id, lease_seconds=60)
    assert acquired and op.owner_run_id == r2.id
    with pytest.raises(ActionExecutionError):
        runtime.repo.assert_business_operation_owner(operation_key="renewal:R", run_id=r1.id)


def test_rc2_stale_business_operation_with_action_cannot_be_stolen(tmp_path):
    _, runtime, _ = make_api(tmp_path)
    r1 = runtime.repo.create_run("one", owner_actor="judge")
    runtime.repo.transition_run_state(r1.id, RunState.RESOLVING, expected=RunState.CREATED)
    runtime.repo.transition_run_state(r1.id, RunState.PLANNING, expected=RunState.RESOLVING)
    runtime.repo.claim_business_operation(operation_key="renewal:R", run_id=r1.id, lease_seconds=1)
    runtime.repo.get_or_create_action(
        run_id=r1.id, logical_key="R:initial", action_type="SEND_EMAIL", target="a@example.com",
        payload_hash="a" * 64, payload_json="{}", initial_state=ActionState.PROPOSED,
    )
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r2 = runtime.repo.create_run("two", owner_actor="judge")
    op, acquired = runtime.repo.claim_business_operation(operation_key="renewal:R", run_id=r2.id, lease_seconds=60)
    assert not acquired and op.owner_run_id == r1.id


def _raw_resource(msg_id: str, *, thread_id: str, sender: str, body: str, internal_ms: int):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    msg["To"] = "proofops-test@example.com"
    msg["Subject"] = "Renewal"
    msg["Message-ID"] = f"<{msg_id}@example>"
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes(policy=policy.SMTP)).decode().rstrip("=")
    return {"id": msg_id, "threadId": thread_id, "internalDate": str(internal_ms), "raw": raw, "labelIds": ["INBOX"]}


class ThreadAwareTransport:
    def __init__(self):
        self.resources = {}
        self.thread_refs = {}
        self.search_called = False

    def search(self, **kwargs):
        self.search_called = True
        return [{"id": f"noise-{i}", "threadId": f"noise-{i}"} for i in range(20)]

    def thread_message_refs(self, *, thread_id):
        return list(self.thread_refs.get(thread_id, []))

    def get_raw(self, *, message_id):
        return self.resources[message_id]

    def send_raw(self, **kwargs):
        raise AssertionError("not used")


def test_rc2_authoritative_thread_not_hidden_by_top_n_sender_mail():
    t = ThreadAwareTransport()
    res = _raw_resource("renewal-old", thread_id="thread-renewal", sender="alice@acme.com", body="We accept the renewal", internal_ms=1000)
    t.resources["renewal-old"] = res
    t.thread_refs["thread-renewal"] = [{"id": "renewal-old", "threadId": "thread-renewal"}]
    p = GoogleGmailProvider(t, sender_email="proofops-test@example.com", contacts={"C":"alice@acme.com"}, renewal_threads={"R": ("thread-renewal",)})
    obs = p.latest_observation("C", renewal_id="R")
    assert obs.intent.value == "ACCEPTED"
    assert not t.search_called


def test_rc2_authoritative_thread_failure_is_not_message_absence():
    class Broken(ThreadAwareTransport):
        def thread_message_refs(self, *, thread_id):
            raise GmailSearchUnavailable("thread unavailable")
    p = GoogleGmailProvider(Broken(), sender_email="proofops-test@example.com", contacts={"C":"alice@acme.com"}, renewal_threads={"R": ("thread-renewal",)})
    with pytest.raises(GmailSearchUnavailable):
        p.latest_observation("C", renewal_id="R")


def test_rc2_live_config_rejects_non_allowlisted_contact():
    env = _valid_live_env()
    env["PROOFOPS_CONTACTS_JSON"] = '{"SMOKE-C001":"ceo@real.example"}'
    with pytest.raises(LiveConfigurationError, match="not allowlisted"):
        LiveRuntimeConfig.from_env(env)


def test_rc2_live_config_rejects_zero_reconciliation_grace():
    env = _valid_live_env()
    env["PROOFOPS_RECONCILIATION_GRACE_SECONDS"] = "0"
    with pytest.raises(LiveConfigurationError, match=">=10"):
        LiveRuntimeConfig.from_env(env)


def _valid_live_env():
    return {
        "PROOFOPS_DATABASE_URL": "postgresql+psycopg://u:p@db.example/proofops",
        "PROOFOPS_GOOGLE_TOKEN_FILE": "/tmp/token.json",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet",
        "PROOFOPS_GMAIL_SENDER": "proofops-test@example.com",
        "PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS": "YES",
        "PROOFOPS_SYNTHETIC_DATASET": "YES",
        "PROOFOPS_CONTACTS_JSON": '{"SMOKE-C001":"proofops-test@example.com"}',
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS": "proofops-test@example.com",
        "PROOFOPS_RENEWAL_THREADS_JSON": '{"SMOKE-R1":["thread-1"]}',
        "GEMINI_API_KEY": "synthetic",
        "PROOFOPS_RECONCILIATION_GRACE_SECONDS": "10",
        "PROOFOPS_MIN_ABSENCE_CHECKS": "2",
    }


def test_rc2_live_send_wrapper_enforces_allowlist():
    class CRM:
        def get_customer(self, customer_id):
            return CustomerRecord(
                customer_id="SMOKE-C001", company="ACME", contact_email="proofops-test@example.com",
                renewal_id="SMOKE-R1", renewal_status="pending", renewal_date=NOW+timedelta(days=5), version=1,
            )
    class Delegate:
        def send(self, **kwargs): return "x"
    wrapped = CRMIdentityBoundGmailProvider(Delegate(), CRM(), configured_contacts={"SMOKE-C001":"proofops-test@example.com"}, allowed_recipients={"proofops-test@example.com"})
    with pytest.raises(ProviderConflict, match="allowlist"):
        wrapped.send(message_id="<x@y>", to="ceo@real.example", subject="x", body="x")


def test_rc2_provider_readiness_probe_has_separate_endpoint(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    assert client.get("/ready/providers").status_code == 401
    login(client)
    assert client.get("/ready/providers").status_code == 503
    runtime.provider_probe = lambda: {"postgresql":"ok","sheets":"ok","gmail":"ok","gemini":"ok"}
    r = client.get("/ready/providers")
    assert r.status_code == 200
    assert r.json()["providers"]["gemini"] == "ok"


def test_rc2_provider_readiness_fails_closed(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    assert client.get("/ready/providers").status_code == 401
    login(client)
    def fail(): raise RuntimeError("gmail unavailable")
    runtime.provider_probe = fail
    r = client.get("/ready/providers")
    assert r.status_code == 503
    assert r.json()["reason"] == "provider-unavailable"
    assert "gmail unavailable" not in r.text


def test_rc2_streamed_oversized_request_without_content_length_is_rejected(tmp_path):
    client, _, _ = make_api(tmp_path)
    def chunks():
        yield b"{" + b"x" * 40000
        yield b"x" * 40000 + b"}"
    r = client.post("/api/session", content=chunks(), headers={"Content-Type":"application/json", "Transfer-Encoding":"chunked"})
    assert r.status_code == 413


def test_rc2_release_version_and_head_are_rc2():
    from app.version import ALEMBIC_HEAD, __version__
    assert __version__ == "1.0.0rc11"
    assert ALEMBIC_HEAD == "0012"


def test_rc2_stale_idempotency_without_committed_resource_reclaims_safely(tmp_path):
    client, runtime, _ = make_api(tmp_path)
    csrf = login(client)
    payload = {"goal_text": "Handle ACME renewal outreach", "company": "ACME", "deadline": None}
    runtime.repo.claim_api_request(
        actor="judge", route="create_run", idempotency_key="crashed-empty",
        request_hash=_hash_request(payload), lease_seconds=1,
    )
    runtime.repo.now_fn.value += timedelta(seconds=2)
    r = client.post("/api/runs", json=payload, headers=mut_headers(csrf, "crashed-empty"))
    assert r.status_code == 201, r.text
    assert len(runtime.repo.list_runs_for_actor("judge")) == 1


def _drive_action_to_unknown(client, runtime, gmail, csrf):
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    body = {"action_id": cp["action_id"], "approval_id": ap["approval_id"]}
    first = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "rt1"))
    assert first.json()["action_state"] == ActionState.UNKNOWN.value
    second = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "rt2"))
    assert second.json()["action_state"] == ActionState.UNKNOWN.value
    return cp, ap, body


def test_rc11_unknown_deadline_expiry_blocks_without_reopening_send(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp, ap, body = _drive_action_to_unknown(client, runtime, gmail, csrf)
    runtime.repo.now_fn.value += timedelta(days=8)
    gmail.configure("success")
    from app.engine.workflow import MaterialStateChanged
    with pytest.raises(MaterialStateChanged, match="GOAL_DEADLINE_EXPIRED_BEFORE_EFFECT"):
        runtime.workflow.execute(
            run_id=cp["run_id"], action_id=cp["action_id"], approval_id=ap["approval_id"], actor="judge"
        )
    assert runtime.repo.get_action_by_id(cp["action_id"]).state != ActionState.CONFIRMED.value
    assert len(gmail.messages) == 0


def test_rc11_unknown_business_drift_blocks_without_reopening_send(tmp_path):
    client, runtime, gmail = make_api(tmp_path, gmail_mode="timeout_before_success")
    csrf = login(client)
    cp, ap, body = _drive_action_to_unknown(client, runtime, gmail, csrf)
    current = runtime.workflow.crm.customers[0]
    runtime.workflow.crm.customers[0] = current.model_copy(
        update={"contact_email": "new-contact@acme.com", "version": current.version + 1}
    )
    gmail.configure("success")
    r = client.post(f"/api/runs/{cp['run_id']}/execute", json=body, headers=mut_headers(csrf, "rt3"))
    # An UNKNOWN action only reconciles its existing provider attempt.  It does not
    # reopen execution, so the changed business evidence cannot authorize a send.
    assert r.status_code == 200
    assert r.json()["action_state"] == ActionState.UNKNOWN.value
    assert runtime.repo.get_action_by_id(cp["action_id"]).state != ActionState.CONFIRMED.value
    assert runtime.repo.get_run(cp["run_id"]).state == RunState.UNKNOWN.value
    assert len(gmail.messages) == 0


def test_rc2_public_placeholder_secrets_are_rejected(monkeypatch):
    from app.api.main import _strong_web_settings
    monkeypatch.setenv("PROOFOPS_SESSION_SECRET", "replace-with-at-least-32-random-bytes")
    monkeypatch.setenv("PROOFOPS_UI_PASSWORD", "replace-with-a-strong-password")
    with pytest.raises(RuntimeError, match="non-placeholder"):
        _strong_web_settings()


def test_rc2_live_mode_requires_https_origin(monkeypatch):
    from app.api.main import _strong_web_settings
    monkeypatch.setenv("PROOFOPS_SESSION_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0")
    monkeypatch.setenv("PROOFOPS_UI_PASSWORD", "Strong-Passphrase-9371")
    monkeypatch.delenv("PROOFOPS_ALLOWED_ORIGIN", raising=False)
    monkeypatch.setenv("PROOFOPS_SECURE_COOKIE", "1")
    with pytest.raises(RuntimeError, match="requires PROOFOPS_ALLOWED_ORIGIN"):
        _strong_web_settings(live=True)


def test_rc2_live_mode_requires_secure_cookie(monkeypatch):
    from app.api.main import _strong_web_settings
    monkeypatch.setenv("PROOFOPS_SESSION_SECRET", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0")
    monkeypatch.setenv("PROOFOPS_UI_PASSWORD", "Strong-Passphrase-9371")
    monkeypatch.setenv("PROOFOPS_ALLOWED_ORIGIN", "https://proofops.example")
    monkeypatch.setenv("PROOFOPS_SECURE_COOKIE", "0")
    with pytest.raises(RuntimeError, match="SECURE_COOKIE=1"):
        _strong_web_settings(live=True)


def test_rc2_live_config_requires_sender_itself_in_allowlist():
    env = _valid_live_env()
    env["PROOFOPS_ALLOWED_TEST_RECIPIENTS"] = "someone-else@example.com"
    with pytest.raises(LiveConfigurationError, match="authenticated Gmail sender"):
        LiveRuntimeConfig.from_env(env)


def test_rc2_authoritative_threads_choose_newest_relevant_message():
    t = ThreadAwareTransport()
    accepted = _raw_resource("m1", thread_id="thread-a", sender="alice@acme.com", body="We accept the renewal", internal_ms=1000)
    declined = _raw_resource("m2", thread_id="thread-b", sender="alice@acme.com", body="We decline the renewal", internal_ms=2000)
    t.resources.update({"m1": accepted, "m2": declined})
    t.thread_refs["thread-a"] = [{"id": "m1", "threadId": "thread-a"}]
    t.thread_refs["thread-b"] = [{"id": "m2", "threadId": "thread-b"}]
    p = GoogleGmailProvider(
        t,
        sender_email="proofops-test@example.com",
        contacts={"C": "alice@acme.com"},
        renewal_threads={"R": ("thread-a", "thread-b")},
    )
    obs = p.latest_observation("C", renewal_id="R")
    assert obs.intent.value == "DECLINED"
    assert obs.message_id == "<m2@example>"


def test_rc2_low_level_confirmed_executor_branch_repairs_without_nameerror(tmp_path):
    client, runtime, gmail = make_api(tmp_path)
    csrf = login(client)
    cp = start_run(client, csrf).json()
    ap = _approve(client, csrf, cp)
    ex = client.post(
        f"/api/runs/{cp['run_id']}/execute",
        json={"action_id": cp["action_id"], "approval_id": ap["approval_id"]},
        headers=mut_headers(csrf, "confirm-low-level"),
    )
    assert ex.status_code == 200
    action = runtime.workflow.consequential.effects.execute_prepared_email(
        action_id=cp["action_id"], approval_id=ap["approval_id"]
    )
    assert action.state == ActionState.CONFIRMED.value


def test_rc2_missing_thread_binding_is_ambiguous_and_empty_binding_is_operator_assertion():
    missing = GoogleGmailProvider(
        ThreadAwareTransport(), sender_email="proofops-test@example.com",
        contacts={"C":"alice@acme.com"}, renewal_threads={},
    ).latest_observation("C", renewal_id="R")
    assert missing.intent.value == "AMBIGUOUS"

    explicit_none = GoogleGmailProvider(
        ThreadAwareTransport(), sender_email="proofops-test@example.com",
        contacts={"C":"alice@acme.com"}, renewal_threads={"R": ()},
    ).latest_observation("C", renewal_id="R")
    assert explicit_none.intent.value == "NONE"
    assert explicit_none.provenance == "operator_assertion"
    assert explicit_none.source_ref.startswith("operator-asserted-no-prior-thread:")


def test_rc2_live_binding_requires_explicit_thread_decision_for_every_renewal():
    from app.api.live_runtime import assert_contact_bindings
    class CRM:
        def get_customer(self, customer_id):
            return CustomerRecord(
                customer_id="SMOKE-C001", company="ACME", contact_email="proofops-test@example.com",
                renewal_id="SMOKE-R1", renewal_status="pending", renewal_date=NOW + timedelta(days=5), version=1,
            )
    with pytest.raises(LiveConfigurationError, match="explicit Gmail thread binding"):
        assert_contact_bindings(CRM(), {"SMOKE-C001":"proofops-test@example.com"}, {})
    # Explicit [] means an operator verified there is no prior renewal conversation.
    assert_contact_bindings(CRM(), {"SMOKE-C001":"proofops-test@example.com"}, {"SMOKE-R1": ()})


def test_rc2_live_gemini_qualification_suite_meets_defined_coverage_floor():
    from scripts.smoke_v10_gemini_live import scenarios
    cases = scenarios()
    assert len(cases) >= 25
    assert sum(c.critical for c in cases) >= 20
    assert {c.expected for c in cases} == {"send", "human", "defer", "none"}
