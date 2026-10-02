from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Iterable


def _text(value: str | None) -> str:
    value = value or ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", value)


def normalize_email(value: str) -> str:
    return _text(value).strip().casefold()


def normalize_recipients(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({normalize_email(v) for v in values if v and v.strip()}))


def canonical_email_payload(
    *,
    customer_id: str,
    renewal_id: str,
    purpose: str,
    to: Iterable[str],
    cc: Iterable[str] = (),
    bcc: Iterable[str] = (),
    subject: str,
    body_text: str,
    thread_id: str | None = None,
    attachments: Iterable[str] = (),
) -> dict:
    return {
        "type": "SEND_EMAIL",
        "customer_id": _text(customer_id).strip(),
        "renewal_id": _text(renewal_id).strip(),
        "purpose": _text(purpose).strip(),
        "to": normalize_recipients(to),
        "cc": normalize_recipients(cc),
        "bcc": normalize_recipients(bcc),
        "subject": _text(subject),
        "body_text": _text(body_text),
        "thread_id": _text(thread_id).strip() or None,
        "attachments": tuple(sorted(_text(a).strip() for a in attachments if a and a.strip())),
    }


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def semantic_payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
