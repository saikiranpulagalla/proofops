from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.fake_business import FakeCRM, FakeCalendarRead, FakeGmailRead
from app.domain.enums import EmailIntent, RunState
from app.domain.models import CustomerRecord, EmailObservation, FollowupMeeting
from app.engine.deterministic import DeterministicRenewalEngine

NOW = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)


def c(id="C001", company="ACME", email="alice@acme.com", status="pending", dnc=False, renewal_date=None):
    return CustomerRecord(
        customer_id=id,
        company=company,
        contact_email=email,
        renewal_id=f"R2026-{id}",
        renewal_status=status,
        renewal_date=renewal_date or (NOW + timedelta(days=10)),
        do_not_contact=dnc,
    )


def engine(customers, intents=None, meetings=None, *, crm_unavailable=False, gmail_unavailable=False, calendar_unavailable=False):
    return DeterministicRenewalEngine(
        FakeCRM(customers, unavailable=crm_unavailable),
        FakeGmailRead(intents or {}, unavailable=gmail_unavailable),
        FakeCalendarRead(meetings or set(), unavailable=calendar_unavailable),
        now_fn=lambda: NOW,
    )


def observed(intent, **kwargs):
    base = dict(intent=intent, message_id="e1", source_ref="gmail:e1", observed_at=NOW, sender_verified=True)
    base.update(kwargs)
    return EmailObservation(**base)


def test_missing_customer_blocks():
    assert engine([]).run("ACME").run_state == RunState.BLOCKED_NEEDS_HUMAN


def test_duplicate_customer_blocks():
    result = engine([c("C1"), c("C2")]).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "CUSTOMER_AMBIGUOUS" in result.reasons


def test_already_renewed_no_action():
    result = engine([c(status="renewed")]).run("ACME")
    assert result.run_state == RunState.COMPLETED_NO_ACTION_REQUIRED
    assert result.actions == ()


def test_closed_lost_is_terminal_not_outreach():
    result = engine([c(status="closed_lost")]).run("ACME")
    assert result.run_state == RunState.COMPLETED_NO_ACTION_REQUIRED
    assert result.actions == ()


def test_unknown_status_fails_closed():
    result = engine([c(status="mystery_status")]).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "UNKNOWN_RENEWAL_STATUS" in result.reasons


def test_do_not_contact_blocks_business_completion_instead_of_claiming_done():
    result = engine([c(dnc=True)]).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert result.actions == ()


def test_email_acceptance_creates_conflict_instead_of_silent_crm_mutation():
    result = engine([c()], {"C001": observed(EmailIntent.ACCEPTED)}).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert result.actions == ()
    assert "CRM_PENDING_EMAIL_ACCEPTED" in result.reasons


def test_existing_relevant_meeting_prevents_duplicate_outreach():
    meeting = FollowupMeeting(
        event_id="event-1",
        starts_at=NOW + timedelta(days=2),
        attendee_emails=("alice@acme.com",),
        purpose="RENEWAL_FOLLOWUP",
    )
    result = engine([c()], meetings={"C001": meeting}).run("ACME")
    assert result.run_state == RunState.COMPLETED_NO_ACTION_REQUIRED
    assert result.actions == ()


def test_irrelevant_late_meeting_does_not_suppress_required_outreach():
    meeting = FollowupMeeting(
        event_id="event-1",
        starts_at=NOW + timedelta(days=30),
        attendee_emails=("alice@acme.com",),
        purpose="RENEWAL_FOLLOWUP",
    )
    result = engine([c(renewal_date=NOW + timedelta(days=10))], meetings={"C001": meeting}).run("ACME")
    assert result.run_state == RunState.EXECUTING


def test_missing_contact_blocks():
    assert engine([c(email=None)]).run("ACME").run_state == RunState.BLOCKED_NEEDS_HUMAN


def test_pending_without_evidence_proposes_single_external_action():
    result = engine([c()]).run("ACME")
    assert result.run_state == RunState.EXECUTING
    assert len(result.actions) == 1
    assert result.actions[0].action_type == "SEND_EMAIL"
    assert result.actions[0].target == "alice@acme.com"


@pytest.mark.parametrize("intent,expected", [
    (EmailIntent.DECLINED, RunState.COMPLETED_NO_ACTION_REQUIRED),
    (EmailIntent.DELAY, RunState.DEFERRED),
    (EmailIntent.AMBIGUOUS, RunState.BLOCKED_NEEDS_HUMAN),
])
def test_intent_edge_cases(intent, expected):
    assert engine([c()], {"C001": observed(intent)}).run("ACME").run_state == expected


def test_auto_reply_cannot_establish_acceptance():
    result = engine([c()], {"C001": observed(EmailIntent.ACCEPTED, auto_reply=True)}).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "AUTO_REPLY_EVIDENCE" in result.reasons


def test_quoted_only_acceptance_is_not_business_truth():
    result = engine([c()], {"C001": observed(EmailIntent.ACCEPTED, quoted_only=True)}).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "QUOTED_ONLY_EVIDENCE" in result.reasons


def test_unverified_sender_cannot_drive_action():
    result = engine([c()], {"C001": observed(EmailIntent.ACCEPTED, sender_verified=False)}).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "UNVERIFIED_SENDER" in result.reasons


def test_missing_renewal_date_blocks():
    customer = c()
    customer = customer.model_copy(update={"renewal_date": None})
    result = engine([customer]).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "MISSING_RENEWAL_DATE" in result.reasons


def test_far_future_renewal_is_deferred():
    result = engine([c(renewal_date=NOW + timedelta(days=365))]).run("ACME")
    assert result.run_state == RunState.DEFERRED
    assert "OUTSIDE_OUTREACH_WINDOW" in result.reasons


def test_overdue_renewal_requires_human_resolution():
    result = engine([c(renewal_date=NOW - timedelta(days=1))]).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "RENEWAL_OVERDUE" in result.reasons


def test_tool_outage_is_not_treated_as_absence():
    result = engine([c()], gmail_unavailable=True).run("ACME")
    assert result.run_state == RunState.BLOCKED_NEEDS_HUMAN
    assert "GMAIL_UNAVAILABLE" in result.reasons
