from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.enums import EmailIntent
from app.domain.models import EmailObservation


@dataclass(frozen=True)
class InterpretedEmailEvidence:
    intent: EmailIntent
    usable_for_automation: bool
    reason: str | None = None


def interpret_email_evidence(
    observation: EmailObservation,
    *,
    max_age_seconds: int,
    max_future_skew_seconds: int = 300,
    now: datetime | None = None,
) -> InterpretedEmailEvidence:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    # NONE is a successful "no relevant evidence" observation and carries no business claim.
    if observation.intent == EmailIntent.NONE:
        return InterpretedEmailEvidence(EmailIntent.NONE, True)

    if observation.observed_at is None or not observation.source_ref:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "MISSING_EVIDENCE_PROVENANCE")
    observed_at = observation.observed_at.astimezone(timezone.utc)
    future_skew = (observed_at - now).total_seconds()
    if future_skew > max_future_skew_seconds:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "FUTURE_DATED_EVIDENCE")
    age = max(0.0, (now - observed_at).total_seconds())

    if not observation.sender_verified:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "UNVERIFIED_SENDER")
    if observation.quoted_only:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "QUOTED_ONLY_EVIDENCE")
    if observation.auto_reply:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "AUTO_REPLY_EVIDENCE")
    if age > max_age_seconds and observation.intent != EmailIntent.DO_NOT_CONTACT:
        return InterpretedEmailEvidence(EmailIntent.AMBIGUOUS, False, "STALE_EMAIL_EVIDENCE")
    return InterpretedEmailEvidence(observation.intent, True)
