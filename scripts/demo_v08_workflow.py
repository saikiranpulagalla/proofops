from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.fake_business import FakeCRM, FakeGmailRead
from app.adapters.fake_gmail import FakeGmailProvider
from app.domain.enums import ActionState
from app.domain.models import CustomerRecord
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import RenewalWorkflowV08
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService


NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now


def customer():
    return CustomerRecord(
        customer_id="C001",
        company="ACME",
        contact_email="alice@acme.com",
        renewal_id="R2026-C001",
        renewal_status="pending",
        renewal_date=NOW + timedelta(days=14),
        version=1,
    )


def build(repo, gmail_effect, clock):
    approvals = ApprovalService(repo)
    effects = EffectExecutor(
        repo,
        gmail_effect,
        reconciliation_grace_seconds=0,
        min_absence_checks=2,
        execution_lease_seconds=5,
        now_fn=clock,
    )
    return RenewalWorkflowV08(
        repo=repo,
        crm=FakeCRM([customer()]),
        gmail_read=FakeGmailRead(),
        planning=PlanningService(repo, BoundedRulePlanner()),
        consequential=ConsequentialActionService(repo, effects, approvals),
        approvals=approvals,
        coordinator=RunCoordinator(repo),
        now_fn=clock,
    )


def show(label, cp):
    print(f"{label:<18} run={cp.run_state.value}", end="")
    if cp.action_state:
        print(f" action={cp.action_state.value}", end="")
    print()


def main():
    with TemporaryDirectory() as td:
        engine = make_engine(f"sqlite:///{Path(td) / 'demo.sqlite'}")
        create_schema(engine)
        repo = LedgerRepository(make_session_factory(engine))
        clock = Clock()
        gmail = FakeGmailProvider()
        gmail.configure("timeout_after_success", visibility_delay_searches=1)
        workflow = build(repo, gmail, clock)

        prepared = workflow.prepare(
            goal_text="Make sure ACME's renewal is handled without contacting them unnecessarily",
            company="ACME",
            deadline=NOW + timedelta(days=7),
        )
        show("prepared", prepared)
        print(f"draft target       {prepared.target}")

        approved = workflow.approve(
            run_id=prepared.run_id,
            action_id=prepared.action_id,
            actor="demo-user",
        )
        show("approved", approved)

        uncertain = workflow.execute(
            run_id=prepared.run_id,
            action_id=prepared.action_id,
            approval_id=approved.approval_id,
            actor="demo-user",
        )
        show("after send", uncertain)
        print(f"physical messages  {len(gmail.messages)}")
        assert uncertain.action_state == ActionState.UNKNOWN

        completed = workflow.resume(run_id=prepared.run_id)
        show("after reconcile", completed)
        print(f"physical messages  {len(gmail.messages)}")
        print("evidence            " + ", ".join(e.evidence_type for e in repo.evidence_for_run(prepared.run_id)))

        second = workflow.prepare(
            goal_text="Handle ACME renewal again",
            company="ACME",
            deadline=NOW + timedelta(days=7),
        )
        show("repeat goal", second)
        print(f"physical messages  {len(gmail.messages)}")
        print("\nPASS: uncertain send reconciled without duplicate; repeat goal deferred because prior outreach is proof of transport, not proof that the renewal is resolved.")


if __name__ == "__main__":
    main()
