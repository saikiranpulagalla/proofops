from __future__ import annotations

from app.ports.gmail import GmailSearchUnavailable
from app.ports.protocols import ProviderConflict, ProviderDataError
from app.security.canonicalization import normalize_email


class CRMIdentityBoundGmailProvider:
    """Bind Gmail business evidence to the current trusted CRM identity on every read.

    The underlying Gmail adapter owns transport/reconciliation semantics.  This wrapper
    prevents a long-running process from continuing to interpret mail for an old contact
    after the CRM contact or renewal identity changes.
    """

    def __init__(self, delegate, crm, *, configured_contacts: dict[str, str], allowed_recipients: set[str] | frozenset[str] | None = None):
        self.delegate = delegate
        self.crm = crm
        self.configured_contacts = {
            str(customer_id): normalize_email(email)
            for customer_id, email in configured_contacts.items()
        }
        self.allowed_recipients = frozenset(normalize_email(x) for x in (allowed_recipients or set()) if normalize_email(x))

    def send(self, **kwargs):
        target = normalize_email(str(kwargs.get("to") or ""))
        if self.allowed_recipients and target not in self.allowed_recipients:
            raise ProviderConflict("outbound recipient is not in the qualified live recipient allowlist")
        return self.delegate.send(**kwargs)

    def find_by_message_id(self, message_id: str):
        return self.delegate.find_by_message_id(message_id)

    @property
    def sender_email(self) -> str:
        """Expose the authenticated provider identity used by reconciliation."""
        value = getattr(self.delegate, "sender_email", None)
        if not isinstance(value, str) or not normalize_email(value):
            raise ProviderDataError("wrapped Gmail provider has no authenticated sender identity")
        return normalize_email(value)

    def history_fence(self) -> str:
        """Delegate the mandatory pre-send reconciliation fence explicitly.

        This is deliberately not a transparent ``__getattr__`` fallback: the effect
        executor depends on these methods for consequential-send safety and must fail
        closed if the production composition does not provide them.
        """
        method = getattr(self.delegate, "history_fence", None)
        if not callable(method):
            raise GmailSearchUnavailable("wrapped Gmail provider does not support history fences")
        value = method()
        if not value:
            raise GmailSearchUnavailable("wrapped Gmail provider returned an empty history fence")
        return str(value)

    def find_by_effect_reference(self, *, history_start_id: str, effect_reference: str):
        method = getattr(self.delegate, "find_by_effect_reference", None)
        if not callable(method):
            raise GmailSearchUnavailable("wrapped Gmail provider does not support effect-reference reconciliation")
        return method(history_start_id=history_start_id, effect_reference=effect_reference)

    def latest_observation(self, customer_id: str, *, renewal_id: str | None = None):
        current = self.crm.get_customer(customer_id)
        if current is None:
            raise ProviderDataError(f"CRM customer {customer_id!r} no longer exists")
        configured = self.configured_contacts.get(customer_id)
        if not configured:
            raise ProviderDataError(f"customer {customer_id!r} is not in the live Gmail allowlist")
        current_email = normalize_email(current.contact_email or "")
        if not current_email or current_email != configured:
            raise ProviderConflict("CRM contact changed from the qualified Gmail identity")
        if renewal_id is not None and current.renewal_id != renewal_id:
            raise ProviderConflict("CRM renewal identity changed from the requested Gmail evidence scope")
        return self.delegate.latest_observation(customer_id, renewal_id=renewal_id)
