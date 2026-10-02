from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass


class SessionError(PermissionError):
    pass


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


@dataclass(frozen=True)
class SessionClaims:
    actor: str
    csrf: str
    issued_at: int
    expires_at: int
    session_id: str


class SessionSigner:
    def __init__(self, secret: str, *, ttl_seconds: int = 3600, now_fn=time.time):
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("session secret must be at least 32 bytes")
        if ttl_seconds <= 0:
            raise ValueError("session ttl must be positive")
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds
        self.now_fn = now_fn

    def issue(self, actor: str) -> tuple[str, SessionClaims]:
        actor = actor.strip()
        if not actor:
            raise ValueError("actor is required")
        now = int(self.now_fn())
        claims = SessionClaims(
            actor=actor,
            csrf=secrets.token_urlsafe(24),
            issued_at=now,
            expires_at=now + self.ttl_seconds,
            session_id=secrets.token_urlsafe(18),
        )
        payload = json.dumps(claims.__dict__, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = _b64e(payload)
        sig = _b64e(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{sig}", claims

    def verify(self, token: str) -> SessionClaims:
        try:
            encoded, supplied_sig = token.split(".", 1)
        except ValueError as exc:
            raise SessionError("invalid session") from exc
        expected_sig = _b64e(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_sig, expected_sig):
            raise SessionError("invalid session signature")
        try:
            raw = json.loads(_b64d(encoded))
            claims = SessionClaims(**raw)
        except Exception as exc:
            raise SessionError("invalid session payload") from exc
        if claims.expires_at <= int(self.now_fn()):
            raise SessionError("session expired")
        if not claims.actor or not claims.csrf or not claims.session_id:
            raise SessionError("invalid session claims")
        return claims
