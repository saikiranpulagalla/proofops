from datetime import datetime, timedelta, timezone

from app.adapters.fake_gmail import FakeGmailProvider
from app.domain.enums import RunState
from app.domain.models import GoalContract, TemporalCondition
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from tests.helpers import advance_run_to_executing


def test_verified_effect_can_finalize_durable_run(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'orchestrator.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME")
    advance_run_to_executing(repo, run.id)
    gmail = FakeGmailProvider()
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"})
    call = dict(
        run_id=run.id, logical_key="R1:initial", customer_id="C001", renewal_id="R1",
        purpose="INITIAL", to="alice@acme.com", subject="Renewal", body="hello", policy=policy,
    )
    action, payload = service.prepare_email(**call)
    approval = approvals.approve(run_id=run.id, action_id=action.id, payload_hash=semantic_payload_hash(payload), actor="demo-user")
    service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)

    now = datetime.now(timezone.utc)
    contract = GoalContract(
        subject_customer_id="C001",
        subject_renewal_id="R1",
        required_conditions=("renewal_state_known", "followup_handled"),
        evidence_requirements=("gmail_sent_message",),
        temporal=TemporalCondition(deadline=now + timedelta(hours=1), evidence_max_age_seconds=3600),
    )
    repo.bind_run_context(
        run_id=run.id,
        workflow_type="TEST",
        subject_customer_id="C001",
        subject_renewal_id="R1",
        goal_contract_json=contract.model_dump_json(),
        snapshot_json='{"customer":{"customer_id":"C001","company":"ACME","contact_email":"alice@acme.com","renewal_id":"R1","renewal_status":"pending","renewal_date":"2026-10-20T17:00:00Z","do_not_contact":false,"version":1},"email_intent":"NONE","recent_email_id":null,"meeting_exists":false,"observed_at":"2026-09-26T06:00:00Z","conflicts":[]}',
    )
    target = RunCoordinator(repo).finalize(
        run_id=run.id,
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        conditions_satisfied_at=now,
        now=now,
    )
    assert target == RunState.COMPLETED_VERIFIED
    assert repo.get_run(run.id).state == RunState.COMPLETED_VERIFIED.value
    assert any(e.event_type == "STATE_TRANSITION" for e in repo.audit_events_for_run(run.id))
