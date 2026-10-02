from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.enums import EmailIntent, RenewalStatus, RiskLevel, RunState
from app.domain.models import BusinessSnapshot, PlannedAction, RunResult
from app.engine.truth import interpret_email_evidence
from app.ports.protocols import CRMPort, GmailReadPort, CalendarReadPort, ProviderUnavailable


STATUS_ALIASES: dict[str, RenewalStatus] = {
    "pending": RenewalStatus.PENDING,
    "open": RenewalStatus.PENDING,
    "renewed": RenewalStatus.RENEWED,
    "done": RenewalStatus.RENEWED,
    "closed_won": RenewalStatus.RENEWED,
    "accepted": RenewalStatus.RENEWED,
    "declined": RenewalStatus.DECLINED,
    "closed_lost": RenewalStatus.DECLINED,
    "lost": RenewalStatus.DECLINED,
    "non_renewal": RenewalStatus.DECLINED,
    "canceled": RenewalStatus.CANCELED,
    "cancelled": RenewalStatus.CANCELED,
    "terminated": RenewalStatus.CANCELED,
    "expired": RenewalStatus.EXPIRED,
    "deferred": RenewalStatus.DEFERRED,
    "on_hold": RenewalStatus.DEFERRED,
}


def normalize_renewal_status(raw: str) -> RenewalStatus:
    return STATUS_ALIASES.get(raw.strip().casefold(), RenewalStatus.UNKNOWN)


class DeterministicRenewalEngine:
    def __init__(
        self,
        crm: CRMPort,
        gmail: GmailReadPort,
        calendar: CalendarReadPort,
        *,
        max_email_age_seconds: int = 30 * 24 * 3600,
        outreach_window_days: int = 45,
        now_fn=lambda: datetime.now(timezone.utc),
    ):
        self.crm = crm
        self.gmail = gmail
        self.calendar = calendar
        self.max_email_age_seconds = max_email_age_seconds
        self.outreach_window_days = outreach_window_days
        self.now_fn = now_fn

    def run(self, company: str) -> RunResult:
        now = self.now_fn().astimezone(timezone.utc)
        try:
            candidates = self.crm.find_customers(company)
        except ProviderUnavailable:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, reasons=("CRM_UNAVAILABLE",))
        if not candidates:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, reasons=("CUSTOMER_NOT_FOUND",))
        if len(candidates) != 1:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, reasons=("CUSTOMER_AMBIGUOUS",))

        customer = candidates[0]
        status = normalize_renewal_status(customer.renewal_status)
        if status == RenewalStatus.UNKNOWN:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("UNKNOWN_RENEWAL_STATUS",))
        if status in {RenewalStatus.RENEWED, RenewalStatus.DECLINED, RenewalStatus.CANCELED, RenewalStatus.EXPIRED}:
            return RunResult(run_state=RunState.COMPLETED_NO_ACTION_REQUIRED, customer_id=customer.customer_id, reasons=(f"TERMINAL_{status.value}",))
        if status == RenewalStatus.DEFERRED:
            return RunResult(run_state=RunState.DEFERRED, customer_id=customer.customer_id, reasons=("CRM_DEFERRED",))

        if customer.renewal_date is None:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("MISSING_RENEWAL_DATE",))
        renewal_date = customer.renewal_date.astimezone(timezone.utc)
        if renewal_date < now:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("RENEWAL_OVERDUE",))
        if renewal_date - now > timedelta(days=self.outreach_window_days):
            return RunResult(run_state=RunState.DEFERRED, customer_id=customer.customer_id, reasons=("OUTSIDE_OUTREACH_WINDOW",))

        try:
            observation = self.gmail.latest_observation(
                customer.customer_id,
                renewal_id=customer.renewal_id,
            )
        except ProviderUnavailable:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("GMAIL_UNAVAILABLE",))
        interpreted = interpret_email_evidence(observation, max_age_seconds=self.max_email_age_seconds, now=now)
        intent = interpreted.intent
        email_id = observation.message_id

        try:
            meeting = self.calendar.get_followup(customer.customer_id)
        except ProviderUnavailable:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("CALENDAR_UNAVAILABLE",))
        meeting_exists = bool(
            meeting
            and meeting.status.casefold() not in {"canceled", "cancelled"}
            and meeting.purpose == "RENEWAL_FOLLOWUP"
            and now <= meeting.starts_at <= renewal_date
        )

        conflicts: list[str] = []
        if interpreted.reason:
            conflicts.append(interpreted.reason)
        if status == RenewalStatus.PENDING and intent == EmailIntent.ACCEPTED:
            conflicts.append("CRM_PENDING_EMAIL_ACCEPTED")

        snapshot = BusinessSnapshot(
            customer=customer,
            email_intent=intent,
            recent_email_id=email_id,
            meeting_exists=meeting_exists,
            conflicts=tuple(conflicts),
        )

        # A contact prohibition is not the same as business completion.
        if customer.do_not_contact or intent == EmailIntent.DO_NOT_CONTACT:
            return RunResult(
                run_state=RunState.BLOCKED_NEEDS_HUMAN,
                customer_id=customer.customer_id,
                reasons=("DO_NOT_CONTACT",),
            )

        if not interpreted.usable_for_automation or intent == EmailIntent.AMBIGUOUS:
            return RunResult(
                run_state=RunState.BLOCKED_NEEDS_HUMAN,
                customer_id=customer.customer_id,
                reasons=tuple(snapshot.conflicts) or ("AMBIGUOUS_EMAIL_EVIDENCE",),
            )

        if intent == EmailIntent.ACCEPTED:
            return RunResult(
                run_state=RunState.BLOCKED_NEEDS_HUMAN,
                customer_id=customer.customer_id,
                reasons=tuple(snapshot.conflicts) or ("CUSTOMER_ACCEPTED_RENEWAL",),
            )
        if intent == EmailIntent.DECLINED:
            return RunResult(run_state=RunState.COMPLETED_NO_ACTION_REQUIRED, customer_id=customer.customer_id, reasons=("CUSTOMER_DECLINED",))
        if intent == EmailIntent.DELAY:
            return RunResult(run_state=RunState.DEFERRED, customer_id=customer.customer_id, reasons=("DELAY",))
        if meeting_exists:
            return RunResult(run_state=RunState.COMPLETED_NO_ACTION_REQUIRED, customer_id=customer.customer_id, reasons=("FOLLOWUP_ALREADY_SCHEDULED",))
        if not customer.contact_email:
            return RunResult(run_state=RunState.BLOCKED_NEEDS_HUMAN, customer_id=customer.customer_id, reasons=("MISSING_CONTACT",))

        action = PlannedAction(
            logical_key=f"{customer.renewal_id}:initial_outreach",
            action_type="SEND_EMAIL",
            purpose="INITIAL_RENEWAL_OUTREACH",
            target=customer.contact_email,
            risk=RiskLevel.EXTERNAL_WRITE,
            payload={"customer_id": customer.customer_id, "to": customer.contact_email},
        )
        return RunResult(run_state=RunState.EXECUTING, customer_id=customer.customer_id, actions=(action,))
