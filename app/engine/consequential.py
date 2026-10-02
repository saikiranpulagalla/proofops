from __future__ import annotations

from app.domain.enums import ActionState, RunState
from app.engine.effects import EffectExecutor, deterministic_message_id
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalContinuationExpired, ApprovalService
from app.security.canonicalization import canonical_email_payload, canonical_json, semantic_payload_hash
from app.security.policy import EmailPolicyContext, enforce_email_policy


class CrossRunActionError(RuntimeError):
    pass


class ConsequentialActionService:
    """Production boundary for consequential writes.

    Policy is evaluated before persistence, approval is bound to run+actor+payload, and
    the low-level executor receives only an action that has been atomically CLAIMED and
    transitioned to PREPARED.
    """

    def __init__(self, repo: LedgerRepository, effects: EffectExecutor, approvals: ApprovalService):
        self.repo = repo
        self.effects = effects
        self.approvals = approvals

    def prepare_email(
        self, *, run_id: int, logical_key: str, customer_id: str, renewal_id: str,
        purpose: str, to: str, subject: str, body: str, policy: EmailPolicyContext,
    ):
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if RunState(run.state) not in {RunState.EXECUTING, RunState.WAITING_APPROVAL}:
            raise RuntimeError(f"run cannot prepare consequential action from {run.state}")
        payload = canonical_email_payload(
            customer_id=customer_id,
            renewal_id=renewal_id,
            purpose=purpose,
            to=(to,),
            subject=subject,
            body_text=body,
        )
        enforce_email_policy(payload, policy)
        p_hash = semantic_payload_hash(payload)
        action, created = self.repo.get_or_create_action(
            run_id=run_id,
            logical_key=logical_key,
            action_type="SEND_EMAIL",
            target=to,
            payload_hash=p_hash,
            payload_json=canonical_json(payload),
            initial_state=ActionState.PROPOSED,
        )
        if action.run_id != run_id:
            raise CrossRunActionError("logical action already belongs to another run")
        if action.payload_hash != p_hash or action.payload_json != canonical_json(payload):
            raise ValueError("logical action payload drift")
        if created:
            self.repo.transition_action_state(action.id, ActionState.POLICY_CHECKED, expected=ActionState.PROPOSED)
            self.repo.transition_action_state(action.id, ActionState.APPROVAL_REQUIRED, expected=ActionState.POLICY_CHECKED)
            self.repo.transition_run_state(run_id, RunState.WAITING_APPROVAL, expected=RunState.EXECUTING)
            action = self.repo.get_action_by_id(action.id)
        return action, payload

    def execute_approved_email(
        self, *, approval_id: int, actor: str, run_id: int, logical_key: str,
        customer_id: str, renewal_id: str, purpose: str, to: str,
        subject: str, body: str, policy: EmailPolicyContext, api_request_id: int | None = None,
    ):
        action, payload = self.prepare_email(
            run_id=run_id,
            logical_key=logical_key,
            customer_id=customer_id,
            renewal_id=renewal_id,
            purpose=purpose,
            to=to,
            subject=subject,
            body=body,
            policy=policy,
        )
        p_hash = semantic_payload_hash(payload)
        state = ActionState(action.state)
        if state == ActionState.RETRYABLE:
            # A historical RC10 RETRYABLE row may have been produced from an empty
            # provider search.  It is not proof of non-commit and cannot be resumed.
            return self.effects.execute_prepared_email(action_id=action.id, approval_id=approval_id)

        if state == ActionState.PREPARED:
            # PREPARED is retained only as a legacy-RC recovery state; new execution
            # crosses approval+effect start atomically and therefore never persists
            # PREPARED without an effect.
            try:
                self.approvals.verify_continuation(
                    approval_id=approval_id, run_id=run_id, action_id=action.id,
                    payload_hash=p_hash, actor=actor,
                )
            except ApprovalContinuationExpired:
                self.repo.expire_prepared_approval_and_reopen(
                    approval_id=approval_id, run_id=run_id, action_id=action.id,
                    now=self.approvals._now(),
                )
                raise
            return self.effects.execute_prepared_email(action_id=action.id, approval_id=approval_id)

        # First execution crosses authorization and provider-attempt creation in one DB
        # transaction. Gmail is unreachable until a durable EXECUTING effect exists.
        external_key = deterministic_message_id(action.logical_key, action.payload_hash)
        effect = self.approvals.claim_and_start_attempt(
            approval_id=approval_id,
            run_id=run_id,
            action_id=action.id,
            payload_hash=p_hash,
            actor=actor,
            provider="gmail",
            external_key=external_key,
            api_request_id=api_request_id,
            lease_seconds=self.effects.execution_lease_seconds,
        )
        return self.effects.execute_started_email(action_id=action.id, effect_id=effect.id)
