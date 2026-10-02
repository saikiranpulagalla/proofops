from pathlib import Path
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.fake_planner import FakePlanner
from app.domain.enums import EmailIntent, RunState
from app.domain.models import BusinessSnapshot, CustomerRecord, GoalContract, TemporalCondition
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.models import PlanActionType, PlanProposal, PlanStep, PlanningContext
from app.planning.service import PlanningService


def main():
    db = Path(tempfile.gettempdir()) / "proofops-v05-planner-demo.sqlite"
    if db.exists():
        db.unlink()
    engine = make_engine(f"sqlite:///{db}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Make sure ACME's renewal is handled by Friday without contacting them unnecessarily")
    repo.transition_run_state(run.id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(run.id, RunState.PLANNING, expected=RunState.RESOLVING)

    customer = CustomerRecord(
        customer_id="C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="R1", renewal_status="pending",
        renewal_date=datetime.now(timezone.utc) + timedelta(days=7),
    )
    context = PlanningContext(
        goal_contract=GoalContract(
            subject_customer_id="C001", subject_renewal_id="R1",
            required_conditions=("renewal_state_known", "followup_handled"),
            temporal=TemporalCondition(evidence_max_age_seconds=3600),
        ),
        snapshot=BusinessSnapshot(customer=customer, email_intent=EmailIntent.NONE, recent_email_id="gmail:m1"),
        evidence_refs=("crm:C001:v1", "gmail:m1"),
        already_satisfied_conditions=frozenset({"renewal_state_known"}),
    )
    proposal = PlanProposal(
        interpretation="ACME needs one bounded renewal outreach.",
        steps=(
            PlanStep(step_id="s1", action=PlanActionType.PREPARE_RENEWAL_OUTREACH, reason="prepare message", evidence_refs=("crm:C001:v1",)),
            PlanStep(step_id="s2", action=PlanActionType.REQUEST_APPROVAL, reason="external write requires approval", evidence_refs=("crm:C001:v1",), depends_on=("s1",)),
            PlanStep(step_id="s3", action=PlanActionType.SEND_RENEWAL_OUTREACH, reason="renewal is due and no response exists", evidence_refs=("crm:C001:v1", "gmail:m1"), depends_on=("s2",)),
            PlanStep(step_id="s4", action=PlanActionType.VERIFY_OUTREACH, reason="prove the external effect", evidence_refs=("crm:C001:v1",), depends_on=("s3",)),
        ),
    )

    compiled = PlanningService(repo, FakePlanner(proposal)).propose_and_validate(run_id=run.id, context=context)
    action = compiled.actions[0]
    print("1. Planner proposed a typed plan with no tool authority")
    print("2. Deterministic validator accepted prepare -> approval -> send -> verify")
    print(f"3. Trusted compiler derived recipient: {action.target}")
    print(f"4. Trusted compiler derived logical key: {action.logical_key}")
    print(f"5. Trusted compiler derived risk: {action.risk}")
    print(f"6. Durable plan audit rows: {len(repo.plans_for_run(run.id))}")
    print(f"7. Run state remains: {repo.get_run(run.id).state}")
    print("PASS: model proposal is validated/audited but cannot execute or choose consequential identity fields")


if __name__ == "__main__":
    main()
