from __future__ import annotations

from dataclasses import dataclass

from app.security.canonicalization import normalize_email


class PolicyViolation(PermissionError):
    pass


@dataclass(frozen=True)
class EmailPolicyContext:
    customer_id: str
    verified_recipients: frozenset[str]
    do_not_contact: bool = False

    @classmethod
    def build(cls, *, customer_id: str, verified_recipients: set[str], do_not_contact: bool = False):
        return cls(
            customer_id=customer_id,
            verified_recipients=frozenset(normalize_email(x) for x in verified_recipients),
            do_not_contact=do_not_contact,
        )


def enforce_email_policy(payload: dict, context: EmailPolicyContext) -> None:
    if payload.get("type") != "SEND_EMAIL":
        raise PolicyViolation("unsupported consequential action")
    if payload.get("customer_id") != context.customer_id:
        raise PolicyViolation("customer mismatch")
    if context.do_not_contact:
        raise PolicyViolation("customer is marked do-not-contact")
    recipients = set(payload.get("to", ())) | set(payload.get("cc", ())) | set(payload.get("bcc", ()))
    if not recipients:
        raise PolicyViolation("no recipient")
    unauthorized = recipients - set(context.verified_recipients)
    if unauthorized:
        raise PolicyViolation(f"unauthorized recipients: {sorted(unauthorized)}")
