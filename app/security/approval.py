from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from app.domain.enums import ApprovalStatus
from app.persistence.repository import ApprovalClaimError, LedgerRepository


class ApprovalError(PermissionError):
    pass


class ApprovalContinuationExpired(ApprovalError):
    pass


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ApprovalService:
    def __init__(self, repo: LedgerRepository, *, now_fn=None, continuation_ttl_seconds: int = 900):
        if continuation_ttl_seconds <= 0:
            raise ValueError("continuation TTL must be positive")
        self.repo = repo
        self.now_fn = now_fn or repo.now
        self.continuation_ttl_seconds = int(continuation_ttl_seconds)

    def _now(self) -> datetime:
        return _aware(self.now_fn())

    def approve(self, *, run_id: int, action_id: int, payload_hash: str, actor: str, ttl_seconds: int = 600, api_request_id: int | None = None):
        if ttl_seconds <= 0:
            raise ValueError("approval TTL must be positive")
        if not actor.strip():
            raise ValueError("actor is required")
        try:
            now = self._now()
            return self.repo.create_or_get_active_approval(
                run_id=run_id,
                action_id=action_id,
                payload_hash=payload_hash,
                actor=actor,
                nonce=secrets.token_urlsafe(24),
                expires_at=now + timedelta(seconds=ttl_seconds),
                now=now,
                api_request_id=api_request_id,
            )
        except ApprovalClaimError as exc:
            raise ApprovalError(str(exc)) from exc

    def revoke(self, approval_id: int, *, api_request_id: int | None = None) -> None:
        try:
            self.repo.revoke_approval(approval_id, api_request_id=api_request_id, now=self._now())
        except ApprovalClaimError as exc:
            raise ApprovalError(str(exc)) from exc

    def verify(self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str) -> None:
        row = self.repo.get_approval(approval_id)
        if row is None:
            raise ApprovalError("approval not found")
        if row.run_id != run_id:
            raise ApprovalError("approval belongs to a different run")
        if row.action_id != action_id:
            raise ApprovalError("approval belongs to a different action")
        if row.actor != actor:
            raise ApprovalError("approval actor mismatch")
        if row.status != ApprovalStatus.ACTIVE.value:
            raise ApprovalError(f"approval is {row.status.lower()}")
        if row.payload_hash != payload_hash:
            raise ApprovalError("approved payload does not match execution payload")
        if _aware(row.expires_at) <= self._now():
            raise ApprovalError("approval expired")


    def verify_continuation(self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str) -> None:
        """Verify an already-claimed authorization for the same logical effect.

        A RETRYABLE effect is not a new business decision: reconciliation has proven the
        previous provider attempt absent, so continuing the *same* payload is allowed under
        the original claimed authorization.  CLAIMED is required; ACTIVE would mean the
        execution boundary was never crossed and CONSUMED means the effect already completed.
        """
        row = self.repo.get_approval(approval_id)
        if row is None:
            raise ApprovalError("approval not found")
        if row.run_id != run_id or row.action_id != action_id:
            raise ApprovalError("approval/action belongs to a different run")
        if row.actor != actor:
            raise ApprovalError("approval actor mismatch")
        if row.payload_hash != payload_hash:
            raise ApprovalError("approved payload does not match continuation payload")
        if row.status != ApprovalStatus.CLAIMED.value:
            raise ApprovalError(f"approval is {row.status.lower()}; continuation requires claimed authorization")
        if row.claimed_at is None:
            raise ApprovalError("claimed approval is missing claimed_at")
        continuation_expires = _aware(row.claimed_at) + timedelta(seconds=self.continuation_ttl_seconds)
        if self._now() >= continuation_expires:
            raise ApprovalContinuationExpired("claimed approval continuation window expired")

    def claim_and_start_attempt(
        self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str,
        provider: str, external_key: str, api_request_id: int | None = None, lease_seconds: int,
    ):
        try:
            return self.repo.claim_approval_and_start_attempt(
                approval_id=approval_id, run_id=run_id, action_id=action_id,
                payload_hash=payload_hash, actor=actor, provider=provider,
                external_key=external_key, api_request_id=api_request_id,
                lease_seconds=lease_seconds, now=self._now(),
            )
        except ApprovalClaimError as exc:
            raise ApprovalError(str(exc)) from exc

    def claim(self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str):
        try:
            return self.repo.claim_approval(
                approval_id=approval_id,
                run_id=run_id,
                action_id=action_id,
                payload_hash=payload_hash,
                actor=actor,
                now=self._now(),
            )
        except ApprovalClaimError as exc:
            raise ApprovalError(str(exc)) from exc
