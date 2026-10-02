from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunORM(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_text: Mapped[str] = mapped_column(Text)
    initial_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(64), default="CREATED", nullable=False)
    workflow_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_renewal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    goal_contract_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class BusinessOperationORM(Base):
    """Exclusive ownership of one business operation across concurrent runs.

    This is intentionally separate from ``ActionORM``.  Actions are created only after
    planning/policy validation, while operation ownership must be established earlier so
    two concurrent goal submissions cannot both progress to planning and race on the
    eventual logical action key.
    """

    __tablename__ = "business_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    owner_run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionORM(Base):
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(320), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    effects: Mapped[list["EffectORM"]] = relationship(back_populates="action", cascade="all, delete-orphan")


class EffectORM(Base):
    __tablename__ = "effects"
    __table_args__ = (UniqueConstraint("action_id", "attempt_no", name="uq_effect_attempt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"), nullable=True)
    # Provenance of the HTTP mutation which crossed the authorization/effect boundary.
    # Later reconciliation requests deliberately do not replace this origin.
    api_request_id: Mapped[int | None] = mapped_column(ForeignKey("api_requests.id"), nullable=True)
    external_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # RC9: durable pre-send provider fence and opaque content marker.  Historical
    # attempts remain nullable because their causal provider state is unknowable.
    provider_history_start_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_effect_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_rfc_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconcile_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_unknown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    action: Mapped[ActionORM] = relationship(back_populates="effects")


class ApprovalORM(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    api_request_id: Mapped[int | None] = mapped_column(ForeignKey("api_requests.id"), nullable=True, unique=True)
    # Exact HTTP request that revoked this approval, when revocation was API-driven.
    rejected_api_request_id: Mapped[int | None] = mapped_column(ForeignKey("api_requests.id"), nullable=True, unique=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceORM(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("effect_id", "evidence_type", "source_ref", name="uq_effect_evidence"),
        UniqueConstraint("run_id", "evidence_type", "source_ref", name="uq_run_evidence_source"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    action_id: Mapped[int | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    effect_id: Mapped[int | None] = mapped_column(ForeignKey("effects.id"), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    renewal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    verified: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AuditEventORM(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApiRequestORM(Base):
    __tablename__ = "api_requests"
    __table_args__ = (
        UniqueConstraint("actor", "route", "idempotency_key", name="uq_api_request_actor_route_key"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="IN_PROGRESS")
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionORM(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DeploymentBootORM(Base):
    __tablename__ = "deployment_boots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_boot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LoginThrottleORM(Base):
    __tablename__ = "login_throttles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    throttle_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PlanORM(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    issues_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
