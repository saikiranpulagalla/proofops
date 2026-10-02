from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.google_gmail import GoogleGmailProvider
from app.adapters.google_sheets import GoogleSheetsCRM, SheetsCRMConfig
from app.domain.enums import ActionState, ApprovalStatus, RunState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import MaterialStateChanged, RenewalWorkflowV08
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService, ApprovalError
from tests.test_google_gmail import MemoryGmailTransport, raw_message
from tests.test_google_sheets import MemorySheetsTransport, row


NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now
    def __call__(self):
        return self.now
    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class DelayedSentVisibilityTransport(MemoryGmailTransport):
    def __init__(self, *args, hide_sent_searches=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.hide_sent_searches = hide_sent_searches
    def search(self, *, query: str, label_ids=(), max_results: int = 10):
        if query.startswith("rfc822msgid:") and self.hide_sent_searches > 0:
            self.hide_sent_searches -= 1
            self.search_calls.append((query, tuple(label_ids), max_results))
            return []
        return super().search(query=query, label_ids=tuple(label_ids), max_results=max_results)

    def history_message_refs(self, *, start_history_id: str):
        # RC11 reconciliation is history/reference based.  Delayed-visibility
        # tests must delay that authoritative path rather than only the retired
        # RFC Message-ID search path.
        if self.hide_sent_searches > 0:
            self.hide_sent_searches -= 1
            return []
        return super().history_message_refs(start_history_id=start_history_id)


def make_workflow(tmp_path, *, sheets_transport=None, gmail_transport=None, clock=None, grace=0, min_absence=2):
    clock = clock or Clock()
    sheets_transport = sheets_transport or MemorySheetsTransport(all_rows=[row()])
    gmail_transport = gmail_transport or MemoryGmailTransport()
    crm = GoogleSheetsCRM(sheets_transport, SheetsCRMConfig(spreadsheet_id="sheet-123"))
    gmail = GoogleGmailProvider(
        gmail_transport,
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"},
        renewal_threads={"R2026-C001": ("renewal-thread",)},
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'v08.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    approvals = ApprovalService(repo)
    effects = EffectExecutor(
        repo,
        gmail,
        reconciliation_grace_seconds=grace,
        min_absence_checks=min_absence,
        execution_lease_seconds=1,
        now_fn=clock,
    )
    consequential = ConsequentialActionService(repo, effects, approvals)
    planning = PlanningService(repo, BoundedRulePlanner())
    workflow = RenewalWorkflowV08(
        repo=repo,
        crm=crm,
        gmail_read=gmail,
        planning=planning,
        consequential=consequential,
        approvals=approvals,
        coordinator=RunCoordinator(repo),
        now_fn=clock,
    )
    return workflow, repo, sheets_transport, gmail_transport, clock


def test_happy_path_full_composition_reaches_verified_completion(tmp_path):
    workflow, repo, _sheets, gmail_t, _clock = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME", deadline=NOW + timedelta(days=7))
    assert cp.run_state == RunState.WAITING_APPROVAL
    assert cp.action_state == ActionState.APPROVAL_REQUIRED
    assert cp.target == "alice@acme.com"

    approved = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    assert approved.action_state == ActionState.APPROVED
    done = workflow.execute(
        run_id=cp.run_id, action_id=cp.action_id, approval_id=approved.approval_id, actor="judge"
    )
    assert done.run_state == RunState.COMPLETED_VERIFIED
    assert done.action_state == ActionState.CONFIRMED
    assert gmail_t.sent_calls == 1
    assert repo.get_approval(approved.approval_id).status == ApprovalStatus.CONSUMED.value
    types = {e.evidence_type for e in repo.evidence_for_run(cp.run_id)}
    assert {"crm_snapshot", "gmail_observation", "gmail_sent_message"}.issubset(types)


def test_business_evidence_is_persisted_before_any_effect(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert repo.latest_attempt(cp.action_id) is None
    evidence = repo.evidence_for_run(cp.run_id)
    assert {e.evidence_type for e in evidence} == {"crm_snapshot", "gmail_observation"}
    assert all(e.customer_id == "C001" and e.renewal_id == "R2026-C001" for e in evidence)


def test_run_context_is_durable_and_identity_bound(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    run = repo.get_run(cp.run_id)
    assert run.workflow_type == "RENEWAL_OUTREACH_V081"
    assert run.subject_customer_id == "C001"
    assert run.subject_renewal_id == "R2026-C001"
    assert run.goal_contract_json and run.snapshot_json
    with pytest.raises(Exception, match="immutable"):
        repo.bind_run_context(
            run_id=cp.run_id,
            workflow_type="OTHER",
            subject_customer_id="C999",
            subject_renewal_id="R999",
            goal_contract_json=run.goal_contract_json,
            snapshot_json=run.snapshot_json,
        )


def test_material_crm_version_change_after_approval_blocks_before_send(tmp_path):
    t = MemorySheetsTransport(all_rows=[row()])
    workflow, repo, sheets, gmail_t, _ = make_workflow(tmp_path, sheets_transport=t)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    sheets.all_rows[0] = row(version=2)
    with pytest.raises(MaterialStateChanged, match="MATERIAL_EVIDENCE_CHANGED"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0
    assert repo.get_run(cp.run_id).state == RunState.BLOCKED_NEEDS_HUMAN.value
    assert repo.get_approval(ap.approval_id).status == ApprovalStatus.REVOKED.value


def test_new_do_not_contact_email_after_approval_blocks_before_send(tmp_path):
    gmail_t = MemoryGmailTransport()
    workflow, repo, _sheets, _, _ = make_workflow(tmp_path, gmail_transport=gmail_t)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    ms = int(NOW.timestamp() * 1000)
    gmail_t.resources["dnc"] = raw_message(
        provider_ref="dnc", sender="alice@acme.com", body="Please do not contact us again", internal_ms=ms,
        thread_id="renewal-thread",
    )
    with pytest.raises(MaterialStateChanged, match="MATERIAL_EVIDENCE_CHANGED"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0
    assert repo.get_run(cp.run_id).state == RunState.BLOCKED_NEEDS_HUMAN.value


def test_expired_approval_never_sends(tmp_path):
    workflow, repo, _sheets, gmail_t, clock = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge", ttl_seconds=1)
    # ApprovalService uses wall-clock time, so expire the persisted approval explicitly for deterministic test.
    from sqlalchemy import update
    from app.persistence.models import ApprovalORM
    with repo.session_factory() as s:
        s.execute(update(ApprovalORM).where(ApprovalORM.id == ap.approval_id).values(expires_at=NOW - timedelta(seconds=1)))
        s.commit()
    with pytest.raises(ApprovalError, match="expired"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0


def test_timeout_after_success_with_delayed_visibility_never_blindly_resends(tmp_path):
    gmail_t = DelayedSentVisibilityTransport(send_mode="unknown_after", hide_sent_searches=1)
    workflow, repo, _sheets, _, clock = make_workflow(
        tmp_path, gmail_transport=gmail_t, clock=Clock(), grace=0, min_absence=2
    )
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    first = workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert first.run_state == RunState.UNKNOWN
    assert first.action_state == ActionState.UNKNOWN
    assert gmail_t.sent_calls == 1
    resumed = workflow.resume(run_id=cp.run_id)
    assert resumed.run_state == RunState.COMPLETED_VERIFIED
    assert resumed.action_state == ActionState.CONFIRMED
    assert gmail_t.sent_calls == 1


def test_process_restart_can_resume_unknown_from_durable_context(tmp_path):
    gmail_t = DelayedSentVisibilityTransport(send_mode="unknown_after", hide_sent_searches=1)
    workflow, repo, sheets, _, clock = make_workflow(tmp_path, gmail_transport=gmail_t, grace=0, min_absence=2)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    first = workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert first.run_state == RunState.UNKNOWN

    # New service objects, same durable DB and providers: no in-memory workflow state is required.
    gmail = GoogleGmailProvider(
        gmail_t,
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"},
        renewal_threads={"R2026-C001": ("renewal-thread",)},
    )
    crm = GoogleSheetsCRM(sheets, SheetsCRMConfig(spreadsheet_id="sheet-123"))
    approvals = ApprovalService(repo)
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2, execution_lease_seconds=1, now_fn=clock)
    workflow2 = RenewalWorkflowV08(
        repo=repo,
        crm=crm,
        gmail_read=gmail,
        planning=PlanningService(repo, BoundedRulePlanner()),
        consequential=ConsequentialActionService(repo, effects, approvals),
        approvals=approvals,
        coordinator=RunCoordinator(repo),
        now_fn=clock,
    )
    resumed = workflow2.resume(run_id=cp.run_id)
    assert resumed.run_state == RunState.COMPLETED_VERIFIED
    assert gmail_t.sent_calls == 1


def test_planner_and_effect_chain_are_auditable_in_one_run(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert len(repo.plans_for_run(cp.run_id)) == 1
    events = repo.audit_events_for_run(cp.run_id)
    names = {e.event_type for e in events}
    assert {"CONTEXT_BOUND", "PLAN_VALIDATED", "APPROVED", "CLAIMED", "CONSUMED", "VERIFIED"}.issubset(names)


def test_terminal_already_renewed_path_has_no_side_effect(tmp_path):
    sheets = MemorySheetsTransport(all_rows=[row(status="renewed")])
    workflow, repo, _, gmail_t, _ = make_workflow(tmp_path, sheets_transport=sheets)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert cp.run_state == RunState.COMPLETED_NO_ACTION_REQUIRED
    assert repo.actions_for_run(cp.run_id) == []
    assert gmail_t.sent_calls == 0


def test_ambiguous_customer_blocks_before_planning_or_effect(tmp_path):
    sheets = MemorySheetsTransport(all_rows=[row(customer_id="C001", renewal_id="R1"), row(customer_id="C002", renewal_id="R2")])
    workflow, repo, _, gmail_t, _ = make_workflow(tmp_path, sheets_transport=sheets)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert cp.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert repo.plans_for_run(cp.run_id) == []
    assert gmail_t.sent_calls == 0


def test_outside_outreach_window_is_deferred_without_side_effect(tmp_path):
    sheets = MemorySheetsTransport(all_rows=[row(renewal_date="2027-06-01T17:00:00+00:00")])
    workflow, repo, _, gmail_t, _ = make_workflow(tmp_path, sheets_transport=sheets)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    assert cp.run_state == RunState.DEFERRED
    assert len(repo.plans_for_run(cp.run_id)) == 1
    assert gmail_t.sent_calls == 0


def test_deadline_expiry_before_effect_blocks_send_instead_of_sending_then_failing(tmp_path):
    clock = Clock(NOW)
    workflow, repo, _sheets, gmail_t, _ = make_workflow(tmp_path, clock=clock)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME", deadline=NOW + timedelta(seconds=2))
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    clock.advance(seconds=5)
    with pytest.raises(MaterialStateChanged, match="GOAL_DEADLINE_EXPIRED_BEFORE_EFFECT"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0
    assert repo.get_run(cp.run_id).state == RunState.FAILED.value


def test_run_scoped_evidence_cannot_be_rebound_to_other_identity(tmp_path):
    workflow, repo, *_ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    crm_source = next(e.source_ref for e in repo.evidence_for_run(cp.run_id) if e.evidence_type == "crm_snapshot")
    with pytest.raises(ValueError, match="identity drift"):
        repo.record_evidence(
            run_id=cp.run_id,
            customer_id="C999",
            renewal_id="R999",
            evidence_type="crm_snapshot",
            source_ref=crm_source,
            expected=None,
            observed="x",
            observed_at=NOW,
            verified=True,
        )


def test_wrong_actor_cannot_execute_composed_workflow(tmp_path):
    workflow, repo, _sheets, gmail_t, _ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    with pytest.raises(ApprovalError, match="actor mismatch"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="attacker")
    assert gmail_t.sent_calls == 0


def test_second_run_for_same_confirmed_renewal_defers_without_duplicate_send(tmp_path):
    workflow, repo, _sheets, gmail_t, _ = make_workflow(tmp_path)
    first = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=first.run_id, action_id=first.action_id, actor="judge")
    done = workflow.execute(run_id=first.run_id, action_id=first.action_id, approval_id=ap.approval_id, actor="judge")
    assert done.run_state == RunState.COMPLETED_VERIFIED
    second = workflow.prepare(goal_text="Handle ACME renewal again", company="ACME")
    assert second.run_state == RunState.DEFERRED
    assert gmail_t.sent_calls == 1
    assert repo.actions_for_run(second.run_id) == []
    assert "prior_confirmed_outreach" in {e.evidence_type for e in repo.evidence_for_run(second.run_id)}


def test_second_run_blocks_when_existing_logical_action_is_unresolved(tmp_path):
    gmail_t = DelayedSentVisibilityTransport(send_mode="unknown_after", hide_sent_searches=5)
    workflow, repo, _sheets, _, _ = make_workflow(tmp_path, gmail_transport=gmail_t, grace=60, min_absence=2)
    first = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=first.run_id, action_id=first.action_id, actor="judge")
    unknown = workflow.execute(run_id=first.run_id, action_id=first.action_id, approval_id=ap.approval_id, actor="judge")
    assert unknown.run_state == RunState.UNKNOWN
    second = workflow.prepare(goal_text="Handle ACME renewal again", company="ACME")
    assert second.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert second.reasons[0].startswith("BUSINESS_OPERATION_IN_PROGRESS_RUN_")
    assert gmail_t.sent_calls == 1


def test_repeated_execute_after_completion_is_idempotent(tmp_path):
    workflow, _repo, _sheets, gmail_t, _ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    first = workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    second = workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert first.run_state == second.run_state == RunState.COMPLETED_VERIFIED
    assert gmail_t.sent_calls == 1


def test_past_deadline_is_rejected_before_provider_reads_or_planning(tmp_path):
    workflow, repo, _sheets, gmail_t, _ = make_workflow(tmp_path)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME", deadline=NOW - timedelta(seconds=1))
    assert cp.run_state == RunState.FAILED
    assert cp.reasons == ("GOAL_DEADLINE_ALREADY_PASSED",)
    assert repo.plans_for_run(cp.run_id) == []
    assert gmail_t.sent_calls == 0


def test_contact_change_after_approval_invalidates_exact_payload(tmp_path):
    sheets = MemorySheetsTransport(all_rows=[row()])
    workflow, repo, _, gmail_t, _ = make_workflow(tmp_path, sheets_transport=sheets)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    sheets.all_rows[0] = row(contact="new-contact@acme.com", version=2)
    with pytest.raises(MaterialStateChanged, match="MATERIAL_EVIDENCE_CHANGED"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0
    assert repo.get_approval(ap.approval_id).status == ApprovalStatus.REVOKED.value


def test_customer_acceptance_arriving_after_approval_prevents_outreach(tmp_path):
    gmail_t = MemoryGmailTransport()
    workflow, repo, _sheets, _, _ = make_workflow(tmp_path, gmail_transport=gmail_t)
    cp = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    ap = workflow.approve(run_id=cp.run_id, action_id=cp.action_id, actor="judge")
    gmail_t.resources["accepted"] = raw_message(
        provider_ref="accepted", sender="alice@acme.com", body="We accept the renewal", internal_ms=int(NOW.timestamp()*1000),
        thread_id="renewal-thread",
    )
    with pytest.raises(MaterialStateChanged, match="MATERIAL_EVIDENCE_CHANGED"):
        workflow.execute(run_id=cp.run_id, action_id=cp.action_id, approval_id=ap.approval_id, actor="judge")
    assert gmail_t.sent_calls == 0
    assert repo.get_run(cp.run_id).state == RunState.BLOCKED_NEEDS_HUMAN.value


def test_run_scoped_evidence_same_source_isolated_between_runs(tmp_path):
    workflow, repo, _sheets, _gmail, _ = make_workflow(tmp_path)
    first = workflow.prepare(goal_text="Handle ACME renewal", company="ACME")
    # A second run is blocked by the unresolved logical action, but CRM evidence may use the
    # same logical source_ref without violating uniqueness because uniqueness is run-scoped.
    second = workflow.prepare(goal_text="Handle ACME renewal second", company="ACME")
    assert second.run_state == RunState.BLOCKED_NEEDS_HUMAN
    refs1 = {(e.evidence_type, e.source_ref) for e in repo.evidence_for_run(first.run_id)}
    refs2 = {(e.evidence_type, e.source_ref) for e in repo.evidence_for_run(second.run_id)}
    # Existing-action guard happens before evidence recording on the second run; this test
    # asserts that it does not fabricate evidence to justify the block.
    assert refs1
    assert refs2 == set()
