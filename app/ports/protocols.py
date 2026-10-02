from datetime import datetime
from typing import Protocol

from app.domain.models import CRMUpdateResult, CustomerRecord, EmailObservation, FollowupMeeting


class ProviderError(RuntimeError):
    """Base class for provider-facing failures that must not be confused with absence."""


class ProviderUnavailable(ProviderError):
    """Transient/availability failure (network, 429, 5xx, etc.)."""


class ProviderUnauthorized(ProviderUnavailable):
    """Authentication/authorization failure (401/403); workflow access is unavailable."""


class ProviderConflict(ProviderError):
    """Provider state changed concurrently or identity is ambiguous."""


class ProviderDataError(ProviderError):
    """Provider returned malformed or schema-invalid business data."""


class CRMPort(Protocol):
    def find_customers(self, company: str) -> list[CustomerRecord]: ...
    def get_customer(self, customer_id: str) -> CustomerRecord | None: ...


class CRMWritePort(Protocol):
    def update_renewal_status(
        self,
        *,
        customer_id: str,
        renewal_id: str,
        expected_version: int,
        new_status: str,
    ) -> CRMUpdateResult: ...


class GmailReadPort(Protocol):
    def latest_observation(self, customer_id: str, *, renewal_id: str | None = None) -> EmailObservation: ...


class CalendarReadPort(Protocol):
    def get_followup(self, customer_id: str) -> FollowupMeeting | None: ...
