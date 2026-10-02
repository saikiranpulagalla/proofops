from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
import threading

import pytest
from sqlalchemy import func, select

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.google_gmail import GoogleGmailProvider
from app.domain.enums import ActionState, ApprovalStatus, EmailIntent, RunState
from app.domain.models import GoalContract, TemporalCondition
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor, deterministic_message_id
from app.engine.orchestrator import RunCoordinator
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.models import ActionORM, ApprovalORM, EffectORM, RunORM
from app.persistence.repository import EvidenceDriftError, LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalError, ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from tests.helpers import advance_run_to_executing
from tests.test_google_gmail import MemoryGmailTransport, encode_raw, raw_message
from tests.test_google_sheets import MemorySheetsTransport, row
from tests.test_v08_workflow import Clock, NOW, make_workflow


def test_k01_effect_only_cross_run_evidence_is_rejected(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    action = repo.get_action_by_id(cp.action_id)
    workflow.approvals.claim(
        approval_id=ap.approval_id,
        run_id=cp.run_id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="judge",
    )
    effect = repo.start_attempt(
        action.id,
        "gmail",
        deterministic_message_id(action.logical_key, action.payload_hash),
        approval_id=ap.approval_id,
    )
    other = repo.create_run("other")
    with pytest.raises(ValueError, match="another run"):
        repo.record_evidence(
            run_id=other.id,
            effect_id=effect.id,
            customer_id="C999",
            renewal_id="R999",
            evidence_type="gmail_sent_message",
            source_ref="foreign-proof",
            expected="x",
            observed="x",
            observed_at=NOW,
            verified=True,
        )
    assert repo.evidence_for_run(other.id) == []


def test_k02_persisted_goal_contract_is_only_completion_authority(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'contract.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine), now_fn=lambda: NOW)
    run = repo.create_run("strict")
    contract = GoalContract(
        subject_customer_id="C1",
        subject_renewal_id="R1",
        required_conditions=("must_have_proof",),
        evidence_requirements=("proof",),
        temporal=TemporalCondition(deadline=NOW + timedelta(days=1)),
    )
    repo.bind_run_context(
        run_id=run.id,
        workflow_type="TEST",
        subject_customer_id="C1",
        subject_renewal_id="R1",
        goal_contract_json=contract.model_dump_json(),
        snapshot_json="{}",
    )
    advance_run_to_executing(repo, run.id)
    result = RunCoordinator(repo).finalize(
        run_id=run.id,
        satisfied_conditions=frozenset({"must_have_proof"}),
        now=NOW,
    )
    assert result == RunState.UNKNOWN
    with pytest.raises(TypeError):
        RunCoordinator(repo).finalize(  # type: ignore[call-arg]
            run_id=run.id,
            contract=GoalContract(),
            satisfied_conditions=frozenset(),
            now=NOW,
        )


def test_k03_confirmed_without_evidence_is_repaired_on_resume(tmp_path):
    workflow, repo, _sheets, gmail_t, clock = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    action = repo.get_action_by_id(cp.action_id)
    workflow.approvals.claim(
        approval_id=ap.approval_id,
        run_id=cp.run_id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="judge",
    )
    external_key = deterministic_message_id(action.logical_key, action.payload_hash)
    effect = repo.start_attempt(action.id, "gmail", external_key, approval_id=ap.approval_id)
    payload = json.loads(action.payload_json)
    provider_ref = workflow.consequential.effects.gmail.send(
        message_id=external_key,
        to=payload["to"][0],
        subject=payload["subject"],
        body=payload["body_text"],
    )
    # Simulate the legacy crash window: states committed, proof/approval transaction lost.
    with repo.session_factory() as s:
        s.get(ActionORM, action.id).state = ActionState.CONFIRMED.value
        e = s.get(EffectORM, effect.id)
        e.state = ActionState.CONFIRMED.value
        e.provider_ref = provider_ref
        s.commit()
    assert repo.evidence_for_run(cp.run_id)
    # Only pre-action evidence exists; provider proof is absent.
    assert "gmail_sent_message" not in {e.evidence_type for e in repo.evidence_for_run(cp.run_id)}
    resumed = workflow.resume(run_id=cp.run_id)
    assert resumed.run_state == RunState.COMPLETED_VERIFIED
    assert "gmail_sent_message" in {e.evidence_type for e in repo.evidence_for_run(cp.run_id)}
    assert repo.get_approval(ap.approval_id).status == ApprovalStatus.CONSUMED.value
    assert gmail_t.sent_calls == 1


def test_k04_confirmation_transaction_rolls_back_if_evidence_conflicts(tmp_path):
    workflow, repo, _sheets, gmail_t, _clock = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    # Reserve the provider source with different immutable content.  The later atomic
    # confirmation must fail and roll back state changes rather than committing CONFIRMED.
    repo.record_evidence(
        run_id=cp.run_id,
        customer_id="C001",
        renewal_id="R2026-C001",
        evidence_type="gmail_sent_message",
        source_ref="sent-1",
        expected="wrong",
        observed="wrong",
        observed_at=NOW,
        verified=True,
    )
    with pytest.raises((EvidenceDriftError, ValueError)):
        workflow.execute(
            run_id=cp.run_id,
            action_id=cp.action_id,
            approval_id=ap.approval_id,
            actor="judge",
        )
    action = repo.get_action_by_id(cp.action_id)
    attempt = repo.latest_attempt(cp.action_id)
    assert action.state == ActionState.EXECUTING.value
    assert attempt.state == ActionState.EXECUTING.value
    assert repo.get_approval(ap.approval_id).status == ApprovalStatus.CLAIMED.value
    assert gmail_t.sent_calls == 1


@pytest.mark.parametrize(
    "body, expected_state",
    [
        ("We accept the renewal", RunState.BLOCKED_NEEDS_HUMAN),
        ("We will not renew this year", RunState.BLOCKED_NEEDS_HUMAN),
        ("Please do not contact us again", RunState.BLOCKED_NEEDS_HUMAN),
    ],
)
def test_k05_k07_new_terminal_business_evidence_after_prior_outreach_never_false_completes(
    tmp_path, body, expected_state
):
    workflow, repo, _sheets, gmail_t, _clock = make_workflow(tmp_path)
    first = workflow.prepare(goal_text="Ensure renewal outreach", company="ACME")
    ap = workflow.approve(run_id=first.run_id, action_id=first.action_id, actor="judge")
    done = workflow.execute(
        run_id=first.run_id,
        action_id=first.action_id,
        approval_id=ap.approval_id,
        actor="judge",
    )
    assert done.run_state == RunState.COMPLETED_VERIFIED
    gmail_t.resources["customer-reply"] = raw_message(
        provider_ref="customer-reply",
        sender="alice@acme.com",
        body=body,
        internal_ms=int((NOW + timedelta(minutes=1)).timestamp() * 1000),
        thread_id="renewal-thread",
    )
    second = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert second.run_state == expected_state
    assert second.run_state != RunState.COMPLETED_NO_ACTION_REQUIRED
    assert gmail_t.sent_calls == 1


def test_k08_unrelated_newest_email_is_not_renewal_evidence():
    older = raw_message(
        provider_ref="renewal",
        body="We accept the renewal",
        internal_ms=1790000000000,
        thread_id="renewal-thread",
    )
    newer = raw_message(
        provider_ref="lunch",
        subject="Lunch",
        body="Please wait, not ready yet",
        internal_ms=1791000000000,
        thread_id="unrelated-thread",
    )
    transport = MemoryGmailTransport(resources={"renewal": older, "lunch": newer})
    provider = GoogleGmailProvider(
        transport,
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"},
        renewal_threads={"R1": ("renewal-thread",)},
    )
    obs = provider.latest_observation("C001", renewal_id="R1")
    assert obs.intent == EmailIntent.ACCEPTED
    assert obs.source_ref == "renewal"


def test_k09_html_quoted_history_is_not_fresh_intent():
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "alice@acme.com"
    msg["To"] = "demo@example.com"
    msg["Subject"] = "Re: Renewal"
    msg["Message-ID"] = "<html-quote@example>"
    msg.set_content('<html><body><blockquote>We accept the renewal</blockquote></body></html>', subtype="html")
    resource = {
        "id": "html-q",
        "threadId": "renewal-thread",
        "labelIds": ["INBOX"],
        "internalDate": str(int(NOW.timestamp() * 1000)),
        "raw": encode_raw(msg),
    }
    provider = GoogleGmailProvider(
        MemoryGmailTransport(resources={"html-q": resource}),
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"},
        renewal_threads={"R1": ("renewal-thread",)},
    )
    obs = provider.latest_observation("C001", renewal_id="R1")
    assert obs.quoted_only is True
    assert obs.intent == EmailIntent.AMBIGUOUS


def test_k10_terminal_renewed_with_past_date_is_not_overdue(tmp_path):
    sheets = MemorySheetsTransport(
        all_rows=[row(status="renewed", renewal_date="2026-09-01T17:00:00+00:00")]
    )
    workflow, _repo, _sheets, gmail_t, _clock = make_workflow(tmp_path, sheets_transport=sheets)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert cp.run_state == RunState.COMPLETED_NO_ACTION_REQUIRED
    assert gmail_t.sent_calls == 0


class FlakyPlanner:
    model_name = "flaky-test-planner"

    def __init__(self):
        self.calls = 0
        self.delegate = BoundedRulePlanner()

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary planner outage")
        return self.delegate.propose(context)


def test_k11_planner_failure_has_explicit_retry_path(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    flaky = FlakyPlanner()
    workflow.planning = PlanningService(repo, flaky)
    first = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert first.run_state == RunState.PLANNING
    assert first.reasons and first.reasons[0].startswith("PLANNER_FAILED:")
    retried = workflow.retry_planning(run_id=first.run_id)
    assert retried.run_state == RunState.WAITING_APPROVAL
    assert flaky.calls == 2


def test_k12_concurrent_business_operation_claim_has_one_owner(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ops.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine), now_fn=lambda: NOW)
    r1 = repo.create_run("one")
    r2 = repo.create_run("two")
    repo.transition_run_state(r1.id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(r2.id, RunState.RESOLVING, expected=RunState.CREATED)
    barrier = threading.Barrier(2)

    def claim(run_id):
        barrier.wait(timeout=5)
        return repo.claim_business_operation(operation_key="renewal:R1", run_id=run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (r1.id, r2.id)))
    acquired = [ok for _, ok in results]
    assert sorted(acquired) == [False, True]
    owner = repo.get_business_operation("renewal:R1")
    assert owner.owner_run_id in {r1.id, r2.id}


def test_k13_approval_ttl_uses_workflow_clock(tmp_path):
    clock = Clock(NOW)
    workflow, repo, _sheets, gmail_t, _ = make_workflow(tmp_path, clock=clock)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge", ttl_seconds=1)
    clock.advance(seconds=2)
    with pytest.raises(ApprovalError, match="expired"):
        workflow.execute(
            run_id=cp.run_id,
            action_id=cp.action_id,
            approval_id=ap.approval_id,
            actor="judge",
        )
    assert gmail_t.sent_calls == 0
    assert repo.get_approval(ap.approval_id).status == ApprovalStatus.EXPIRED.value


def test_k14_execution_lease_uses_workflow_clock(tmp_path):
    clock = Clock(NOW)
    workflow, repo, _sheets, _gmail_t, _ = make_workflow(tmp_path, clock=clock, grace=60)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    action = repo.get_action_by_id(cp.action_id)
    workflow.approvals.claim(
        approval_id=ap.approval_id,
        run_id=cp.run_id,
        action_id=action.id,
        payload_hash=action.payload_hash,
        actor="judge",
    )
    effect = repo.start_attempt(
        action.id,
        "gmail",
        deterministic_message_id(action.logical_key, action.payload_hash),
        approval_id=ap.approval_id,
        lease_seconds=1,
    )
    assert effect.lease_expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=1)
    clock.advance(seconds=2)
    assert workflow.consequential.effects.recover_expired_execution(action.id, now=clock()) is True
    assert repo.get_action_by_id(action.id).state != ActionState.EXECUTING.value


def test_k15_same_evidence_source_with_changed_content_is_anomaly(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    crm = next(e for e in repo.evidence_for_run(cp.run_id) if e.evidence_type == "crm_snapshot")
    with pytest.raises(EvidenceDriftError, match="content drift"):
        repo.record_evidence(
            run_id=cp.run_id,
            customer_id=crm.customer_id,
            renewal_id=crm.renewal_id,
            evidence_type=crm.evidence_type,
            source_ref=crm.source_ref,
            expected=crm.expected_hash,
            observed="tampered-hash",
            observed_at=crm.observed_at,
            verified=True,
        )


def test_k16_naive_deadline_is_rejected_before_run_creation(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        workflow.prepare(
            goal_text="Handle ACME renewal",
            company="ACME",
            deadline=datetime(2026, 9, 27, 12, 0),
        )
    with repo.session_factory() as s:
        assert s.scalar(select(func.count(RunORM.id))) == 0


def test_k17_scoped_recovery_does_not_mutate_unrelated_action(tmp_path):
    clock = Clock(NOW)
    engine = make_engine(f"sqlite:///{tmp_path / 'recovery.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine), now_fn=clock)
    gmail = MemoryGmailTransport()
    provider = GoogleGmailProvider(gmail, sender_email="demo@example.com")
    effects = EffectExecutor(repo, provider, reconciliation_grace_seconds=60, now_fn=clock)
    approvals = ApprovalService(repo, now_fn=clock)
    service = ConsequentialActionService(repo, effects, approvals)

    actions = []
    for i in (1, 2):
        run = repo.create_run(f"r{i}")
        advance_run_to_executing(repo, run.id)
        policy_ctx = EmailPolicyContext.build(
            customer_id=f"C{i}", verified_recipients={f"u{i}@example.com"}
        )
        call = dict(
            run_id=run.id,
            logical_key=f"R{i}:initial",
            customer_id=f"C{i}",
            renewal_id=f"R{i}",
            purpose="INITIAL",
            to=f"u{i}@example.com",
            subject="Renewal",
            body="hello",
            policy=policy_ctx,
        )
        action, payload = service.prepare_email(**call)
        approval = approvals.approve(
            run_id=run.id,
            action_id=action.id,
            payload_hash=semantic_payload_hash(payload),
            actor="judge",
        )
        approvals.claim(
            approval_id=approval.id,
            run_id=run.id,
            action_id=action.id,
            payload_hash=action.payload_hash,
            actor="judge",
        )
        repo.start_attempt(
            action.id,
            "gmail",
            deterministic_message_id(action.logical_key, action.payload_hash),
            approval_id=approval.id,
            lease_seconds=1,
        )
        actions.append(action.id)
    clock.advance(seconds=2)
    effects.recover_expired_execution(actions[0], now=clock())
    assert repo.get_action_by_id(actions[0]).state != ActionState.EXECUTING.value
    assert repo.get_action_by_id(actions[1]).state == ActionState.EXECUTING.value


def test_k18_unscoped_customer_mail_is_not_treated_as_renewal_absence():
    transport = MemoryGmailTransport(
        resources={"x": raw_message(provider_ref="x", body="Please wait", thread_id="other")}
    )
    provider = GoogleGmailProvider(
        transport,
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"},
        renewal_threads={},
    )
    obs = provider.latest_observation("C001", renewal_id="R1")
    assert obs.intent == EmailIntent.AMBIGUOUS
    assert obs.source_ref and obs.source_ref.startswith("unscoped:")


def test_k19_workflow_contract_explicitly_scopes_completion_to_outreach(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    run = repo.get_run(cp.run_id)
    contract = GoalContract.model_validate_json(run.goal_contract_json)
    assert run.workflow_type == "RENEWAL_OUTREACH_V081"
    assert "outreach_verified" in contract.required_conditions
    assert "gmail_sent_message" in contract.evidence_requirements


def test_k20_health_version_matches_integrity_release():
    from app.api.main import health

    status = health()
    assert status["version"] == "1.0.0rc11"
    assert status["stage"] == "release-candidate"
