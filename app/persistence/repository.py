from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.enums import ActionState, ApprovalStatus, RunState
from app.domain.models import EvidenceFact
from app.domain.state_machine import RUN_TRANSITIONS, ensure_action_transition, ensure_run_transition
from .models import (
    ActionORM,
    ApiRequestORM,
    ApprovalORM,
    AuditEventORM,
    BusinessOperationORM,
    DeploymentBootORM,
    EffectORM,
    EvidenceORM,
    LoginThrottleORM,
    PlanORM,
    RunORM,
    SessionORM,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ApprovalClaimError(PermissionError):
    pass


class ActionExecutionError(RuntimeError):
    pass


class EvidenceDriftError(ValueError):
    pass


class ApiIdempotencyConflict(ValueError):
    pass


class LoginRateLimited(PermissionError):
    pass


ACTIVE_RUN_STATES = {
    RunState.CREATED.value,
    RunState.RESOLVING.value,
    RunState.PLANNING.value,
    RunState.EXECUTING.value,
    RunState.WAITING_APPROVAL.value,
    RunState.RECONCILING.value,
    RunState.UNKNOWN.value,
}


class LedgerRepository:
    def __init__(self, session_factory, *, now_fn=utcnow):
        self.session_factory = session_factory
        self.now_fn = now_fn

    def now(self) -> datetime:
        value = self.now_fn()
        return _aware(value) or utcnow()

    def _audit(self, s, *, run_id: int | None, object_type: str, object_id: int | str,
               event_type: str, from_state: str | None = None, to_state: str | None = None,
               details: str | None = None) -> None:
        s.add(AuditEventORM(
            run_id=run_id,
            object_type=object_type,
            object_id=str(object_id),
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            details=details,
            created_at=self.now(),
        ))

    def healthcheck(self) -> None:
        with self.session_factory() as s:
            s.execute(select(1)).scalar_one()

    def create_run(
        self, goal_text: str, *, owner_actor: str | None = None, api_request_id: int | None = None,
        initial_request_json: str | None = None,
    ) -> RunORM:
        with self.session_factory() as s:
            run = RunORM(
                goal_text=goal_text, owner_actor=owner_actor, api_request_id=api_request_id,
                initial_request_json=initial_request_json, state=RunState.CREATED.value,
            )
            s.add(run)
            s.flush()
            self._audit(s, run_id=run.id, object_type="run", object_id=run.id, event_type="CREATED", to_state=run.state)
            s.commit()
            s.refresh(run)
            return run

    def bind_initial_request(self, *, run_id: int, initial_request_json: str) -> RunORM:
        """Immutably backfill RC5 initialization input for a legacy partial run."""
        with self.session_factory() as s:
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if run is None:
                raise KeyError(run_id)
            if run.initial_request_json is not None and run.initial_request_json != initial_request_json:
                raise ActionExecutionError("run initialization input is immutable once bound")
            if run.initial_request_json is None:
                run.initial_request_json = initial_request_json
                run.updated_at = self.now()
                self._audit(s, run_id=run_id, object_type="run", object_id=run_id, event_type="INITIAL_REQUEST_BOUND")
                s.commit()
                s.refresh(run)
            return run

    def get_run(self, run_id: int) -> RunORM | None:
        with self.session_factory() as s:
            return s.get(RunORM, run_id)

    def get_run_by_api_request(self, api_request_id: int) -> RunORM | None:
        with self.session_factory() as s:
            return s.scalar(select(RunORM).where(RunORM.api_request_id == api_request_id))

    def claim_business_operation(self, *, operation_key: str, run_id: int, lease_seconds: int = 120) -> tuple[BusinessOperationORM, bool]:
        """Claim one business operation before planning.

        The unique row is the serialization point for concurrent duplicate goal
        submissions.  A completed/blocked/deferred owner may hand ownership to a fresh
        run; an active owner cannot be pre-empted.
        """
        if not operation_key.strip():
            raise ValueError("operation_key is required")
        with self.session_factory() as s:
            if s.get(RunORM, run_id) is None:
                raise KeyError(run_id)
            row = s.scalar(
                select(BusinessOperationORM)
                .where(BusinessOperationORM.operation_key == operation_key)
                .with_for_update()
            )
            now = self.now()
            if row is None:
                row = BusinessOperationORM(
                    operation_key=operation_key,
                    owner_run_id=run_id,
                    created_at=now,
                    updated_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                s.add(row)
                try:
                    s.flush()
                except IntegrityError:
                    # Another transaction won the unique-key race.  Re-open a clean
                    # transaction and evaluate its owner rather than surfacing a raw DB
                    # exception to the workflow.
                    s.rollback()
                    row = s.scalar(
                        select(BusinessOperationORM)
                        .where(BusinessOperationORM.operation_key == operation_key)
                        .with_for_update()
                    )
                    if row is None:
                        raise
                else:
                    self._audit(
                        s,
                        run_id=run_id,
                        object_type="business_operation",
                        object_id=row.id,
                        event_type="CLAIMED",
                        details=operation_key,
                    )
                    s.commit()
                    s.refresh(row)
                    return row, True

            if row.owner_run_id == run_id:
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.updated_at = now
                s.commit()
                s.refresh(row)
                return row, True
            owner = s.get(RunORM, row.owner_run_id)
            lease_live = row.lease_expires_at is not None and _aware(row.lease_expires_at) > now
            if owner is not None and owner.state in ACTIVE_RUN_STATES and lease_live:
                return row, False
            if owner is not None and owner.state in ACTIVE_RUN_STATES and not lease_live:
                # Never steal ownership once the prior run has created any consequential
                # action/effect.  That run must be recovered/reconciled instead.
                action_count = s.scalar(select(func.count(ActionORM.id)).where(ActionORM.run_id == owner.id)) or 0
                if action_count:
                    return row, False
                current_owner_state = RunState(owner.state)
                if RunState.BLOCKED_NEEDS_HUMAN in RUN_TRANSITIONS[current_owner_state]:
                    owner.state = RunState.BLOCKED_NEEDS_HUMAN.value
                    owner.updated_at = now
                    self._audit(s, run_id=owner.id, object_type="run", object_id=owner.id,
                                event_type="STALE_OPERATION_OWNER", from_state=current_owner_state.value,
                                to_state=RunState.BLOCKED_NEEDS_HUMAN.value)

            previous_owner = row.owner_run_id
            row.owner_run_id = run_id
            row.updated_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            self._audit(
                s,
                run_id=run_id,
                object_type="business_operation",
                object_id=row.id,
                event_type="OWNERSHIP_TRANSFERRED",
                details=f"{previous_owner}->{run_id}",
            )
            s.commit()
            s.refresh(row)
            return row, True

    def get_business_operation(self, operation_key: str) -> BusinessOperationORM | None:
        with self.session_factory() as s:
            return s.scalar(
                select(BusinessOperationORM).where(BusinessOperationORM.operation_key == operation_key)
            )

    def assert_business_operation_owner(self, *, operation_key: str, run_id: int, lease_seconds: int = 120) -> BusinessOperationORM:
        now = self.now()
        with self.session_factory() as s:
            row = s.scalar(select(BusinessOperationORM).where(
                BusinessOperationORM.operation_key == operation_key
            ).with_for_update())
            if row is None or row.owner_run_id != run_id:
                raise ActionExecutionError("business operation is owned by another run")
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            s.commit()
            s.refresh(row)
            return row

    def bind_run_context(
        self, *, run_id: int, workflow_type: str, subject_customer_id: str,
        subject_renewal_id: str, goal_contract_json: str, snapshot_json: str,
    ) -> RunORM:
        with self.session_factory() as s:
            row = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if row is None:
                raise KeyError(run_id)
            values = (
                workflow_type, subject_customer_id, subject_renewal_id,
                goal_contract_json, snapshot_json,
            )
            existing = (
                row.workflow_type, row.subject_customer_id, row.subject_renewal_id,
                row.goal_contract_json, row.snapshot_json,
            )
            if any(v is not None for v in existing) and existing != values:
                raise ActionExecutionError("run context is immutable once bound")
            row.workflow_type = workflow_type
            row.subject_customer_id = subject_customer_id
            row.subject_renewal_id = subject_renewal_id
            row.goal_contract_json = goal_contract_json
            row.snapshot_json = snapshot_json
            row.updated_at = self.now()
            self._audit(s, run_id=run_id, object_type="run", object_id=run_id,
                        event_type="CONTEXT_BOUND", details=workflow_type)
            s.commit()
            s.refresh(row)
            return row

    def update_run_snapshot(self, *, run_id: int, snapshot_json: str) -> RunORM:
        with self.session_factory() as s:
            row = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if row is None:
                raise KeyError(run_id)
            if row.workflow_type is None:
                raise ActionExecutionError("run context has not been bound")
            row.snapshot_json = snapshot_json
            row.updated_at = self.now()
            self._audit(s, run_id=run_id, object_type="run", object_id=run_id,
                        event_type="SNAPSHOT_REFRESHED")
            s.commit()
            s.refresh(row)
            return row

    def transition_run_state(self, run_id: int, target: RunState, *, expected: RunState | None = None) -> RunORM:
        with self.session_factory() as s:
            row = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if row is None:
                raise KeyError(run_id)
            current = RunState(row.state)
            if expected is not None and current != expected:
                raise ActionExecutionError(f"run state changed: expected {expected}, found {current}")
            if current == target:
                return row
            ensure_run_transition(current, target)
            row.state = target.value
            row.updated_at = self.now()
            self._audit(s, run_id=run_id, object_type="run", object_id=run_id,
                        event_type="STATE_TRANSITION", from_state=current.value, to_state=target.value)
            s.commit()
            s.refresh(row)
            return row

    def get_or_create_action(
        self, *, run_id: int, logical_key: str, action_type: str, target: str,
        payload_hash: str, payload_json: str, initial_state: ActionState,
    ) -> tuple[ActionORM, bool]:
        with self.session_factory() as s:
            existing = s.scalar(select(ActionORM).where(ActionORM.logical_key == logical_key))
            if existing:
                return existing, False
            if s.get(RunORM, run_id) is None:
                raise KeyError(f"run {run_id} not found")
            row = ActionORM(
                run_id=run_id,
                logical_key=logical_key,
                action_type=action_type,
                target=target,
                payload_hash=payload_hash,
                payload_json=payload_json,
                state=initial_state.value,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                existing = s.scalar(select(ActionORM).where(ActionORM.logical_key == logical_key))
                if existing is None:
                    raise
                return existing, False
            self._audit(s, run_id=run_id, object_type="action", object_id=row.id,
                        event_type="CREATED", to_state=initial_state.value)
            s.commit()
            s.refresh(row)
            return row, True

    def get_action(self, logical_key: str) -> ActionORM | None:
        with self.session_factory() as s:
            return s.scalar(select(ActionORM).where(ActionORM.logical_key == logical_key))

    def get_action_by_id(self, action_id: int) -> ActionORM | None:
        with self.session_factory() as s:
            return s.get(ActionORM, action_id)

    def effects_for_action(self, action_id: int) -> list[EffectORM]:
        with self.session_factory() as s:
            return list(s.scalars(
                select(EffectORM).where(EffectORM.action_id == action_id).order_by(EffectORM.attempt_no, EffectORM.id)
            ))

    def effect_for_api_request(self, api_request_id: int, *, action_id: int | None = None) -> EffectORM | None:
        """Find only the first provider attempt durably caused by this HTTP request."""
        with self.session_factory() as s:
            query = select(EffectORM).where(EffectORM.api_request_id == api_request_id)
            if action_id is not None:
                query = query.where(EffectORM.action_id == action_id)
            return s.scalar(query.order_by(EffectORM.id).limit(1))

    def transition_action_state(self, action_id: int, target: ActionState, *, expected: ActionState | None = None) -> ActionORM:
        with self.session_factory() as s:
            row = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            if row is None:
                raise KeyError(action_id)
            current = ActionState(row.state)
            if expected is not None and current != expected:
                raise ActionExecutionError(f"action state changed: expected {expected}, found {current}")
            if current == target:
                return row
            ensure_action_transition(current, target)
            row.state = target.value
            row.updated_at = self.now()
            self._audit(s, run_id=row.run_id, object_type="action", object_id=row.id,
                        event_type="STATE_TRANSITION", from_state=current.value, to_state=target.value)
            s.commit()
            s.refresh(row)
            return row

    def terminate_retryable_continuation(self, *, run_id: int, action_id: int, reason: str, run_target: RunState = RunState.BLOCKED_NEEDS_HUMAN) -> None:
        now = self.now()
        with self.session_factory() as s:
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            if run is None or action is None or action.run_id != run_id:
                raise KeyError(run_id, action_id)
            if ActionState(action.state) != ActionState.RETRYABLE:
                return
            ensure_action_transition(ActionState.RETRYABLE, ActionState.CANCELED)
            action.state = ActionState.CANCELED.value
            action.updated_at = now
            if RunState(run.state) == RunState.EXECUTING:
                ensure_run_transition(RunState.EXECUTING, run_target)
                run.state = run_target.value
                run.updated_at = now
            self._audit(s, run_id=run_id, object_type="action", object_id=action_id, event_type="RETRY_CONTINUATION_TERMINATED", details=reason)
            s.commit()

    def start_attempt(self, action_id: int, provider: str, external_key: str, *, approval_id: int | None = None, lease_seconds: int = 30) -> EffectORM:
        now = self.now()
        with self.session_factory() as s:
            # Atomic compare-and-set prevents two workers from owning the same logical action.
            claimed = s.execute(
                update(ActionORM)
                # RETRYABLE is a historical RC10 state, never new authorization to
                # dispatch another consequential effect.  Only a freshly approved
                # PREPARED action may acquire the one effect-attempt owner.
                .where(ActionORM.id == action_id, ActionORM.state == ActionState.PREPARED.value)
                .values(state=ActionState.EXECUTING.value, updated_at=now)
            )
            if claimed.rowcount != 1:
                s.rollback()
                row = s.get(ActionORM, action_id)
                raise ActionExecutionError(f"action is not executable from state {row.state if row else 'MISSING'}")
            action = s.get(ActionORM, action_id)
            max_attempt = s.scalar(select(func.max(EffectORM.attempt_no)).where(EffectORM.action_id == action_id)) or 0
            effect = EffectORM(
                action_id=action_id,
                attempt_no=max_attempt + 1,
                provider=provider,
                approval_id=approval_id,
                external_key=external_key,
                state=ActionState.EXECUTING.value,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            s.add(effect)
            s.flush()
            previous_state = ActionState.PREPARED.value
            self._audit(s, run_id=action.run_id, object_type="action", object_id=action_id,
                        event_type="STATE_TRANSITION", from_state=previous_state, to_state=ActionState.EXECUTING.value)
            self._audit(s, run_id=action.run_id, object_type="effect", object_id=effect.id,
                        event_type="ATTEMPT_STARTED", to_state=ActionState.EXECUTING.value)
            s.commit()
            s.refresh(effect)
            return effect

    def get_effect(self, effect_id: int) -> EffectORM | None:
        with self.session_factory() as s:
            return s.get(EffectORM, effect_id)

    def set_effect_provider_fence(self, effect_id: int, *, history_start_id: str, effect_reference: str) -> EffectORM:
        """Persist reconciliation proof inputs before the external provider call."""
        with self.session_factory() as s:
            row = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if row is None:
                raise KeyError(effect_id)
            if row.provider_history_start_id not in {None, history_start_id} or row.provider_effect_reference not in {None, effect_reference}:
                raise ActionExecutionError("provider reconciliation fence is immutable")
            row.provider_history_start_id, row.provider_effect_reference = history_start_id, effect_reference
            row.updated_at = self.now()
            s.commit(); s.refresh(row); return row

    def bind_effect_provider_observation(self, effect_id: int, *, provider_message_id: str, provider_thread_id: str | None, provider_rfc_message_id: str | None) -> EffectORM:
        with self.session_factory() as s:
            row=s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if row is None: raise KeyError(effect_id)
            if row.provider_ref not in {None, provider_message_id}:
                raise ActionExecutionError("conflicting provider message identity")
            row.provider_ref=provider_message_id; row.provider_thread_id=provider_thread_id; row.provider_rfc_message_id=provider_rfc_message_id; row.updated_at=self.now()
            s.commit(); s.refresh(row); return row

    def latest_attempt(self, action_id: int) -> EffectORM | None:
        with self.session_factory() as s:
            return s.scalar(
                select(EffectORM)
                .where(EffectORM.action_id == action_id)
                .order_by(EffectORM.attempt_no.desc())
                .limit(1)
            )

    def transition_attempt(
        self, effect_id: int, *, target: ActionState, provider_ref: str | None = None,
        last_error: str | None = None, increment_reconcile: bool = False,
    ) -> EffectORM:
        with self.session_factory() as s:
            row = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if row is None:
                raise KeyError(effect_id)
            current = ActionState(row.state)
            if current != target:
                ensure_action_transition(current, target)
            now = self.now()
            row.state = target.value
            row.updated_at = now
            if provider_ref is not None:
                row.provider_ref = provider_ref
            if last_error is not None:
                row.last_error = last_error
            if target == ActionState.UNKNOWN and row.first_unknown_at is None:
                row.first_unknown_at = now
            if increment_reconcile:
                row.reconcile_count += 1
            action = s.get(ActionORM, row.action_id)
            self._audit(s, run_id=action.run_id if action else None, object_type="effect", object_id=row.id,
                        event_type="STATE_TRANSITION", from_state=current.value, to_state=target.value,
                        details=last_error)
            s.commit()
            s.refresh(row)
            return row

    def mark_action_and_effect_unknown(self, action_id: int, effect_id: int, error: str) -> None:
        with self.session_factory() as s:
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            effect = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if action is None or effect is None:
                raise KeyError(action_id, effect_id)
            acur, ecur = ActionState(action.state), ActionState(effect.state)
            if acur != ActionState.UNKNOWN:
                ensure_action_transition(acur, ActionState.UNKNOWN)
            if ecur != ActionState.UNKNOWN:
                ensure_action_transition(ecur, ActionState.UNKNOWN)
            now = self.now()
            action.state = ActionState.UNKNOWN.value
            action.updated_at = now
            effect.state = ActionState.UNKNOWN.value
            effect.updated_at = now
            effect.last_error = error
            effect.first_unknown_at = effect.first_unknown_at or now
            self._audit(s, run_id=action.run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=acur.value, to_state=ActionState.UNKNOWN.value, details=error)
            self._audit(s, run_id=action.run_id, object_type="effect", object_id=effect.id,
                        event_type="STATE_TRANSITION", from_state=ecur.value, to_state=ActionState.UNKNOWN.value, details=error)
            s.commit()

    def begin_reconciliation(self, action_id: int, effect_id: int) -> None:
        with self.session_factory() as s:
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            effect = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if action is None or effect is None:
                raise KeyError(action_id, effect_id)
            acur, ecur = ActionState(action.state), ActionState(effect.state)
            if acur != ActionState.RECONCILING:
                ensure_action_transition(acur, ActionState.RECONCILING)
            if ecur != ActionState.RECONCILING:
                ensure_action_transition(ecur, ActionState.RECONCILING)
            action.state = ActionState.RECONCILING.value
            effect.state = ActionState.RECONCILING.value
            effect.reconcile_count += 1
            action.updated_at = effect.updated_at = self.now()
            self._audit(s, run_id=action.run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=acur.value, to_state=ActionState.RECONCILING.value)
            self._audit(s, run_id=action.run_id, object_type="effect", object_id=effect.id,
                        event_type="RECONCILE", from_state=ecur.value, to_state=ActionState.RECONCILING.value)
            s.commit()

    def finish_reconciliation(self, action_id: int, effect_id: int, target: ActionState, *, provider_ref: str | None = None, error: str | None = None) -> None:
        if target not in {ActionState.CONFIRMED, ActionState.UNKNOWN, ActionState.FAILED}:
            raise ValueError(target)
        with self.session_factory() as s:
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            effect = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if action is None or effect is None:
                raise KeyError(action_id, effect_id)
            acur, ecur = ActionState(action.state), ActionState(effect.state)
            ensure_action_transition(acur, target)
            ensure_action_transition(ecur, target)
            now = self.now()
            action.state = target.value
            action.updated_at = now
            effect.state = target.value
            effect.updated_at = now
            if provider_ref is not None:
                effect.provider_ref = provider_ref
            if error is not None:
                effect.last_error = error
            if target == ActionState.UNKNOWN and effect.first_unknown_at is None:
                effect.first_unknown_at = now
            self._audit(s, run_id=action.run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=acur.value, to_state=target.value, details=error)
            self._audit(s, run_id=action.run_id, object_type="effect", object_id=effect.id,
                        event_type="STATE_TRANSITION", from_state=ecur.value, to_state=target.value, details=error)
            s.commit()

    def expired_executing_attempts(self, *, now: datetime | None = None) -> list[EffectORM]:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            return list(s.scalars(select(EffectORM).where(
                EffectORM.state == ActionState.EXECUTING.value,
                EffectORM.lease_expires_at.is_not(None),
                EffectORM.lease_expires_at <= now,
            )))

    def expired_executing_attempt_for_action(self, action_id: int, *, now: datetime | None = None) -> EffectORM | None:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            return s.scalar(
                select(EffectORM)
                .where(
                    EffectORM.action_id == action_id,
                    EffectORM.state == ActionState.EXECUTING.value,
                    EffectORM.lease_expires_at.is_not(None),
                    EffectORM.lease_expires_at <= now,
                )
                .order_by(EffectORM.attempt_no.desc())
                .limit(1)
            )

    def create_or_get_active_approval(
        self, *, run_id: int, action_id: int, payload_hash: str, actor: str,
        nonce: str, expires_at: datetime, now: datetime | None = None,
        api_request_id: int | None = None,
    ):
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            # Lock the action so concurrent approval requests converge on one active authorization.
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            if action is None or action.run_id != run_id:
                raise ApprovalClaimError("approval run/action mismatch")
            active_rows = list(s.scalars(
                select(ApprovalORM).where(
                    ApprovalORM.run_id == run_id,
                    ApprovalORM.action_id == action_id,
                    ApprovalORM.payload_hash == payload_hash,
                    ApprovalORM.actor == actor,
                    ApprovalORM.status == ApprovalStatus.ACTIVE.value,
                ).with_for_update()
            ))
            for existing in active_rows:
                if _aware(existing.expires_at) > now:
                    same_causal_request = api_request_id is not None and existing.api_request_id == api_request_id
                    same_direct_window = api_request_id is None and existing.api_request_id is None and _aware(existing.expires_at) == _aware(expires_at)
                    if same_causal_request or same_direct_window:
                        return existing
                    raise ApprovalClaimError(
                        "an active approval already exists with a different authorization request/window"
                    )
                existing.status = ApprovalStatus.EXPIRED.value
                self._audit(s, run_id=run_id, object_type="approval", object_id=existing.id, event_type="EXPIRED")

            # If the only active approval(s) expired, re-open the approval boundary before
            # issuing a fresh authorization.
            if active_rows and ActionState(action.state) == ActionState.APPROVED:
                ensure_action_transition(ActionState.APPROVED, ActionState.INVALIDATED)
                action.state = ActionState.INVALIDATED.value
                ensure_action_transition(ActionState.INVALIDATED, ActionState.APPROVAL_REQUIRED)
                action.state = ActionState.APPROVAL_REQUIRED.value
                action.updated_at = now

            if ActionState(action.state) != ActionState.APPROVAL_REQUIRED:
                raise ApprovalClaimError(f"action is not awaiting approval: {action.state}")
            row = ApprovalORM(
                run_id=run_id,
                action_id=action_id,
                payload_hash=payload_hash,
                actor=actor,
                api_request_id=api_request_id,
                nonce=nonce,
                status=ApprovalStatus.ACTIVE.value,
                expires_at=expires_at,
                created_at=now,
            )
            s.add(row)
            ensure_action_transition(ActionState(action.state), ActionState.APPROVED)
            action.state = ActionState.APPROVED.value
            action.updated_at = now
            s.flush()
            self._audit(s, run_id=run_id, object_type="approval", object_id=row.id, event_type="APPROVED")
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=ActionState.APPROVAL_REQUIRED.value,
                        to_state=ActionState.APPROVED.value)
            s.commit()
            s.refresh(row)
            return row

    def get_approval(self, approval_id: int):
        with self.session_factory() as s:
            return s.get(ApprovalORM, approval_id)

    def revoke_approval(self, approval_id: int, *, api_request_id: int | None = None, now: datetime | None = None) -> None:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            row = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            if row is None:
                raise KeyError(approval_id)
            if row.status != ApprovalStatus.ACTIVE.value:
                raise ApprovalClaimError(f"cannot revoke approval in state {row.status}")
            if api_request_id is not None:
                request = s.get(ApiRequestORM, api_request_id)
                if request is None:
                    raise KeyError(api_request_id)
                row.rejected_api_request_id = api_request_id
            row.status = ApprovalStatus.REVOKED.value
            action = s.get(ActionORM, row.action_id)
            if action is not None and ActionState(action.state) == ActionState.APPROVED:
                ensure_action_transition(ActionState.APPROVED, ActionState.INVALIDATED)
                action.state = ActionState.INVALIDATED.value
                ensure_action_transition(ActionState.INVALIDATED, ActionState.APPROVAL_REQUIRED)
                action.state = ActionState.APPROVAL_REQUIRED.value
                action.updated_at = now
            self._audit(s, run_id=row.run_id, object_type="approval", object_id=row.id, event_type="REVOKED")
            s.commit()

    def claim_approval(self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str, now: datetime | None = None) -> ApprovalORM:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            approval = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if approval is None or action is None or run is None:
                raise ApprovalClaimError("approval/action/run not found")
            if approval.run_id != run_id or action.run_id != run_id:
                raise ApprovalClaimError("approval/action belongs to another run")
            if approval.action_id != action_id:
                raise ApprovalClaimError("approval belongs to a different action")
            if approval.actor != actor:
                raise ApprovalClaimError("approval actor mismatch")
            if approval.payload_hash != payload_hash or action.payload_hash != payload_hash:
                raise ApprovalClaimError("approved payload does not match execution payload")
            if approval.status != ApprovalStatus.ACTIVE.value:
                raise ApprovalClaimError(f"approval is {approval.status.lower()}")
            if _aware(approval.expires_at) <= now:
                approval.status = ApprovalStatus.EXPIRED.value
                if ActionState(action.state) == ActionState.APPROVED:
                    ensure_action_transition(ActionState.APPROVED, ActionState.INVALIDATED)
                    action.state = ActionState.INVALIDATED.value
                    ensure_action_transition(ActionState.INVALIDATED, ActionState.APPROVAL_REQUIRED)
                    action.state = ActionState.APPROVAL_REQUIRED.value
                    action.updated_at = now
                self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="EXPIRED")
                s.commit()
                raise ApprovalClaimError("approval expired")
            if ActionState(action.state) != ActionState.APPROVED:
                raise ApprovalClaimError(f"action is not approved: {action.state}")
            if RunState(run.state) != RunState.WAITING_APPROVAL:
                raise ApprovalClaimError(f"run is not waiting for approval: {run.state}")
            # SQLite ignores SELECT FOR UPDATE.  Claim ownership with a portable
            # compare-and-set before changing action/run state or starting an effect.
            claimed = s.execute(
                update(ApprovalORM)
                .where(
                    ApprovalORM.id == approval_id,
                    ApprovalORM.status == ApprovalStatus.ACTIVE.value,
                    ApprovalORM.expires_at > now,
                )
                .values(status=ApprovalStatus.CLAIMED.value, claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                s.rollback()
                current = s.get(ApprovalORM, approval_id)
                raise ApprovalClaimError(
                    f"approval is {current.status.lower() if current is not None else 'missing'}"
                )
            approval = s.get(ApprovalORM, approval_id)
            assert approval is not None
            ensure_action_transition(ActionState.APPROVED, ActionState.PREPARED)
            ensure_run_transition(RunState.WAITING_APPROVAL, RunState.EXECUTING)
            action.state = ActionState.PREPARED.value
            action.updated_at = now
            run.state = RunState.EXECUTING.value
            run.updated_at = now
            self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="CLAIMED")
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=ActionState.APPROVED.value, to_state=ActionState.PREPARED.value)
            self._audit(s, run_id=run_id, object_type="run", object_id=run.id,
                        event_type="STATE_TRANSITION", from_state=RunState.WAITING_APPROVAL.value, to_state=RunState.EXECUTING.value)
            s.commit()
            s.refresh(approval)
            return approval

    def claim_approval_and_start_attempt(
        self, *, approval_id: int, run_id: int, action_id: int, payload_hash: str, actor: str,
        provider: str, external_key: str, api_request_id: int | None = None,
        lease_seconds: int = 30, now: datetime | None = None,
    ) -> EffectORM:
        """Atomically cross the authorization/effect boundary.

        The approval claim, run/action transition and first/new provider attempt are one
        transaction.  A process crash therefore cannot leave a newly executed request in
        ``CLAIMED + PREPARED`` with no durable effect attempt.
        """
        now = _aware(now) or self.now()
        if not provider.strip() or not external_key.strip():
            raise ValueError("provider and external_key are required")
        with self.session_factory() as s:
            approval = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if approval is None or action is None or run is None:
                raise ApprovalClaimError("approval/action/run not found")
            if api_request_id is not None and s.get(ApiRequestORM, api_request_id) is None:
                raise ApprovalClaimError("originating API request not found")
            if approval.run_id != run_id or action.run_id != run_id:
                raise ApprovalClaimError("approval/action belongs to another run")
            if approval.action_id != action_id:
                raise ApprovalClaimError("approval belongs to a different action")
            if approval.actor != actor:
                raise ApprovalClaimError("approval actor mismatch")
            if approval.payload_hash != payload_hash or action.payload_hash != payload_hash:
                raise ApprovalClaimError("approved payload does not match execution payload")
            if approval.status != ApprovalStatus.ACTIVE.value:
                raise ApprovalClaimError(f"approval is {approval.status.lower()}")
            if _aware(approval.expires_at) <= now:
                approval.status = ApprovalStatus.EXPIRED.value
                if ActionState(action.state) == ActionState.APPROVED:
                    ensure_action_transition(ActionState.APPROVED, ActionState.INVALIDATED)
                    action.state = ActionState.INVALIDATED.value
                    ensure_action_transition(ActionState.INVALIDATED, ActionState.APPROVAL_REQUIRED)
                    action.state = ActionState.APPROVAL_REQUIRED.value
                    action.updated_at = now
                self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="EXPIRED")
                s.commit()
                raise ApprovalClaimError("approval expired")
            if ActionState(action.state) != ActionState.APPROVED:
                raise ApprovalClaimError(f"action is not approved: {action.state}")
            if RunState(run.state) != RunState.WAITING_APPROVAL:
                raise ApprovalClaimError(f"run is not waiting for approval: {run.state}")

            # This conditional update is the durable first-claim owner election on
            # every supported backend.  A competing worker loses before it can create
            # an effect attempt or reach the provider.
            claimed = s.execute(
                update(ApprovalORM)
                .where(
                    ApprovalORM.id == approval_id,
                    ApprovalORM.status == ApprovalStatus.ACTIVE.value,
                    ApprovalORM.expires_at > now,
                )
                .values(status=ApprovalStatus.CLAIMED.value, claimed_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                s.rollback()
                current = s.get(ApprovalORM, approval_id)
                raise ApprovalClaimError(
                    f"approval is {current.status.lower() if current is not None else 'missing'}"
                )
            approval = s.get(ApprovalORM, approval_id)
            assert approval is not None

            ensure_action_transition(ActionState.APPROVED, ActionState.PREPARED)
            ensure_action_transition(ActionState.PREPARED, ActionState.EXECUTING)
            ensure_run_transition(RunState.WAITING_APPROVAL, RunState.EXECUTING)
            action.state = ActionState.EXECUTING.value
            action.updated_at = now
            run.state = RunState.EXECUTING.value
            run.updated_at = now

            max_attempt = s.scalar(select(func.max(EffectORM.attempt_no)).where(EffectORM.action_id == action_id)) or 0
            effect = EffectORM(
                action_id=action_id,
                attempt_no=max_attempt + 1,
                provider=provider,
                approval_id=approval_id,
                api_request_id=api_request_id,
                external_key=external_key,
                state=ActionState.EXECUTING.value,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                created_at=now,
                updated_at=now,
            )
            s.add(effect)
            s.flush()
            self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="CLAIMED")
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=ActionState.APPROVED.value, to_state=ActionState.PREPARED.value)
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=ActionState.PREPARED.value, to_state=ActionState.EXECUTING.value)
            self._audit(s, run_id=run_id, object_type="run", object_id=run.id,
                        event_type="STATE_TRANSITION", from_state=RunState.WAITING_APPROVAL.value, to_state=RunState.EXECUTING.value)
            self._audit(s, run_id=run_id, object_type="effect", object_id=effect.id,
                        event_type="ATTEMPT_STARTED", to_state=ActionState.EXECUTING.value)
            s.commit()
            s.refresh(effect)
            return effect

    def claimed_approval_for_action(self, action_id: int) -> ApprovalORM | None:
        with self.session_factory() as s:
            return s.scalar(
                select(ApprovalORM)
                .where(ApprovalORM.action_id == action_id, ApprovalORM.status == ApprovalStatus.CLAIMED.value)
                .order_by(ApprovalORM.id.desc())
                .limit(1)
            )

    def terminate_prepared_without_effect(
        self, *, run_id: int, action_id: int, approval_id: int | None, reason: str, run_target: RunState,
        now: datetime | None = None,
    ) -> None:
        """Fail closed for a legacy PREPARED crash that provably never started an effect."""
        now = _aware(now) or self.now()
        if run_target not in {RunState.FAILED, RunState.BLOCKED_NEEDS_HUMAN}:
            raise ValueError("prepared-without-effect termination must be FAILED or BLOCKED_NEEDS_HUMAN")
        with self.session_factory() as s:
            approval = (
                s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
                if approval_id is not None else None
            )
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if action is None or run is None or action.run_id != run_id:
                raise KeyError(run_id, action_id)
            if approval_id is not None and (approval is None or approval.action_id != action_id or approval.run_id != run_id):
                raise KeyError(run_id, action_id, approval_id)
            if ActionState(action.state) != ActionState.PREPARED:
                raise ActionExecutionError(f"action is not PREPARED: {action.state}")
            effect_count = s.scalar(select(func.count(EffectORM.id)).where(EffectORM.action_id == action_id)) or 0
            if effect_count:
                raise ActionExecutionError("cannot terminate PREPARED-without-effect recovery after an effect exists")
            if approval is not None and approval.status == ApprovalStatus.CLAIMED.value:
                approval.status = ApprovalStatus.EXPIRED.value
            ensure_action_transition(ActionState.PREPARED, ActionState.INVALIDATED)
            action.state = ActionState.INVALIDATED.value
            action.updated_at = now
            current_run = RunState(run.state)
            if current_run != run_target:
                ensure_run_transition(current_run, run_target)
                run.state = run_target.value
                run.updated_at = now
            if approval is not None:
                self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id,
                            event_type="PREPARED_RECOVERY_TERMINATED", details=reason)
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id,
                        event_type="STATE_TRANSITION", from_state=ActionState.PREPARED.value, to_state=ActionState.INVALIDATED.value, details=reason)
            self._audit(s, run_id=run_id, object_type="run", object_id=run.id,
                        event_type="PREPARED_RECOVERY_TERMINATED", from_state=current_run.value, to_state=run_target.value, details=reason)
            s.commit()

    def expire_prepared_approval_and_reopen(
        self, *, approval_id: int, run_id: int, action_id: int, now: datetime | None = None,
    ) -> None:
        """Recover a legacy PREPARED/no-effect crash after continuation authorization expires."""
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            approval = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if approval is None or action is None or run is None or action.run_id != run_id or approval.action_id != action_id:
                raise KeyError(run_id, action_id, approval_id)
            if ActionState(action.state) != ActionState.PREPARED:
                raise ApprovalClaimError(f"action is not prepared: {action.state}")
            if RunState(run.state) != RunState.EXECUTING:
                raise ApprovalClaimError(f"run is not executing: {run.state}")
            effect_count = s.scalar(select(func.count(EffectORM.id)).where(EffectORM.action_id == action_id)) or 0
            if effect_count:
                raise ApprovalClaimError("cannot reopen PREPARED authorization after an effect attempt exists")
            if approval.status != ApprovalStatus.CLAIMED.value:
                raise ApprovalClaimError(f"approval is not claimed: {approval.status}")
            ensure_action_transition(ActionState.PREPARED, ActionState.INVALIDATED)
            ensure_action_transition(ActionState.INVALIDATED, ActionState.APPROVAL_REQUIRED)
            ensure_run_transition(RunState.EXECUTING, RunState.WAITING_APPROVAL)
            approval.status = ApprovalStatus.EXPIRED.value
            action.state = ActionState.APPROVAL_REQUIRED.value
            action.updated_at = now
            run.state = RunState.WAITING_APPROVAL.value
            run.updated_at = now
            self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="CONTINUATION_EXPIRED")
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id, event_type="STATE_TRANSITION",
                        from_state=ActionState.PREPARED.value, to_state=ActionState.APPROVAL_REQUIRED.value,
                        details="LEGACY_PREPARED_CONTINUATION_EXPIRED")
            self._audit(s, run_id=run_id, object_type="run", object_id=run.id, event_type="STATE_TRANSITION",
                        from_state=RunState.EXECUTING.value, to_state=RunState.WAITING_APPROVAL.value)
            s.commit()

    def expire_retryable_approval_and_reopen(
        self, *, approval_id: int, run_id: int, action_id: int, now: datetime | None = None,
    ) -> None:
        """Expire a stale claimed retry authorization and reopen human approval.

        This is intentionally atomic: the old CLAIMED approval becomes EXPIRED, the
        RETRYABLE action returns to APPROVAL_REQUIRED, and the run returns to
        WAITING_APPROVAL in one transaction.  The action/logical key are preserved.
        """
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            approval = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            run = s.scalar(select(RunORM).where(RunORM.id == run_id).with_for_update())
            if approval is None or action is None or run is None:
                raise ApprovalClaimError("approval/action/run not found")
            if approval.run_id != run_id or approval.action_id != action_id or action.run_id != run_id:
                raise ApprovalClaimError("approval/action belongs to another run")
            if approval.status != ApprovalStatus.CLAIMED.value:
                raise ApprovalClaimError(f"approval is {approval.status.lower()}; cannot expire retry continuation")
            if ActionState(action.state) != ActionState.RETRYABLE:
                raise ApprovalClaimError(f"action is not retryable: {action.state}")
            if RunState(run.state) != RunState.EXECUTING:
                raise ApprovalClaimError(f"run is not executing: {run.state}")
            ensure_action_transition(ActionState.RETRYABLE, ActionState.APPROVAL_REQUIRED)
            ensure_run_transition(RunState.EXECUTING, RunState.WAITING_APPROVAL)
            approval.status = ApprovalStatus.EXPIRED.value
            action.state = ActionState.APPROVAL_REQUIRED.value
            action.updated_at = now
            run.state = RunState.WAITING_APPROVAL.value
            run.updated_at = now
            self._audit(s, run_id=run_id, object_type="approval", object_id=approval.id, event_type="CONTINUATION_EXPIRED")
            self._audit(s, run_id=run_id, object_type="action", object_id=action.id, event_type="STATE_TRANSITION",
                        from_state=ActionState.RETRYABLE.value, to_state=ActionState.APPROVAL_REQUIRED.value)
            self._audit(s, run_id=run_id, object_type="run", object_id=run.id, event_type="STATE_TRANSITION",
                        from_state=RunState.EXECUTING.value, to_state=RunState.WAITING_APPROVAL.value)
            s.commit()

    def consume_approval(self, approval_id: int, *, now: datetime | None = None) -> None:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            row = s.scalar(select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update())
            if row is None:
                raise KeyError(approval_id)
            if row.status == ApprovalStatus.CONSUMED.value:
                return
            if row.status != ApprovalStatus.CLAIMED.value:
                raise ApprovalClaimError(f"cannot consume approval in state {row.status}")
            row.status = ApprovalStatus.CONSUMED.value
            row.consumed_at = now
            self._audit(s, run_id=row.run_id, object_type="approval", object_id=row.id, event_type="CONSUMED")
            s.commit()

    def record_evidence(
        self, *, run_id: int, action_id: int | None = None, effect_id: int | None = None,
        customer_id: str | None, renewal_id: str | None, evidence_type: str, source_ref: str,
        expected: str | None, observed: str | None, observed_at: datetime, verified: bool = True,
    ) -> EvidenceORM:
        if not source_ref.strip():
            raise ValueError("evidence source_ref is required")
        with self.session_factory() as s:
            run = s.get(RunORM, run_id)
            if run is None:
                raise KeyError(run_id)
            action = None
            if action_id is not None:
                action = s.get(ActionORM, action_id)
                if action is None or action.run_id != run_id:
                    raise ValueError("evidence action is missing or belongs to another run")
            if effect_id is not None:
                effect = s.get(EffectORM, effect_id)
                if effect is None:
                    raise ValueError("evidence effect is missing")
                if action_id is None:
                    action_id = effect.action_id
                    action = s.get(ActionORM, action_id)
                    if action is None or action.run_id != run_id:
                        raise ValueError("evidence effect belongs to another run")
                elif effect.action_id != action_id:
                    raise ValueError("evidence effect/action mismatch")
                elif action is None:
                    action = s.get(ActionORM, action_id)
                if action is None or action.run_id != run_id:
                    raise ValueError("evidence effect/action belongs to another run")
            existing = s.scalar(select(EvidenceORM).where(
                EvidenceORM.run_id == run_id,
                EvidenceORM.evidence_type == evidence_type,
                EvidenceORM.source_ref == source_ref,
            ))
            if existing:
                identity_same = (
                    existing.action_id == action_id
                    and existing.effect_id == effect_id
                    and existing.customer_id == customer_id
                    and existing.renewal_id == renewal_id
                )
                if not identity_same:
                    raise ValueError("evidence identity drift for existing source")
                content_same = (
                    existing.expected == expected
                    and existing.observed == observed
                    and bool(existing.verified) == bool(verified)
                )
                if not content_same:
                    raise EvidenceDriftError("evidence content drift for existing immutable source")
                return existing
            verified_at = self.now()
            row = EvidenceORM(
                run_id=run_id,
                action_id=action_id,
                effect_id=effect_id,
                customer_id=customer_id,
                renewal_id=renewal_id,
                evidence_type=evidence_type,
                source_ref=source_ref,
                expected=expected,
                observed=observed,
                observed_at=observed_at,
                verified_at=verified_at,
                verified=1 if verified else 0,
            )
            s.add(row)
            self._audit(s, run_id=run_id, object_type="evidence", object_id=source_ref,
                        event_type="VERIFIED" if verified else "UNVERIFIED")
            s.commit()
            s.refresh(row)
            return row

    def verified_evidence_for_effect(self, effect_id: int, evidence_type: str) -> list[EvidenceORM]:
        with self.session_factory() as s:
            return list(s.scalars(select(EvidenceORM).where(
                EvidenceORM.effect_id == effect_id,
                EvidenceORM.evidence_type == evidence_type,
                EvidenceORM.verified == 1,
            )))

    def confirm_effect_with_evidence(
        self,
        *,
        action_id: int,
        effect_id: int,
        provider_ref: str,
        customer_id: str | None,
        renewal_id: str | None,
        evidence_type: str,
        source_ref: str,
        expected: str | None,
        observed: str | None,
        observed_at: datetime,
        approval_id: int | None,
        now: datetime | None = None,
    ) -> EvidenceORM:
        """Atomically confirm effect/action, persist proof, and consume authorization.

        This closes the crash window where an external effect had been marked CONFIRMED
        but its durable proof had not yet been stored.  The method is idempotent for an
        already-confirmed effect *only* when the exact same immutable evidence is supplied.
        """
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            action = s.scalar(select(ActionORM).where(ActionORM.id == action_id).with_for_update())
            effect = s.scalar(select(EffectORM).where(EffectORM.id == effect_id).with_for_update())
            if action is None or effect is None:
                raise KeyError(action_id, effect_id)
            if effect.action_id != action_id:
                raise ValueError("effect/action mismatch")
            run_id = action.run_id

            acur, ecur = ActionState(action.state), ActionState(effect.state)
            if acur != ActionState.CONFIRMED:
                ensure_action_transition(acur, ActionState.CONFIRMED)
                action.state = ActionState.CONFIRMED.value
                action.updated_at = now
                self._audit(
                    s, run_id=run_id, object_type="action", object_id=action.id,
                    event_type="STATE_TRANSITION", from_state=acur.value,
                    to_state=ActionState.CONFIRMED.value,
                )
            if ecur != ActionState.CONFIRMED:
                ensure_action_transition(ecur, ActionState.CONFIRMED)
                effect.state = ActionState.CONFIRMED.value
                effect.updated_at = now
                self._audit(
                    s, run_id=run_id, object_type="effect", object_id=effect.id,
                    event_type="STATE_TRANSITION", from_state=ecur.value,
                    to_state=ActionState.CONFIRMED.value,
                )
            effect.provider_ref = provider_ref

            existing = s.scalar(select(EvidenceORM).where(
                EvidenceORM.run_id == run_id,
                EvidenceORM.evidence_type == evidence_type,
                EvidenceORM.source_ref == source_ref,
            ))
            if existing is not None:
                same = (
                    existing.action_id == action_id
                    and existing.effect_id == effect_id
                    and existing.customer_id == customer_id
                    and existing.renewal_id == renewal_id
                    and existing.expected == expected
                    and existing.observed == observed
                    and bool(existing.verified)
                )
                if not same:
                    raise EvidenceDriftError("evidence content drift for confirmed provider effect")
                evidence = existing
            else:
                evidence = EvidenceORM(
                    run_id=run_id,
                    action_id=action_id,
                    effect_id=effect_id,
                    customer_id=customer_id,
                    renewal_id=renewal_id,
                    evidence_type=evidence_type,
                    source_ref=source_ref,
                    expected=expected,
                    observed=observed,
                    observed_at=observed_at,
                    verified_at=now,
                    verified=1,
                )
                s.add(evidence)
                self._audit(
                    s, run_id=run_id, object_type="evidence", object_id=source_ref,
                    event_type="VERIFIED",
                )

            if approval_id is not None:
                approval = s.scalar(
                    select(ApprovalORM).where(ApprovalORM.id == approval_id).with_for_update()
                )
                if approval is None:
                    raise ApprovalClaimError("approval not found while confirming effect")
                if approval.run_id != run_id or approval.action_id != action_id:
                    raise ApprovalClaimError("approval does not belong to confirmed action/run")
                if approval.status == ApprovalStatus.CLAIMED.value:
                    approval.status = ApprovalStatus.CONSUMED.value
                    approval.consumed_at = now
                    self._audit(
                        s, run_id=run_id, object_type="approval", object_id=approval.id,
                        event_type="CONSUMED",
                    )
                elif approval.status != ApprovalStatus.CONSUMED.value:
                    raise ApprovalClaimError(f"cannot confirm effect with approval in state {approval.status}")

            s.commit()
            s.refresh(evidence)
            return evidence

    def evidence_for_run(self, run_id: int) -> list[EvidenceFact]:
        with self.session_factory() as s:
            rows = list(s.scalars(select(EvidenceORM).where(EvidenceORM.run_id == run_id)))
            return [EvidenceFact(
                evidence_type=r.evidence_type,
                source_ref=r.source_ref,
                customer_id=r.customer_id,
                renewal_id=r.renewal_id,
                observed_at=_aware(r.observed_at),
                verified_at=_aware(r.verified_at),
                verified=bool(r.verified),
                expected_hash=r.expected,
                observed_hash=r.observed,
            ) for r in rows]

    def audit_events_for_run(self, run_id: int) -> list[AuditEventORM]:
        with self.session_factory() as s:
            return list(s.scalars(select(AuditEventORM).where(AuditEventORM.run_id == run_id).order_by(AuditEventORM.id)))


    def begin_login_attempt(
        self, *, throttle_key: str, max_failures: int = 5, window_seconds: int = 300,
        lockout_seconds: int = 300,
    ) -> None:
        """Reserve one password attempt atomically across workers.

        Attempts are counted before password verification; a successful login clears the
        row.  This prevents concurrent brute-force requests from bypassing an in-memory
        limiter while still allowing a correct credential before the budget is exhausted.
        """
        if not throttle_key.strip():
            raise ValueError("throttle_key is required")
        if max_failures < 1 or window_seconds < 1 or lockout_seconds < 1:
            raise ValueError("login throttle values must be positive")
        now = self.now()
        window_start = now - timedelta(seconds=window_seconds)
        with self.session_factory() as s:
            row = s.scalar(
                select(LoginThrottleORM)
                .where(LoginThrottleORM.throttle_key == throttle_key)
                .with_for_update()
            )
            if row is None:
                row = LoginThrottleORM(
                    throttle_key=throttle_key, failures=1, window_started_at=now,
                    locked_until=None, updated_at=now,
                )
                s.add(row)
                try:
                    s.commit()
                    return
                except IntegrityError:
                    s.rollback()
                    row = s.scalar(
                        select(LoginThrottleORM)
                        .where(LoginThrottleORM.throttle_key == throttle_key)
                        .with_for_update()
                    )
                    if row is None:
                        raise
            if row.locked_until is not None and _aware(row.locked_until) > now:
                raise LoginRateLimited("too many failed login attempts; try again later")
            if _aware(row.window_started_at) < window_start:
                row.failures = 0
                row.window_started_at = now
                row.locked_until = None
            row.failures += 1
            if row.failures > max_failures:
                row.locked_until = now + timedelta(seconds=lockout_seconds)
                row.updated_at = now
                s.commit()
                raise LoginRateLimited("too many failed login attempts; try again later")
            row.updated_at = now
            s.commit()

    def clear_login_attempts(self, *, throttle_key: str) -> None:
        with self.session_factory() as s:
            row = s.scalar(
                select(LoginThrottleORM)
                .where(LoginThrottleORM.throttle_key == throttle_key)
                .with_for_update()
            )
            if row is None:
                return
            s.delete(row)
            s.commit()

    def create_session(self, *, session_id: str, actor: str, expires_at: datetime) -> SessionORM:
        with self.session_factory() as s:
            row = SessionORM(session_id=session_id, actor=actor, expires_at=expires_at, created_at=self.now())
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def session_is_active(self, *, session_id: str, actor: str, now: datetime | None = None) -> bool:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            row = s.scalar(select(SessionORM).where(SessionORM.session_id == session_id, SessionORM.actor == actor))
            return bool(row and row.revoked_at is None and _aware(row.expires_at) > now)

    def revoke_session(self, *, session_id: str, actor: str, now: datetime | None = None) -> None:
        now = _aware(now) or self.now()
        with self.session_factory() as s:
            row = s.scalar(select(SessionORM).where(SessionORM.session_id == session_id, SessionORM.actor == actor).with_for_update())
            if row is None:
                return
            if row.revoked_at is None:
                row.revoked_at = now
                s.commit()

    def record_plan_attempt(
        self, *, run_id: int, model_name: str, input_hash: str, proposal_json: str,
        validation_status: str, issues_json: str,
    ) -> PlanORM:
        with self.session_factory() as s:
            if s.get(RunORM, run_id) is None:
                raise KeyError(run_id)
            row = PlanORM(
                run_id=run_id,
                model_name=model_name,
                input_hash=input_hash,
                proposal_json=proposal_json,
                validation_status=validation_status,
                issues_json=issues_json,
            )
            s.add(row)
            s.flush()
            self._audit(
                s, run_id=run_id, object_type="plan", object_id=row.id,
                event_type="PLAN_VALIDATED", details=validation_status,
            )
            s.commit()
            s.refresh(row)
            return row

    def register_deployment_boot(self, *, deployment_key: str, boot_id: str) -> int:
        if not deployment_key.strip() or not boot_id.strip():
            raise ValueError("deployment_key and boot_id are required")
        now = self.now()
        with self.session_factory() as s:
            row = s.scalar(
                select(DeploymentBootORM)
                .where(DeploymentBootORM.deployment_key == deployment_key)
                .with_for_update()
            )
            if row is None:
                row = DeploymentBootORM(
                    deployment_key=deployment_key, generation=1, last_boot_id=boot_id, updated_at=now
                )
                s.add(row)
                try:
                    s.commit()
                    return 1
                except IntegrityError:
                    s.rollback()
                    row = s.scalar(
                        select(DeploymentBootORM)
                        .where(DeploymentBootORM.deployment_key == deployment_key)
                        .with_for_update()
                    )
                    if row is None:
                        raise
            row.generation += 1
            row.last_boot_id = boot_id
            row.updated_at = now
            s.commit()
            return row.generation

    def current_deployment_generation(self, deployment_key: str) -> int | None:
        with self.session_factory() as s:
            return s.scalar(
                select(DeploymentBootORM.generation)
                .where(DeploymentBootORM.deployment_key == deployment_key)
            )

    def actions_for_run(self, run_id: int) -> list[ActionORM]:
        with self.session_factory() as s:
            return list(s.scalars(select(ActionORM).where(ActionORM.run_id == run_id).order_by(ActionORM.id)))

    def has_unresolved_effects(self, run_id: int) -> bool:
        unresolved = {ActionState.EXECUTING.value, ActionState.UNKNOWN.value, ActionState.RECONCILING.value}
        with self.session_factory() as s:
            return bool(s.scalar(
                select(func.count(EffectORM.id))
                .join(ActionORM, EffectORM.action_id == ActionORM.id)
                .where(ActionORM.run_id == run_id, EffectORM.state.in_(unresolved))
            ))

    def plans_for_run(self, run_id: int) -> list[PlanORM]:
        with self.session_factory() as s:
            return list(s.scalars(select(PlanORM).where(PlanORM.run_id == run_id).order_by(PlanORM.id)))

    def list_runs_for_actor(self, actor: str, *, limit: int = 50) -> list[RunORM]:
        with self.session_factory() as s:
            return list(s.scalars(
                select(RunORM)
                .where(RunORM.owner_actor == actor)
                .order_by(RunORM.id.desc())
                .limit(limit)
            ))

    def get_run_for_actor(self, run_id: int, actor: str) -> RunORM | None:
        with self.session_factory() as s:
            return s.scalar(select(RunORM).where(RunORM.id == run_id, RunORM.owner_actor == actor))

    def approvals_for_run(self, run_id: int) -> list[ApprovalORM]:
        with self.session_factory() as s:
            return list(s.scalars(select(ApprovalORM).where(ApprovalORM.run_id == run_id).order_by(ApprovalORM.id)))

    def claim_api_request(
        self, *, actor: str, route: str, idempotency_key: str, request_hash: str,
        resource_id: str | None = None, lease_seconds: int = 60,
    ) -> tuple[ApiRequestORM, bool]:
        if not actor.strip() or not route.strip() or not idempotency_key.strip():
            raise ValueError("actor, route and idempotency key are required")
        if len(idempotency_key) > 128:
            raise ValueError("idempotency key is too long")
        now = self.now()
        with self.session_factory() as s:
            existing = s.scalar(select(ApiRequestORM).where(
                ApiRequestORM.actor == actor, ApiRequestORM.route == route,
                ApiRequestORM.idempotency_key == idempotency_key,
            ).with_for_update())
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ApiIdempotencyConflict("idempotency key reused with a different request")
                if existing.state == "IN_PROGRESS" and existing.lease_expires_at is not None and _aware(existing.lease_expires_at) <= now:
                    existing.state = "UNKNOWN"
                    existing.updated_at = now
                    s.commit()
                    s.refresh(existing)
                return existing, False
            row = ApiRequestORM(
                actor=actor, route=route, idempotency_key=idempotency_key,
                request_hash=request_hash, state="IN_PROGRESS", resource_id=resource_id,
                created_at=now, updated_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            s.add(row)
            try:
                s.commit()
                s.refresh(row)
                return row, True
            except IntegrityError:
                s.rollback()
                existing = s.scalar(select(ApiRequestORM).where(
                    ApiRequestORM.actor == actor, ApiRequestORM.route == route,
                    ApiRequestORM.idempotency_key == idempotency_key,
                ))
                if existing is None:
                    raise
                if existing.request_hash != request_hash:
                    raise ApiIdempotencyConflict("idempotency key reused with a different request")
                return existing, False

    def reclaim_unknown_api_request(
        self, request_id: int, *, lease_seconds: int = 60, allow_bound_resource: bool = False,
    ) -> ApiRequestORM:
        now = self.now()
        with self.session_factory() as s:
            row = s.scalar(select(ApiRequestORM).where(ApiRequestORM.id == request_id).with_for_update())
            if row is None:
                raise KeyError(request_id)
            if row.state != "UNKNOWN":
                raise ApiIdempotencyConflict("only UNKNOWN requests may be reclaimed")
            if row.resource_id is not None and not allow_bound_resource:
                raise ApiIdempotencyConflict("resource-bound UNKNOWN request requires operation-specific recovery proof")
            # Runs created by create_run carry api_request_id.  If one exists, absence is
            # not proven and the request must not be replayed.  Mutation routes bind a
            # pre-existing run and pass allow_bound_resource=True after their own proof.
            if row.route == "create_run" and s.scalar(select(func.count(RunORM.id)).where(RunORM.api_request_id == request_id)):
                raise ApiIdempotencyConflict("request already created a durable run")
            row.state = "IN_PROGRESS"
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            s.commit()
            s.refresh(row)
            return row

    def update_api_request_resource(self, request_id: int, resource_id: str) -> ApiRequestORM:
        with self.session_factory() as s:
            row = s.scalar(select(ApiRequestORM).where(ApiRequestORM.id == request_id).with_for_update())
            if row is None:
                raise KeyError(request_id)
            if row.resource_id is not None and row.resource_id != resource_id:
                raise ApiIdempotencyConflict("idempotency resource drift")
            row.resource_id = resource_id
            row.updated_at = self.now()
            s.commit()
            s.refresh(row)
            return row

    def complete_api_request(self, request_id: int, *, status_code: int, response_json: str, resource_id: str | None = None) -> ApiRequestORM:
        with self.session_factory() as s:
            row = s.scalar(select(ApiRequestORM).where(ApiRequestORM.id == request_id).with_for_update())
            if row is None:
                raise KeyError(request_id)
            if row.state == "COMPLETED":
                if row.status_code != status_code or row.response_json != response_json:
                    raise ApiIdempotencyConflict("completed idempotent response drift")
                return row
            if resource_id is not None:
                if row.resource_id is not None and row.resource_id != resource_id:
                    raise ApiIdempotencyConflict("idempotency resource drift")
                row.resource_id = resource_id
            row.state = "COMPLETED"
            row.status_code = status_code
            row.response_json = response_json
            row.updated_at = self.now()
            row.lease_expires_at = None
            s.commit()
            s.refresh(row)
            return row

    def get_api_request(self, *, actor: str, route: str, idempotency_key: str) -> ApiRequestORM | None:
        with self.session_factory() as s:
            return s.scalar(select(ApiRequestORM).where(
                ApiRequestORM.actor == actor,
                ApiRequestORM.route == route,
                ApiRequestORM.idempotency_key == idempotency_key,
            ))
