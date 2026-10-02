from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.fake_gmail import FakeGmailProvider
from app.domain.enums import ActionState, ApprovalStatus
from app.engine.consequential import ConsequentialActionService, CrossRunActionError
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import ActionExecutionError, LedgerRepository
from app.security.approval import ApprovalError, ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext, PolicyViolation
from tests.helpers import advance_run_to_executing


@pytest.fixture
def system(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'v03.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME")
    advance_run_to_executing(repo, run.id)
    gmail = FakeGmailProvider()
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2)
    approvals = ApprovalService(repo)
    svc = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"})
    return repo, run, gmail, approvals, svc, effects, policy


def prepare(system, **overrides):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs = dict(
        run_id=run.id,
        logical_key="R2026-C001:initial_outreach",
        customer_id="C001",
        renewal_id="R2026-C001",
        purpose="INITIAL_RENEWAL_OUTREACH",
        to="alice@acme.com",
        subject="Renewal",
        body="Hello Alice",
        policy=policy,
    )
    kwargs.update(overrides)
    action, payload = svc.prepare_email(**kwargs)
    approval = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=semantic_payload_hash(payload),
        actor="demo-user",
    )
    return kwargs, action, approval


def test_valid_approval_executes_once(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    result = svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert result.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1
    assert repo.get_approval(approval.id).status == ApprovalStatus.CONSUMED.value


def test_no_approval_cannot_execute(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, _approval = prepare(system)
    # Revoke the real approval and then try a nonexistent approval id.
    approvals.revoke(_approval.id)
    with pytest.raises(ApprovalError):
        svc.execute_approved_email(approval_id=999999, actor="demo-user", **kwargs)
    assert len(gmail.messages) == 0


def test_payload_change_after_approval_invalidates(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    kwargs["body"] = "Changed after approval"
    with pytest.raises((ApprovalError, ValueError)):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert len(gmail.messages) == 0


def test_recipient_change_after_approval_is_blocked_by_policy(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    kwargs["to"] = "attacker@example.com"
    with pytest.raises(PolicyViolation):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert len(gmail.messages) == 0


def test_wrong_customer_is_blocked(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    kwargs["customer_id"] = "C999"
    with pytest.raises(PolicyViolation):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert len(gmail.messages) == 0


def test_do_not_contact_policy_blocks_even_before_approval(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'dnc.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME")
    advance_run_to_executing(repo, run.id)
    gmail = FakeGmailProvider()
    svc = ConsequentialActionService(repo, EffectExecutor(repo, gmail), ApprovalService(repo))
    policy = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"}, do_not_contact=True)
    with pytest.raises(PolicyViolation):
        svc.prepare_email(
            run_id=run.id, logical_key="R1:initial", customer_id="C001", renewal_id="R1",
            purpose="INITIAL", to="alice@acme.com", subject="Renewal", body="Hi", policy=policy,
        )
    assert len(gmail.messages) == 0


def test_revoked_approval_blocks_and_action_can_be_reapproved(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    approvals.revoke(approval.id)
    assert repo.get_action_by_id(action.id).state == ActionState.APPROVAL_REQUIRED.value
    with pytest.raises(ApprovalError):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert len(gmail.messages) == 0


def test_expired_approval_blocks_and_returns_action_to_approval_required(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    with repo.session_factory() as s:
        row = s.get(type(approval), approval.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    with pytest.raises(ApprovalError):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs)
    assert repo.get_action_by_id(action.id).state == ActionState.APPROVAL_REQUIRED.value
    assert len(gmail.messages) == 0


def test_approval_for_other_action_cannot_be_replayed(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    # Keep the same run waiting; create a second action directly in APPROVAL_REQUIRED for isolation test.
    other, _ = repo.get_or_create_action(
        run_id=run.id,
        logical_key="R2026-C001:followup_1",
        action_type="SEND_EMAIL",
        target="alice@acme.com",
        payload_hash="f" * 64,
        payload_json='{"type":"SEND_EMAIL"}',
        initial_state=ActionState.APPROVAL_REQUIRED,
    )
    with pytest.raises(ApprovalError):
        approvals.verify(
            approval_id=approval.id,
            run_id=run.id,
            action_id=other.id,
            payload_hash="f" * 64,
            actor="demo-user",
        )


def test_wrong_actor_cannot_claim_approval(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    with pytest.raises(ApprovalError):
        svc.execute_approved_email(approval_id=approval.id, actor="other-user", **kwargs)
    assert len(gmail.messages) == 0


def test_cross_run_execution_cannot_reuse_action_or_approval(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    run2 = repo.create_run("same goal again")
    advance_run_to_executing(repo, run2.id)
    kwargs2 = dict(kwargs)
    kwargs2["run_id"] = run2.id
    with pytest.raises(CrossRunActionError):
        svc.execute_approved_email(approval_id=approval.id, actor="demo-user", **kwargs2)
    assert len(gmail.messages) == 0


def test_claimed_approval_cannot_be_revoked_mid_execution(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    approvals.claim(
        approval_id=approval.id,
        run_id=run.id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="demo-user",
    )
    with pytest.raises(ApprovalError):
        approvals.revoke(approval.id)
    assert repo.get_approval(approval.id).status == ApprovalStatus.CLAIMED.value


def test_raw_executor_cannot_send_unapproved_action(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    # Action is APPROVED but not atomically claimed -> not PREPARED.
    with pytest.raises(ActionExecutionError):
        effects.execute_prepared_email(action_id=action.id, approval_id=approval.id)
    assert len(gmail.messages) == 0


def test_requesting_approval_after_expiry_issues_fresh_approval(system):
    repo, run, gmail, approvals, svc, effects, policy = system
    kwargs, action, approval = prepare(system)
    with repo.session_factory() as sdb:
        row = sdb.get(type(approval), approval.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        sdb.commit()
    fresh = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="demo-user",
        ttl_seconds=600,
    )
    assert fresh.id != approval.id
    assert repo.get_approval(approval.id).status == ApprovalStatus.EXPIRED.value
    assert repo.get_approval(fresh.id).status == ApprovalStatus.ACTIVE.value
