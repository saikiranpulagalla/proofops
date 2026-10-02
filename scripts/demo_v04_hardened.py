from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.fake_gmail import FakeGmailProvider
from app.domain.enums import ActionState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext


def advance(repo, run_id):
    from app.domain.enums import RunState
    repo.transition_run_state(run_id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(run_id, RunState.PLANNING, expected=RunState.RESOLVING)
    repo.transition_run_state(run_id, RunState.EXECUTING, expected=RunState.PLANNING)


def main():
    db = Path(tempfile.gettempdir()) / "proofops-v04-hardened-demo.sqlite"
    if db.exists():
        db.unlink()
    engine = make_engine(f"sqlite:///{db}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Make sure ACME's renewal is handled")
    advance(repo, run.id)

    gmail = FakeGmailProvider()
    gmail.configure("timeout_after_success", visibility_delay_searches=1)
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"})

    call = dict(
        run_id=run.id,
        logical_key="R2026-C001:initial_outreach",
        customer_id="C001",
        renewal_id="R2026-C001",
        purpose="INITIAL_RENEWAL_OUTREACH",
        to="alice@acme.com",
        subject="Renewal",
        body="Hello Alice — renewal follow-up.",
        policy=policy,
    )
    action, payload = service.prepare_email(**call)
    approval = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=semantic_payload_hash(payload),
        actor="demo-user",
    )

    print("1. Exact email payload prepared and policy-checked")
    print("2. Human approval bound to run + actor + payload hash")
    result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
    print("3. Gmail committed, but provider response was lost")
    print(f"4. First reconciliation state: {result.state}")
    print("5. ProofOps did NOT resend while outcome was uncertain")
    result = effects.reconcile(action.id)
    print(f"6. Second reconciliation state: {result.state}")
    print(f"7. Physical messages: {len(gmail.messages)}")
    print(f"8. Durable evidence rows: {len(repo.evidence_for_run(run.id))}")
    assert result.state == ActionState.CONFIRMED.value
    assert len(gmail.messages) == 1
    print("PASS: delayed visibility -> UNKNOWN -> reconcile -> CONFIRMED, with zero duplicate side effects")


if __name__ == "__main__":
    main()
