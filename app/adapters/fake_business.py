from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.domain.enums import EmailIntent
from app.domain.models import CRMUpdateResult, CustomerRecord, EmailObservation, FollowupMeeting, utcnow
from app.ports.protocols import ProviderConflict, ProviderUnavailable


@dataclass
class FakeCRM:
    customers: list[CustomerRecord] = field(default_factory=list)
    unavailable: bool = False

    def find_customers(self, company: str) -> list[CustomerRecord]:
        if self.unavailable:
            raise ProviderUnavailable("CRM unavailable")
        q = company.strip().casefold()
        return [c for c in self.customers if c.company.strip().casefold() == q]

    def get_customer(self, customer_id: str) -> CustomerRecord | None:
        if self.unavailable:
            raise ProviderUnavailable("CRM unavailable")
        return next((c for c in self.customers if c.customer_id == customer_id), None)

    def update_renewal_status(
        self, *, customer_id: str, renewal_id: str, expected_version: int, new_status: str
    ) -> CRMUpdateResult:
        if self.unavailable:
            raise ProviderUnavailable("CRM unavailable")
        matches = [
            (idx, c) for idx, c in enumerate(self.customers)
            if c.customer_id == customer_id and c.renewal_id == renewal_id
        ]
        if len(matches) != 1:
            raise ProviderConflict("CRM renewal identity is missing or ambiguous")
        idx, current = matches[0]
        if current.version != expected_version:
            raise ProviderConflict(
                f"stale CRM version: expected {expected_version}, found {current.version}"
            )
        updated = current.model_copy(
            update={"renewal_status": new_status, "version": expected_version + 1}
        )
        self.customers[idx] = updated
        return CRMUpdateResult(
            customer=updated,
            previous_version=expected_version,
            new_version=updated.version,
            provider_ref=f"fake:{renewal_id}",
        )


@dataclass
class FakeGmailRead:
    observations: dict[object, EmailObservation | tuple[EmailIntent, str | None]] = field(default_factory=dict)
    unavailable: bool = False

    def latest_observation(self, customer_id: str, *, renewal_id: str | None = None) -> EmailObservation:
        if self.unavailable:
            raise ProviderUnavailable("Gmail read unavailable")
        value = self.observations.get((customer_id, renewal_id), self.observations.get(customer_id))
        if value is None:
            return EmailObservation(intent=EmailIntent.NONE)
        if isinstance(value, EmailObservation):
            return value
        intent, message_id = value
        return EmailObservation(
            intent=intent,
            message_id=message_id,
            source_ref=message_id or f"fake:{customer_id}",
            observed_at=utcnow(),
            sender_verified=True,
        )


@dataclass
class FakeCalendarRead:
    meetings: set[str] | dict[str, FollowupMeeting] = field(default_factory=set)
    unavailable: bool = False

    def get_followup(self, customer_id: str) -> FollowupMeeting | None:
        if self.unavailable:
            raise ProviderUnavailable("Calendar unavailable")
        if isinstance(self.meetings, dict):
            return self.meetings.get(customer_id)
        if customer_id in self.meetings:
            return FollowupMeeting(
                event_id=f"event-{customer_id}",
                starts_at=utcnow() + timedelta(days=1),
                attendee_emails=("customer@example.com",),
            )
        return None
