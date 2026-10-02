from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.fake_gmail import FakeGmailProvider
from app.domain.enums import ActionState, RunState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor, deterministic_message_id
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.models import EffectORM
from app.persistence.repository import ActionExecutionError, LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from tests.helpers import advance_run_to_executing


@pytest.fixture
def setup(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ledger.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME renewal")
    advance_run_to_executing(repo, run.id)
    gmail = FakeGmailProvider()
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2, execution_lease_seconds=1)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"})
    return repo, run, gmail, effects, approvals, service, policy


def authorize(setup, *, logical_key="R1:initial", body="hello", subject="Renewal"):
    repo, run, gmail, effects, approvals, service, policy = setup
    kwargs = dict(
        run_id=run.id,
        logical_key=logical_key,
        customer_id="C001",
        renewal_id="R1",
        purpose="INITIAL",
        to="alice@acme.com",
        subject=subject,
        body=body,
        policy=policy,
    )
    action, payload = service.prepare_email(**kwargs)
    approval = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=semantic_payload_hash(payload),
        actor="demo-user",
    )
    return kwargs, action, approval


def execute(setup, **kwargs):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup, **kwargs)
    result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
    return result, action, approval


def test_success_is_verified_and_evidence_persisted(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    action, _old, approval = execute(setup)
    assert action.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1
    evidence = repo.evidence_for_run(run.id)
    assert len(evidence) == 1
    assert evidence[0].verified
    assert evidence[0].expected_hash == evidence[0].observed_hash


def test_timeout_after_success_reconciles_without_duplicate(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    gmail.configure("timeout_after_success")
    action, _old, approval = execute(setup)
    assert action.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1


def test_delayed_visibility_does_not_trigger_blind_retry(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    gmail.configure("timeout_after_success", visibility_delay_searches=1)
    action, _old, approval = execute(setup)
    assert action.state == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 1
    # Second reconciliation observes the already-sent message; still no resend.
    action = effects.reconcile(action.id)
    assert action.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1


def test_rc11_timeout_before_success_remains_unknown_and_never_blindly_retries(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    gmail.configure("timeout_before_success")
    action, _old, approval = execute(setup)
    assert action.state == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 0
    # RC10 A01: provider absence is not proof that it did not commit.  An
    # UNKNOWN effect is deliberately never converted into permission to send.
    action = effects.reconcile(action.id)
    assert action.state == ActionState.UNKNOWN.value
    gmail.configure("success")
    action = effects.execute_prepared_email(action_id=action.id)
    assert action.state == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 0


def test_search_failure_keeps_outcome_unknown(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    gmail.configure("timeout_after_success", search_failures=1)
    action, _old, approval = execute(setup)
    assert action.state == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 1


def test_wrong_payload_with_same_message_id_is_never_confirmed(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup, body="expected", subject="Expected")
    approvals.claim(
        approval_id=approval.id,
        run_id=run.id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="demo-user",
    )
    ext = deterministic_message_id(action.logical_key, action.payload_hash)
    effect = repo.start_attempt(action.id, "gmail", ext, approval_id=approval.id, lease_seconds=1)
    repo.mark_action_and_effect_unknown(action.id, effect.id, "simulated ambiguous outcome")
    repo.transition_run_state(run.id, RunState.UNKNOWN, expected=RunState.EXECUTING)
    gmail.preload(message_id=ext, to="alice@acme.com", subject="WRONG", body="WRONG")
    resolved = effects.reconcile(action.id)
    assert resolved.state == ActionState.FAILED.value
    assert repo.evidence_for_run(run.id) == []


def test_payload_change_on_same_logical_action_is_rejected(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup, body="hello")
    changed = dict(call)
    changed["body"] = "changed"
    with pytest.raises(ValueError):
        service.prepare_email(**changed)


def test_stress_timeout_after_success_never_duplicates(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'stress.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    gmail = FakeGmailProvider()
    gmail.configure("timeout_after_success")
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)

    for i in range(100):
        run = repo.create_run(f"run-{i}")
        advance_run_to_executing(repo, run.id)
        policy = EmailPolicyContext.build(customer_id=f"C{i}", verified_recipients={f"user{i}@example.com"})
        call = dict(
            run_id=run.id, logical_key=f"R{i}:initial", customer_id=f"C{i}", renewal_id=f"R{i}",
            purpose="INITIAL", to=f"user{i}@example.com", subject="Renewal", body="hello", policy=policy,
        )
        action, payload = service.prepare_email(**call)
        approval = approvals.approve(run_id=run.id, action_id=action.id, payload_hash=semantic_payload_hash(payload), actor="demo-user")
        result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
        assert result.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 100


def test_crash_after_provider_commit_recovers_without_resend(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup)
    approvals.claim(approval_id=approval.id, run_id=run.id, action_id=action.id, payload_hash=action.payload_hash, actor="demo-user")
    ext = deterministic_message_id(action.logical_key, action.payload_hash)
    effect = repo.start_attempt(action.id, "gmail", ext, approval_id=approval.id, lease_seconds=1)
    gmail.send(message_id=ext, to="alice@acme.com", subject="Renewal", body="hello")
    with repo.session_factory() as s:
        row = s.get(EffectORM, effect.id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    recovered = effects.recover_expired_executions(now=datetime.now(timezone.utc))
    assert action.id in recovered
    assert repo.get_action_by_id(action.id).state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1


def test_crash_before_provider_call_never_assumes_safe_retry(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup)
    approvals.claim(approval_id=approval.id, run_id=run.id, action_id=action.id, payload_hash=action.payload_hash, actor="demo-user")
    ext = deterministic_message_id(action.logical_key, action.payload_hash)
    effect = repo.start_attempt(action.id, "gmail", ext, approval_id=approval.id, lease_seconds=1)
    with repo.session_factory() as s:
        row = s.get(EffectORM, effect.id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    effects.recover_expired_executions(now=datetime.now(timezone.utc))
    assert repo.get_action_by_id(action.id).state == ActionState.UNKNOWN.value
    effects.reconcile(action.id)
    assert repo.get_action_by_id(action.id).state == ActionState.UNKNOWN.value
    assert len(gmail.messages) == 0


def test_raw_executor_rejects_live_executing_action(setup):
    repo, run, gmail, effects, approvals, service, policy = setup
    call, action, approval = authorize(setup)
    approvals.claim(approval_id=approval.id, run_id=run.id, action_id=action.id, payload_hash=action.payload_hash, actor="demo-user")
    ext = deterministic_message_id(action.logical_key, action.payload_hash)
    repo.start_attempt(action.id, "gmail", ext, approval_id=approval.id, lease_seconds=30)
    with pytest.raises(ActionExecutionError):
        effects.reconcile(action.id)
