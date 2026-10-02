from __future__ import annotations

from datetime import timedelta

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.fake_business import FakeCRM, FakeGmailRead
from app.adapters.fake_gmail import FakeGmailProvider
from app.api.app import ApiRuntime
from app.domain.models import CustomerRecord, utcnow
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import RenewalWorkflowV08
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService


def build_demo_runtime(database_url: str = "sqlite:///proofops-v09-demo.sqlite") -> ApiRuntime:
    engine = make_engine(database_url)
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    now = utcnow()
    crm = FakeCRM([CustomerRecord(
        customer_id="C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="R2026-C001", renewal_status="pending",
        renewal_date=now + timedelta(days=14), version=1,
    )])
    gmail_read = FakeGmailRead()
    gmail_effect = FakeGmailProvider()
    approvals = ApprovalService(repo)
    effects = EffectExecutor(repo, gmail_effect, reconciliation_grace_seconds=0, min_absence_checks=2)
    consequential = ConsequentialActionService(repo, effects, approvals)
    workflow = RenewalWorkflowV08(
        repo=repo, crm=crm, gmail_read=gmail_read,
        planning=PlanningService(repo, BoundedRulePlanner()),
        consequential=consequential, approvals=approvals,
        coordinator=RunCoordinator(repo),
    )
    return ApiRuntime(workflow=workflow, repo=repo, approvals=approvals)
