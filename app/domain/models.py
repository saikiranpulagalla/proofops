from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import ActionState, EmailIntent, RiskLevel, RunState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc) if value is not None else None


class TemporalCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    deadline: datetime | None = None
    evidence_max_age_seconds: int | None = None
    max_future_skew_seconds: int = 300

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value: datetime | None):
        return _require_aware(value, "deadline")

    @field_validator("evidence_max_age_seconds", "max_future_skew_seconds")
    @classmethod
    def validate_non_negative(cls, value: int | None):
        if value is not None and value < 0:
            raise ValueError("temporal values must be non-negative")
        return value


class GoalContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject_customer_id: str | None = None
    subject_renewal_id: str | None = None
    required_conditions: tuple[str, ...] = ()
    forbidden_conditions: tuple[str, ...] = ()
    optional_conditions: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    temporal: TemporalCondition = Field(default_factory=TemporalCondition)


class CustomerRecord(BaseModel):
    customer_id: str
    company: str
    contact_email: str | None = None
    renewal_id: str
    renewal_status: str
    renewal_date: datetime | None = None
    do_not_contact: bool = False
    version: int = Field(default=1, ge=1)

    @field_validator("renewal_date")
    @classmethod
    def validate_renewal_date(cls, value: datetime | None):
        return _require_aware(value, "renewal_date")


class CRMUpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    customer: CustomerRecord
    previous_version: int = Field(ge=1)
    new_version: int = Field(ge=2)
    provider_ref: str
    verified: bool = True

    @model_validator(mode="after")
    def validate_version_increment(self):
        if self.new_version != self.previous_version + 1:
            raise ValueError("CRM update version must increment exactly once")
        if self.customer.version != self.new_version:
            raise ValueError("customer version must match verified new_version")
        if not self.provider_ref.strip():
            raise ValueError("provider_ref is required")
        return self


class EmailObservation(BaseModel):
    intent: EmailIntent = EmailIntent.NONE
    message_id: str | None = None
    source_ref: str | None = None
    observed_at: datetime | None = None
    sender_verified: bool = False
    quoted_only: bool = False
    auto_reply: bool = False
    provenance: str = "provider"

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime | None):
        return _require_aware(value, "observed_at")


class FollowupMeeting(BaseModel):
    event_id: str
    starts_at: datetime
    attendee_emails: tuple[str, ...] = ()
    status: str = "confirmed"
    purpose: str = "RENEWAL_FOLLOWUP"

    @field_validator("starts_at")
    @classmethod
    def validate_starts_at(cls, value: datetime):
        return _require_aware(value, "meeting starts_at")


class BusinessSnapshot(BaseModel):
    customer: CustomerRecord
    email_intent: EmailIntent = EmailIntent.NONE
    recent_email_id: str | None = None
    meeting_exists: bool = False
    observed_at: datetime = Field(default_factory=utcnow)
    conflicts: tuple[str, ...] = ()


class PlannedAction(BaseModel):
    logical_key: str
    action_type: str
    purpose: str
    target: str
    risk: RiskLevel
    payload: dict[str, Any] = Field(default_factory=dict)
    state: ActionState = ActionState.PROPOSED


class RunResult(BaseModel):
    run_state: RunState
    customer_id: str | None = None
    actions: tuple[PlannedAction, ...] = ()
    reasons: tuple[str, ...] = ()


class EvidenceFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_type: str
    source_ref: str
    customer_id: str | None = None
    renewal_id: str | None = None
    observed_at: datetime
    verified_at: datetime = Field(default_factory=utcnow)
    verified: bool = True
    expected_hash: str | None = None
    observed_hash: str | None = None

    @field_validator("observed_at", "verified_at")
    @classmethod
    def validate_dates(cls, value: datetime):
        return _require_aware(value, "evidence timestamp")

    @model_validator(mode="after")
    def require_source(self):
        if not self.source_ref.strip():
            raise ValueError("evidence source_ref is required")
        return self


# Backward-compatible name used by earlier code/tests.
EvidenceRecord = EvidenceFact
