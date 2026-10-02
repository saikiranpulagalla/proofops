from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import EmailObservation
from app.ports.protocols import ProviderDataError, ProviderUnavailable


class GmailOutcomeUnknown(ProviderUnavailable):
    """A send may or may not have committed at Gmail; reconciliation is mandatory."""


class GmailSearchUnavailable(ProviderUnavailable):
    """Gmail could not be searched/read to prove an external effect."""


class GmailSendRejected(ProviderDataError):
    """Gmail definitively rejected the requested send before it can be treated as success."""


@dataclass(frozen=True)
class GmailMessage:
    provider_ref: str
    message_id: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    # Provider-observed sender.  RC9 reconciliation requires it for a
    # correlation-reference candidate; legacy deterministic fakes may omit it.
    from_email: str = ""
    thread_id: str | None = None
    label_ids: tuple[str, ...] = ()
    # All recipient-visible rendered body alternatives present in the provider MIME
    # object.  Reconciliation rejects a candidate when alternatives disagree.
    body_variants: tuple[str, ...] = ()


class GmailEffectPort(Protocol):
    def send(self, *, message_id: str, to: str, subject: str = "", body: str, effect_reference: str | None = None) -> str: ...
    def find_by_message_id(self, message_id: str) -> list[GmailMessage]: ...


class GmailBusinessReadPort(Protocol):
    def latest_observation(self, customer_id: str, *, renewal_id: str | None = None) -> EmailObservation: ...
