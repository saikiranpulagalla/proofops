"""Independent-process regression for Astra A04.

The test deliberately uses two engines and OS processes against one SQLite file and
counts provider invocations, not merely final database rows.
"""
from __future__ import annotations

import multiprocessing

from app.domain.enums import ActionState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.ports.gmail import GmailMessage
from app.security.approval import ApprovalService
from app.security.policy import EmailPolicyContext


class _ProcessProvider:
    def __init__(self, calls):
        self.calls = calls
        self.messages: dict[str, GmailMessage] = {}

    def history_fence(self) -> str:
        return "100"

    def find_by_effect_reference(self, *, effect_reference: str, **_kwargs):
        marker = "\n\nReference: " + effect_reference
        return [message for message in self.messages.values() if message.body.endswith(marker)]

    def send(self, *, message_id: str, to: str, subject: str, body: str, effect_reference: str | None = None):
        with self.calls.get_lock():
            self.calls.value += 1
            provider_ref = f"provider-{self.calls.value}"
        reference = f"\n\nReference: {effect_reference}" if effect_reference else ""
        self.messages[provider_ref] = GmailMessage(
            provider_ref=provider_ref,
            thread_id=f"thread-{provider_ref}",
            message_id=message_id,
            from_email="self@example.com",
            to=(to,), cc=(), bcc=(), subject=subject, body=body + reference, label_ids=("SENT",),
        )
        return provider_ref

    def find_by_message_id(self, _message_id: str):
        return list(self.messages.values())


def _worker(db_url: str, approval_id: int, run_id: int, action_id: int, calls, barrier, queue):
    repo = LedgerRepository(make_session_factory(make_engine(db_url)))
    provider = _ProcessProvider(calls)
    service = ConsequentialActionService(repo, EffectExecutor(repo, provider), ApprovalService(repo))
    call = dict(
        run_id=run_id, logical_key="R1:initial", customer_id="C001", renewal_id="R1", purpose="INITIAL",
        to="self@example.com", subject="Renewal", body="hello",
        policy=EmailPolicyContext.build(customer_id="C001", verified_recipients={"self@example.com"}),
    )
    barrier.wait(timeout=15)
    try:
        action = service.execute_approved_email(approval_id=approval_id, actor="race", **call)
        queue.put(("state", action.state))
    except Exception as exc:  # losing the atomic claim is an expected safe outcome
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_rc11_sqlite_independent_process_claim_race_issues_at_most_one_send(tmp_path):
    from app.persistence.db import create_schema
    from tests.helpers import advance_run_to_executing
    from app.security.canonicalization import semantic_payload_hash

    db_url = f"sqlite:///{tmp_path/'shared.sqlite'}"
    engine = make_engine(db_url)
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME renewal")
    advance_run_to_executing(repo, run.id)
    setup_provider = _ProcessProvider(multiprocessing.get_context("spawn").Value("i", 0))
    setup_service = ConsequentialActionService(repo, EffectExecutor(repo, setup_provider), ApprovalService(repo))
    call = dict(
        run_id=run.id, logical_key="R1:initial", customer_id="C001", renewal_id="R1", purpose="INITIAL",
        to="self@example.com", subject="Renewal", body="hello",
        policy=EmailPolicyContext.build(customer_id="C001", verified_recipients={"self@example.com"}),
    )
    action, payload = setup_service.prepare_email(**call)
    approval = ApprovalService(repo).approve(run_id=run.id, action_id=action.id, payload_hash=semantic_payload_hash(payload), actor="race")
    engine.dispose()

    ctx = multiprocessing.get_context("spawn")
    calls, barrier, queue = ctx.Value("i", 0), ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(target=_worker, args=(db_url, approval.id, run.id, action.id, calls, barrier, queue))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in workers]
    assert calls.value <= 1, outcomes
    assert any(kind == "state" and state == ActionState.CONFIRMED.value for kind, state in outcomes), outcomes
