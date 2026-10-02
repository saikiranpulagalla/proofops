from datetime import datetime, timedelta, timezone

from app.domain.enums import EmailIntent
from app.domain.models import EmailObservation
from app.engine.truth import interpret_email_evidence


def obs(intent, **kwargs):
    base = dict(
        intent=intent,
        source_ref="gmail:msg-1",
        observed_at=datetime.now(timezone.utc),
        sender_verified=True,
    )
    base.update(kwargs)
    return EmailObservation(**base)


def test_stale_acceptance_is_ambiguous():
    result = interpret_email_evidence(
        obs(EmailIntent.ACCEPTED, observed_at=datetime.now(timezone.utc) - timedelta(days=31)),
        max_age_seconds=30 * 24 * 3600,
    )
    assert result.intent == EmailIntent.AMBIGUOUS
    assert not result.usable_for_automation
    assert result.reason == "STALE_EMAIL_EVIDENCE"


def test_verified_do_not_contact_is_fail_closed_even_if_old():
    result = interpret_email_evidence(
        obs(EmailIntent.DO_NOT_CONTACT, observed_at=datetime.now(timezone.utc) - timedelta(days=365)),
        max_age_seconds=30 * 24 * 3600,
    )
    assert result.intent == EmailIntent.DO_NOT_CONTACT
    assert result.usable_for_automation


def test_future_dated_evidence_is_rejected():
    result = interpret_email_evidence(
        obs(EmailIntent.ACCEPTED, observed_at=datetime.now(timezone.utc) + timedelta(days=1)),
        max_age_seconds=30 * 24 * 3600,
    )
    assert result.intent == EmailIntent.AMBIGUOUS
    assert result.reason == "FUTURE_DATED_EVIDENCE"


def test_missing_provenance_fails_closed_for_consequential_claim():
    result = interpret_email_evidence(
        EmailObservation(intent=EmailIntent.ACCEPTED),
        max_age_seconds=30 * 24 * 3600,
    )
    assert result.intent == EmailIntent.AMBIGUOUS
    assert result.reason == "MISSING_EVIDENCE_PROVENANCE"


def test_none_observation_needs_no_claim_provenance():
    result = interpret_email_evidence(EmailObservation(), max_age_seconds=30 * 24 * 3600)
    assert result.intent == EmailIntent.NONE
    assert result.usable_for_automation
