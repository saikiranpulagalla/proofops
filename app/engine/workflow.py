from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.enums import ActionState, EmailIntent, RenewalStatus, RunState
from app.domain.models import BusinessSnapshot, CustomerRecord, EmailObservation, GoalContract, TemporalCondition, utcnow
from app.engine.completion import CompletionInput, evaluate_goal
from app.engine.consequential import ConsequentialActionService
from app.engine.deterministic import normalize_renewal_status
from app.engine.goal_intent import GoalIntent, GoalSubjectState, compile_goal_intent
from app.engine.orchestrator import RunCoordinator
from app.engine.truth import interpret_email_evidence
from app.persistence.repository import LedgerRepository
from app.planning.models import CompiledPlan, PlanActionType, PlanningContext
from app.planning.service import PlanningService
from app.ports.protocols import ProviderError
from app.security.approval import ApprovalContinuationExpired, ApprovalService
from app.security.policy import EmailPolicyContext


def _json_hash(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _customer_source_ref(customer: CustomerRecord) -> str:
    # CRM version is useful metadata, but external editors may fail to increment it.
    # Content-address the observation so same-version semantic drift can be preserved as
    # a new observation instead of colliding with an older immutable evidence source.
    payload = customer.model_dump(mode="json")
    return f"crm:{customer.renewal_id}:v{customer.version}:{_json_hash(payload)[:16]}"


def _observation_source_ref(customer_id: str, observation: EmailObservation) -> str:
    if observation.source_ref:
        source = f"gmail:{observation.source_ref}"
        if observation.source_ref.startswith((
            "authoritative-thread-no-messages:",
            "operator-asserted-no-prior-thread:",
        )) and observation.observed_at is not None:
            return f"{source}@{observation.observed_at.isoformat()}"
        return source
    if observation.observed_at is not None:
        return f"gmail:none:{customer_id}@{observation.observed_at.isoformat()}"
    return f"gmail:none:{customer_id}"


def _snapshot_json(snapshot: BusinessSnapshot) -> str:
    return snapshot.model_dump_json()


def _contract_json(contract: GoalContract) -> str:
    return contract.model_dump_json()


def _dt_iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _material_snapshot_payload(snapshot: BusinessSnapshot) -> dict:
    customer = snapshot.customer
    return {
        "customer_id": customer.customer_id,
        "company": customer.company.strip(),
        "contact_email": customer.contact_email.casefold() if customer.contact_email else None,
        "renewal_id": customer.renewal_id,
        "renewal_status": normalize_renewal_status(customer.renewal_status).value,
        "renewal_date": _dt_iso(customer.renewal_date),
        "do_not_contact": bool(customer.do_not_contact),
        "version": customer.version,
        "email_intent": snapshot.email_intent.value,
        "recent_email_id": snapshot.recent_email_id,
        "conflicts": tuple(sorted(snapshot.conflicts)),
    }


@dataclass(frozen=True)
class WorkflowCheckpoint:
    run_id: int
    run_state: RunState
    action_id: int | None = None
    action_state: ActionState | None = None
    approval_id: int | None = None
    target: str | None = None
    subject: str | None = None
    body: str | None = None
    reasons: tuple[str, ...] = ()


class MaterialStateChanged(RuntimeError):
    pass


class RenewalWorkflowV08:
    """Durable V0.8 composition boundary.

    The model can propose a typed plan, but business identity, evidence, recipient,
    approval, side effects, reconciliation and completion all remain deterministic.
    """

    def __init__(
        self,
        *,
        repo: LedgerRepository,
        crm,
        gmail_read,
        planning: PlanningService,
        consequential: ConsequentialActionService,
        approvals: ApprovalService,
        coordinator: RunCoordinator,
        max_email_age_seconds: int = 30 * 24 * 3600,
        outreach_window_days: int = 45,
        now_fn=utcnow,
    ):
        self.repo = repo
        self.crm = crm
        self.gmail_read = gmail_read
        self.planning = planning
        self.consequential = consequential
        self.approvals = approvals
        self.coordinator = coordinator
        self.max_email_age_seconds = max_email_age_seconds
        self.outreach_window_days = outreach_window_days
        self.now_fn = now_fn
        # This workflow is the composition root for its safety-critical services.  A
        # single clock must govern approval TTLs, execution leases, evidence timestamps,
        # reconciliation windows and deadlines; silently mixing wall-clock and injected
        # clocks can authorize stale work or strand recovery.
        self.repo.now_fn = now_fn
        self.approvals.now_fn = now_fn
        self.consequential.effects.now_fn = now_fn

    def _collect(self, company: str) -> tuple[CustomerRecord, EmailObservation, BusinessSnapshot]:
        now = self.now_fn().astimezone(timezone.utc)
        candidates = self.crm.find_customers(company)
        if not candidates:
            raise MaterialStateChanged("CUSTOMER_NOT_FOUND")
        if len(candidates) != 1:
            raise MaterialStateChanged("CUSTOMER_AMBIGUOUS")
        customer = candidates[0]

        status = normalize_renewal_status(customer.renewal_status)
        if status == RenewalStatus.UNKNOWN:
            raise MaterialStateChanged("UNKNOWN_RENEWAL_STATUS")
        terminal_statuses = {
            RenewalStatus.RENEWED,
            RenewalStatus.DECLINED,
            RenewalStatus.CANCELED,
            RenewalStatus.EXPIRED,
        }
        if status in terminal_statuses:
            # Terminal CRM truth is authoritative for this outreach workflow; a past
            # renewal date is expected and must not downgrade a completed outcome into
            # an "overdue" error.
            observation = EmailObservation(intent=EmailIntent.NONE)
        else:
            if customer.renewal_date is None:
                raise MaterialStateChanged("MISSING_RENEWAL_DATE")
            renewal_date = customer.renewal_date.astimezone(timezone.utc)
            if renewal_date < now:
                raise MaterialStateChanged("RENEWAL_OVERDUE")
            observation = self.gmail_read.latest_observation(
                customer.customer_id,
                renewal_id=customer.renewal_id,
            )
        interpreted = interpret_email_evidence(
            observation,
            max_age_seconds=self.max_email_age_seconds,
            now=now,
        )
        conflicts: list[str] = []
        if interpreted.reason:
            conflicts.append(interpreted.reason)
        if status == RenewalStatus.PENDING and interpreted.intent == EmailIntent.ACCEPTED:
            conflicts.append("CRM_PENDING_EMAIL_ACCEPTED")
        if status == RenewalStatus.PENDING and interpreted.intent == EmailIntent.DECLINED:
            conflicts.append("CRM_PENDING_EMAIL_DECLINED")
        snapshot = BusinessSnapshot(
            customer=customer,
            email_intent=interpreted.intent,
            recent_email_id=observation.message_id,
            meeting_exists=False,
            observed_at=now,
            conflicts=tuple(conflicts),
        )
        return customer, observation, snapshot

    def _contract_for(
        self, snapshot: BusinessSnapshot, *, deadline: datetime | None, prior_confirmed_outreach: bool = False,
        goal_intent: GoalIntent | None = None,
    ) -> GoalContract:
        customer = snapshot.customer
        status = normalize_renewal_status(customer.renewal_status)
        now = self.now_fn().astimezone(timezone.utc)
        # Deterministic user constraints outrank business heuristics and the model.
        if goal_intent is not None and goal_intent.forbid_external_contact:
            required = ("human_review_requested",)
            evidence = ("crm_snapshot", "gmail_observation")
        # Deterministic business semantics decide what proof a plan must eventually satisfy.
        elif status in {RenewalStatus.RENEWED, RenewalStatus.DECLINED, RenewalStatus.CANCELED, RenewalStatus.EXPIRED}:
            required = ("renewal_state_known", "no_action_required")
            evidence = ("crm_snapshot",)
        elif status == RenewalStatus.PENDING and customer.renewal_date and customer.renewal_date.astimezone(timezone.utc) - now > timedelta(days=self.outreach_window_days):
            required = ("renewal_state_known", "deferred")
            evidence = ("crm_snapshot",)
        elif snapshot.conflicts or snapshot.email_intent in {
            EmailIntent.ACCEPTED,
            EmailIntent.DECLINED,
            EmailIntent.DO_NOT_CONTACT,
            EmailIntent.AMBIGUOUS,
        } or customer.do_not_contact:
            required = ("human_review_requested",)
            evidence = ("crm_snapshot", "gmail_observation")
        elif snapshot.email_intent == EmailIntent.DELAY:
            required = ("renewal_state_known", "deferred")
            evidence = ("crm_snapshot", "gmail_observation")
        elif prior_confirmed_outreach:
            # Prior transport proof prevents duplicate outreach, but it does not prove
            # that the renewal itself reached a terminal business outcome.  The correct
            # state is waiting/deferred, not "completed".
            required = ("renewal_state_known", "deferred")
            evidence = ("crm_snapshot", "prior_confirmed_outreach")
        else:
            required = ("renewal_state_known", "followup_handled", "outreach_verified")
            evidence = ("crm_snapshot", "gmail_sent_message")
        return GoalContract(
            subject_customer_id=customer.customer_id,
            subject_renewal_id=customer.renewal_id,
            required_conditions=required,
            forbidden_conditions=(("do_not_contact", "user_forbids_external_contact") if goal_intent and goal_intent.forbid_external_contact else ("do_not_contact",)),
            evidence_requirements=evidence,
            # A verified prior side effect is durable proof for the same renewal; fresh CRM
            # state is still collected on every run, so the prior transport proof itself
            # does not expire merely because 30 days passed.
            temporal=TemporalCondition(
                deadline=deadline,
                evidence_max_age_seconds=None if prior_confirmed_outreach else 30 * 24 * 3600,
            ),
        )

    def _record_business_evidence(self, *, run_id: int, customer: CustomerRecord, observation: EmailObservation) -> tuple[str, ...]:
        crm_ref = _customer_source_ref(customer)
        crm_payload = customer.model_dump(mode="json")
        self.repo.record_evidence(
            run_id=run_id,
            customer_id=customer.customer_id,
            renewal_id=customer.renewal_id,
            evidence_type="crm_snapshot",
            source_ref=crm_ref,
            expected=None,
            observed=_json_hash(crm_payload),
            observed_at=self.now_fn(),
            verified=True,
        )
        gmail_ref = _observation_source_ref(customer.customer_id, observation)
        gmail_payload = observation.model_dump(mode="json")
        gmail_evidence_type = (
            "operator_asserted_no_prior_conversation"
            if observation.provenance == "operator_assertion"
            else "gmail_observation"
        )
        self.repo.record_evidence(
            run_id=run_id,
            customer_id=customer.customer_id,
            renewal_id=customer.renewal_id,
            evidence_type=gmail_evidence_type,
            source_ref=gmail_ref,
            expected=None,
            observed=_json_hash(gmail_payload),
            observed_at=observation.observed_at or self.now_fn(),
            verified=True,
        )
        return crm_ref, gmail_ref

    @staticmethod
    def _draft(customer: CustomerRecord) -> tuple[str, str]:
        subject = f"{customer.company} renewal follow-up"
        body = (
            f"Hello,\n\nThis is a follow-up regarding {customer.company}'s upcoming renewal. "
            "Please let us know if you would like to discuss next steps.\n\nThank you."
        )
        return subject, body

    def prepare(self, *, goal_text: str, company: str, deadline: datetime | None = None, owner_actor: str | None = None, api_request_id: int | None = None) -> WorkflowCheckpoint:
        if not goal_text.strip():
            raise ValueError("goal_text is required")
        if not company.strip():
            raise ValueError("company is required")
        if deadline is not None and deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        goal_intent = compile_goal_intent(goal_text, company)
        init = {
            "company": company.strip(),
            "deadline": deadline.astimezone(timezone.utc).isoformat() if deadline is not None else None,
            "goal_intent": goal_intent.as_json(),
        }
        run = self.repo.create_run(
            goal_text, owner_actor=owner_actor, api_request_id=api_request_id,
            initial_request_json=json.dumps(init, sort_keys=True, separators=(",", ":")),
        )
        return self._resume_initialization(run.id)

    def _load_initial_request(self, run_id: int) -> tuple[str, datetime | None, GoalIntent]:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if not run.initial_request_json:
            raise RuntimeError("run initialization input is unavailable")
        try:
            value = json.loads(run.initial_request_json)
            company = str(value["company"]).strip()
            raw_deadline = value.get("deadline")
            deadline = datetime.fromisoformat(raw_deadline) if raw_deadline else None
            raw_intent = value.get("goal_intent") or {}
            if raw_intent:
                goal_intent = GoalIntent(
                    forbid_external_contact=bool(raw_intent.get("forbid_external_contact", False)),
                    subject_hint=(str(raw_intent.get("subject_hint")).strip() if raw_intent.get("subject_hint") else None),
                    subject_state=GoalSubjectState(
                        raw_intent.get(
                            "subject_state",
                            "CONFLICTS_WITH_STRUCTURED_SUBJECT"
                            if raw_intent.get("subject_conflict", False)
                            else "NO_SUBJECT_ASSERTION",
                        )
                    ),
                )
            else:
                goal_intent = compile_goal_intent(run.goal_text, company)
        except Exception as exc:
            raise RuntimeError("run initialization input is invalid") from exc
        if not company or (deadline is not None and deadline.tzinfo is None):
            raise RuntimeError("run initialization input is invalid")
        return company, deadline, goal_intent

    def _resume_initialization(self, run_id: int) -> WorkflowCheckpoint:
        """Resume a partially-created run from durable initialization input.

        The method is phase-idempotent: CREATED enters RESOLVING; RESOLVING either
        finishes context binding or, if a prior crash already bound immutable context,
        advances directly to PLANNING; PLANNING resumes the planner.
        """
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        state = RunState(run.state)
        if state not in {RunState.CREATED, RunState.RESOLVING, RunState.PLANNING}:
            return WorkflowCheckpoint(run_id, state)
        try:
            company, deadline, goal_intent = self._load_initial_request(run_id)
        except RuntimeError as exc:
            # Legacy pre-RC5 rows may lack the new durable initialization input. Fail
            # closed rather than inventing company/deadline values from the goal text.
            if state == RunState.CREATED:
                self.repo.transition_run_state(run_id, RunState.FAILED, expected=RunState.CREATED)
                return WorkflowCheckpoint(run_id, RunState.FAILED, reasons=("MISSING_DURABLE_INITIALIZATION_INPUT",))
            if state == RunState.RESOLVING:
                self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING)
                return WorkflowCheckpoint(run_id, RunState.BLOCKED_NEEDS_HUMAN, reasons=("MISSING_DURABLE_INITIALIZATION_INPUT",))
            raise exc

        if state == RunState.CREATED:
            self.repo.transition_run_state(run_id, RunState.RESOLVING, expected=RunState.CREATED)
            state = RunState.RESOLVING
            run = self.repo.get_run(run_id)

        if state == RunState.RESOLVING:
            if goal_intent.subject_state != GoalSubjectState.NO_SUBJECT_ASSERTION and goal_intent.subject_state != GoalSubjectState.MATCHES_STRUCTURED_SUBJECT:
                reason = {
                    GoalSubjectState.CONFLICTS_WITH_STRUCTURED_SUBJECT: "GOAL_SUBJECT_CONFLICT",
                    GoalSubjectState.AMBIGUOUS_SUBJECT: "AMBIGUOUS_GOAL_SUBJECT",
                    GoalSubjectState.UNRESOLVED_PLAUSIBLE_SUBJECT: "UNRESOLVED_GOAL_SUBJECT",
                }[goal_intent.subject_state]
                self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING)
                return WorkflowCheckpoint(
                    run_id, RunState.BLOCKED_NEEDS_HUMAN,
                    reasons=(reason,),
                )
            # A crash may occur after immutable context was committed but before the
            # state transition. Do not recollect a different snapshot and violate the
            # already-bound contract; simply continue from the durable checkpoint.
            if run.goal_contract_json and run.snapshot_json and run.subject_renewal_id:
                self.repo.transition_run_state(run_id, RunState.PLANNING, expected=RunState.RESOLVING)
                return self._continue_planning(run_id)

            if deadline is not None and deadline.astimezone(timezone.utc) <= self.now_fn().astimezone(timezone.utc):
                self.repo.transition_run_state(run_id, RunState.FAILED, expected=RunState.RESOLVING)
                return WorkflowCheckpoint(run_id, RunState.FAILED, reasons=("GOAL_DEADLINE_ALREADY_PASSED",))
            try:
                customer, observation, snapshot = self._collect(company)
            except (ProviderError, MaterialStateChanged) as exc:
                self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING)
                return WorkflowCheckpoint(run_id, RunState.BLOCKED_NEEDS_HUMAN, reasons=(str(exc),))

            operation_key = f"renewal:{customer.renewal_id}"
            operation, acquired = self.repo.claim_business_operation(
                operation_key=operation_key, run_id=run_id,
            )
            if not acquired:
                self.repo.transition_run_state(
                    run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING
                )
                return WorkflowCheckpoint(
                    run_id, RunState.BLOCKED_NEEDS_HUMAN,
                    reasons=(f"BUSINESS_OPERATION_IN_PROGRESS_RUN_{operation.owner_run_id}",),
                )

            logical_key = f"{customer.renewal_id}:initial_outreach"
            prior_action = self.repo.get_action(logical_key)
            prior_confirmed = prior_action is not None and ActionState(prior_action.state) == ActionState.CONFIRMED
            if prior_action is not None and not prior_confirmed:
                self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING)
                return WorkflowCheckpoint(
                    run_id, RunState.BLOCKED_NEEDS_HUMAN,
                    reasons=(f"EXISTING_LOGICAL_ACTION_{prior_action.state}",),
                )

            contract = self._contract_for(snapshot, deadline=deadline, prior_confirmed_outreach=prior_confirmed, goal_intent=goal_intent)
            self._record_business_evidence(run_id=run_id, customer=customer, observation=observation)
            if prior_confirmed:
                prior_ref = f"action:{prior_action.id}"
                prior_sent = [
                    e for e in self.repo.evidence_for_run(prior_action.run_id)
                    if e.evidence_type == "gmail_sent_message" and e.verified
                    and e.customer_id == customer.customer_id and e.renewal_id == customer.renewal_id
                ]
                if not prior_sent:
                    self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.RESOLVING)
                    return WorkflowCheckpoint(
                        run_id, RunState.BLOCKED_NEEDS_HUMAN,
                        reasons=("PRIOR_CONFIRMED_ACTION_MISSING_EVIDENCE",),
                    )
                prior_observed_at = max(e.observed_at for e in prior_sent)
                self.repo.record_evidence(
                    run_id=run_id,
                    customer_id=customer.customer_id,
                    renewal_id=customer.renewal_id,
                    evidence_type="prior_confirmed_outreach",
                    source_ref=prior_ref,
                    expected=prior_action.payload_hash,
                    observed=prior_action.payload_hash,
                    observed_at=prior_observed_at,
                    verified=True,
                )
            self.repo.bind_run_context(
                run_id=run_id,
                workflow_type="RENEWAL_OUTREACH_V081",
                subject_customer_id=customer.customer_id,
                subject_renewal_id=customer.renewal_id,
                goal_contract_json=_contract_json(contract),
                snapshot_json=_snapshot_json(snapshot),
            )
            self.repo.transition_run_state(run_id, RunState.PLANNING, expected=RunState.RESOLVING)
            return self._continue_planning(run_id)

        return self._continue_planning(run_id)

    def _continue_planning(self, run_id: int) -> WorkflowCheckpoint:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if RunState(run.state) != RunState.PLANNING:
            raise RuntimeError(f"run is not in PLANNING: {run.state}")
        contract, snapshot = self._load_bound(run_id)
        customer = snapshot.customer
        self.repo.assert_business_operation_owner(
            operation_key=f"renewal:{customer.renewal_id}", run_id=run_id
        )
        evidence_refs = tuple(e.source_ref for e in self.repo.evidence_for_run(run_id))

        already = {"renewal_state_known"}
        _, _, goal_intent = self._load_initial_request(run_id)
        context = PlanningContext(
            goal_contract=contract,
            snapshot=snapshot,
            goal_text=run.goal_text,
            goal_constraints=goal_intent.constraints,
            evidence_refs=evidence_refs,
            already_satisfied_conditions=frozenset(already),
        )
        try:
            compiled = self.planning.propose_and_validate(run_id=run_id, context=context)
        except Exception as exc:
            # Preserve durable PLANNING state and expose an explicit operator-retry path;
            # do not strand the run behind an exception-only recovery mechanism.
            return WorkflowCheckpoint(
                run_id,
                RunState.PLANNING,
                reasons=(f"PLANNER_FAILED:{type(exc).__name__}",),
            )
        # Planner latency must not let a stale worker execute after operation ownership
        # was transferred while it was waiting on the model.  This is an ownership
        # failure, not a planner failure; surface durable truth rather than mislabel it.
        try:
            self.repo.assert_business_operation_owner(
                operation_key=f"renewal:{customer.renewal_id}", run_id=run_id
            )
        except Exception as exc:
            current = self.repo.get_run(run_id)
            return WorkflowCheckpoint(
                run_id, RunState(current.state), reasons=(f"BUSINESS_OPERATION_OWNERSHIP_LOST:{type(exc).__name__}",)
            )
        terminal = self._terminal_plan(run_id, compiled)
        if terminal is not None:
            return terminal

        if len(compiled.actions) != 1:
            self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.PLANNING)
            return WorkflowCheckpoint(run_id, RunState.BLOCKED_NEEDS_HUMAN, reasons=("EXPECTED_ONE_COMPILED_ACTION",))

        self.repo.transition_run_state(run_id, RunState.EXECUTING, expected=RunState.PLANNING)
        action = compiled.actions[0]
        subject, body = self._draft(customer)
        policy = EmailPolicyContext.build(
            customer_id=customer.customer_id,
            verified_recipients={customer.contact_email} if customer.contact_email else set(),
            do_not_contact=customer.do_not_contact,
        )
        row, payload = self.consequential.prepare_email(
            run_id=run_id,
            logical_key=action.logical_key,
            customer_id=action.customer_id,
            renewal_id=action.renewal_id,
            purpose=action.purpose,
            to=action.target,
            subject=subject,
            body=body,
            policy=policy,
        )
        return WorkflowCheckpoint(
            run_id,
            RunState.WAITING_APPROVAL,
            action_id=row.id,
            action_state=ActionState(row.state),
            target=payload["to"][0],
            subject=payload["subject"],
            body=payload["body_text"],
        )

    def retry_planning(self, *, run_id: int) -> WorkflowCheckpoint:
        return self._continue_planning(run_id)

    def _terminal_plan(self, run_id: int, compiled: CompiledPlan) -> WorkflowCheckpoint | None:
        actions = [s.action for s in compiled.proposal.steps]
        if PlanActionType.REQUEST_HUMAN_REVIEW in actions:
            self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.PLANNING)
            return WorkflowCheckpoint(run_id, RunState.BLOCKED_NEEDS_HUMAN, reasons=("HUMAN_REVIEW_REQUESTED",))
        if PlanActionType.DEFER in actions:
            self.repo.transition_run_state(run_id, RunState.DEFERRED, expected=RunState.PLANNING)
            return WorkflowCheckpoint(run_id, RunState.DEFERRED, reasons=("DEFERRED_BY_VALIDATED_PLAN",))
        if PlanActionType.NO_ACTION_REQUIRED in actions:
            target = self.coordinator.finalize(
                run_id=run_id,
                satisfied_conditions=frozenset({"renewal_state_known", "no_action_required"}),
                no_action_required=True,
                conditions_satisfied_at=self.now_fn(),
                now=self.now_fn(),
            )
            return WorkflowCheckpoint(run_id, target, reasons=("NO_ACTION_REQUIRED",))
        return None

    def approve(self, *, run_id: int, action_id: int, actor: str, ttl_seconds: int = 600, api_request_id: int | None = None) -> WorkflowCheckpoint:
        run = self.repo.get_run(run_id)
        action = self.repo.get_action_by_id(action_id)
        if run is None or action is None or action.run_id != run_id:
            raise KeyError(run_id, action_id)
        if RunState(run.state) != RunState.WAITING_APPROVAL:
            raise RuntimeError(f"run is not waiting for approval: {run.state}")
        approval = self.approvals.approve(
            run_id=run_id,
            action_id=action_id,
            payload_hash=action.payload_hash,
            actor=actor,
            ttl_seconds=ttl_seconds,
            api_request_id=api_request_id,
        )
        action = self.repo.get_action_by_id(action_id)
        return WorkflowCheckpoint(
            run_id,
            RunState.WAITING_APPROVAL,
            action_id=action_id,
            action_state=ActionState(action.state),
            approval_id=approval.id,
        )

    def _load_bound(self, run_id: int) -> tuple[GoalContract, BusinessSnapshot]:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if not run.goal_contract_json or not run.snapshot_json:
            raise RuntimeError("run context is incomplete")
        return GoalContract.model_validate_json(run.goal_contract_json), BusinessSnapshot.model_validate_json(run.snapshot_json)

    def _refresh_before_effect(self, *, run_id: int, approval_id: int) -> tuple[BusinessSnapshot, EmailPolicyContext]:
        contract, old_snapshot = self._load_bound(run_id)
        if contract.temporal.deadline and self.now_fn().astimezone(timezone.utc) > contract.temporal.deadline:
            approval = self.repo.get_approval(approval_id)
            action = self.repo.get_action_by_id(approval.action_id) if approval else None
            if action is not None and ActionState(action.state) == ActionState.RETRYABLE:
                self.repo.terminate_retryable_continuation(
                    run_id=run_id, action_id=action.id, reason="GOAL_DEADLINE_EXPIRED_BEFORE_RETRY",
                    run_target=RunState.FAILED,
                )
            elif action is not None and ActionState(action.state) == ActionState.PREPARED and not self.repo.effects_for_action(action.id):
                self.repo.terminate_prepared_without_effect(
                    run_id=run_id, action_id=action.id, approval_id=approval_id,
                    reason="GOAL_DEADLINE_EXPIRED_BEFORE_EFFECT", run_target=RunState.FAILED,
                    now=self.now_fn(),
                )
            else:
                try:
                    self.approvals.revoke(approval_id)
                except Exception:
                    pass
                run = self.repo.get_run(run_id)
                if run and RunState(run.state) == RunState.WAITING_APPROVAL:
                    self.repo.transition_run_state(run_id, RunState.FAILED, expected=RunState.WAITING_APPROVAL)
            raise MaterialStateChanged("GOAL_DEADLINE_EXPIRED_BEFORE_EFFECT")
        old = old_snapshot.customer
        current = self.crm.get_customer(old.customer_id)
        if current is None:
            self._invalidate_for_drift(run_id, approval_id, "CUSTOMER_DISAPPEARED")
        assert current is not None
        observation = self.gmail_read.latest_observation(
            current.customer_id,
            renewal_id=current.renewal_id,
        )
        interpreted = interpret_email_evidence(observation, max_age_seconds=self.max_email_age_seconds, now=self.now_fn())
        conflicts: list[str] = []
        if interpreted.reason:
            conflicts.append(interpreted.reason)
        status = normalize_renewal_status(current.renewal_status)
        if status == RenewalStatus.PENDING and interpreted.intent == EmailIntent.ACCEPTED:
            conflicts.append("CRM_PENDING_EMAIL_ACCEPTED")
        if status == RenewalStatus.PENDING and interpreted.intent == EmailIntent.DECLINED:
            conflicts.append("CRM_PENDING_EMAIL_DECLINED")
        new_snapshot = BusinessSnapshot(
            customer=current,
            email_intent=interpreted.intent,
            recent_email_id=observation.message_id,
            meeting_exists=False,
            observed_at=self.now_fn(),
            conflicts=tuple(conflicts),
        )

        # The CRM version is only one drift signal.  Safety must not depend on every
        # external editor incrementing it correctly, so compare the full business
        # meaning that the approved draft depended on.
        material = _material_snapshot_payload(new_snapshot) != _material_snapshot_payload(old_snapshot)

        # Re-evaluate time-dependent business eligibility even when no field changed.
        # A renewal that was actionable at approval time may become overdue before send.
        if status == RenewalStatus.UNKNOWN:
            self.repo.update_run_snapshot(run_id=run_id, snapshot_json=_snapshot_json(new_snapshot))
            self._record_business_evidence(run_id=run_id, customer=current, observation=observation)
            self._invalidate_for_drift(run_id, approval_id, "UNKNOWN_RENEWAL_STATUS")
        if status == RenewalStatus.PENDING:
            if current.renewal_date is None:
                self.repo.update_run_snapshot(run_id=run_id, snapshot_json=_snapshot_json(new_snapshot))
                self._record_business_evidence(run_id=run_id, customer=current, observation=observation)
                self._invalidate_for_drift(run_id, approval_id, "MISSING_RENEWAL_DATE")
            renewal_date = current.renewal_date.astimezone(timezone.utc)
            now = self.now_fn().astimezone(timezone.utc)
            if renewal_date < now:
                self.repo.update_run_snapshot(run_id=run_id, snapshot_json=_snapshot_json(new_snapshot))
                self._record_business_evidence(run_id=run_id, customer=current, observation=observation)
                self._invalidate_for_drift(run_id, approval_id, "RENEWAL_OVERDUE")
            if renewal_date - now > timedelta(days=self.outreach_window_days):
                self.repo.update_run_snapshot(run_id=run_id, snapshot_json=_snapshot_json(new_snapshot))
                self._record_business_evidence(run_id=run_id, customer=current, observation=observation)
                self._invalidate_for_drift(run_id, approval_id, "RENEWAL_OUTSIDE_OUTREACH_WINDOW")
        if material:
            self.repo.update_run_snapshot(run_id=run_id, snapshot_json=_snapshot_json(new_snapshot))
            self._record_business_evidence(run_id=run_id, customer=current, observation=observation)
            self._invalidate_for_drift(run_id, approval_id, "MATERIAL_EVIDENCE_CHANGED")
        # Keep contract identity as an explicit guard even though snapshot comparison above covers it.
        if contract.subject_customer_id != current.customer_id or contract.subject_renewal_id != current.renewal_id:
            self._invalidate_for_drift(run_id, approval_id, "GOAL_SUBJECT_CHANGED")
        return new_snapshot, EmailPolicyContext.build(
            customer_id=current.customer_id,
            verified_recipients={current.contact_email} if current.contact_email else set(),
            do_not_contact=current.do_not_contact,
        )

    def _invalidate_for_drift(self, run_id: int, approval_id: int, reason: str):
        approval = self.repo.get_approval(approval_id)
        action = self.repo.get_action_by_id(approval.action_id) if approval else None
        if action is not None and ActionState(action.state) == ActionState.RETRYABLE:
            self.repo.terminate_retryable_continuation(
                run_id=run_id, action_id=action.id, reason=reason, run_target=RunState.BLOCKED_NEEDS_HUMAN
            )
        elif action is not None and ActionState(action.state) == ActionState.PREPARED and not self.repo.effects_for_action(action.id):
            self.repo.terminate_prepared_without_effect(
                run_id=run_id, action_id=action.id, approval_id=approval_id,
                reason=reason, run_target=RunState.BLOCKED_NEEDS_HUMAN, now=self.now_fn(),
            )
        else:
            try:
                self.approvals.revoke(approval_id)
            except Exception:
                pass
            run = self.repo.get_run(run_id)
            if run and RunState(run.state) == RunState.WAITING_APPROVAL:
                self.repo.transition_run_state(run_id, RunState.BLOCKED_NEEDS_HUMAN, expected=RunState.WAITING_APPROVAL)
        raise MaterialStateChanged(reason)

    def execute(self, *, run_id: int, action_id: int, approval_id: int, actor: str, api_request_id: int | None = None) -> WorkflowCheckpoint:
        run = self.repo.get_run(run_id)
        action = self.repo.get_action_by_id(action_id)
        if run is None or action is None or action.run_id != run_id:
            raise KeyError(run_id, action_id)
        if ActionState(action.state) == ActionState.CONFIRMED:
            if RunState(run.state) in {RunState.COMPLETED_VERIFIED, RunState.COMPLETED_NO_ACTION_REQUIRED}:
                return WorkflowCheckpoint(run_id, RunState(run.state), action_id=action_id, action_state=ActionState.CONFIRMED)
            return self._finalize_confirmed(run_id, action_id)
        _contract, bound_snapshot = self._load_bound(run_id)
        self.repo.assert_business_operation_owner(
            operation_key=f"renewal:{bound_snapshot.customer.renewal_id}", run_id=run_id
        )
        snapshot, policy = self._refresh_before_effect(run_id=run_id, approval_id=approval_id)
        payload = json.loads(action.payload_json)
        result = self.consequential.execute_approved_email(
            approval_id=approval_id,
            actor=actor,
            run_id=run_id,
            logical_key=action.logical_key,
            customer_id=payload["customer_id"],
            renewal_id=payload["renewal_id"],
            purpose=payload["purpose"],
            to=payload["to"][0],
            subject=payload["subject"],
            body=payload["body_text"],
            policy=policy,
            api_request_id=api_request_id,
        )
        state = ActionState(result.state)
        if state == ActionState.CONFIRMED:
            return self._finalize_confirmed(run_id, result.id)
        run = self.repo.get_run(run_id)
        return WorkflowCheckpoint(run_id, RunState(run.state), action_id=result.id, action_state=state, approval_id=approval_id)

    def _finalize_confirmed(self, run_id: int, action_id: int) -> WorkflowCheckpoint:
        _contract, _snapshot = self._load_bound(run_id)
        if not self.consequential.effects.ensure_confirmed_evidence(action_id, now=self.now_fn()):
            run = self.repo.get_run(run_id)
            if run is not None and RunState(run.state) == RunState.EXECUTING:
                self.repo.transition_run_state(run_id, RunState.UNKNOWN, expected=RunState.EXECUTING)
            current = self.repo.get_run(run_id)
            return WorkflowCheckpoint(
                run_id,
                RunState(current.state),
                action_id=action_id,
                action_state=ActionState.CONFIRMED,
                reasons=("CONFIRMED_EFFECT_MISSING_VERIFIED_EVIDENCE",),
            )
        # A previous crash may have left the run UNKNOWN even though the action/effect
        # was already confirmed.  Once durable proof is reconstructed, walk through the
        # legal reconciliation states before evaluating completion.
        current = self.repo.get_run(run_id)
        if current is not None and RunState(current.state) == RunState.UNKNOWN:
            self.repo.transition_run_state(run_id, RunState.RECONCILING, expected=RunState.UNKNOWN)
            self.repo.transition_run_state(run_id, RunState.EXECUTING, expected=RunState.RECONCILING)
        elif current is not None and RunState(current.state) == RunState.RECONCILING:
            self.repo.transition_run_state(run_id, RunState.EXECUTING, expected=RunState.RECONCILING)
        if self.repo.has_unresolved_effects(run_id):
            run = self.repo.get_run(run_id)
            return WorkflowCheckpoint(run_id, RunState(run.state), action_id=action_id, action_state=ActionState.CONFIRMED)
        evidence = self.repo.evidence_for_run(run_id)
        sent = [e for e in evidence if e.evidence_type == "gmail_sent_message" and e.verified]
        satisfied_at = max((e.observed_at for e in sent), default=self.now_fn())
        target = self.coordinator.finalize(
            run_id=run_id,
            satisfied_conditions=frozenset({"renewal_state_known", "followup_handled", "outreach_verified"}),
            has_unknown_effects=False,
            conditions_satisfied_at=satisfied_at,
            now=self.now_fn(),
        )
        action = self.repo.get_action_by_id(action_id)
        return WorkflowCheckpoint(run_id, target, action_id=action_id, action_state=ActionState(action.state))

    def resume(self, *, run_id: int) -> WorkflowCheckpoint:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        actions = self.repo.actions_for_run(run_id)
        if not actions:
            if RunState(run.state) in {RunState.CREATED, RunState.RESOLVING, RunState.PLANNING}:
                return self._resume_initialization(run_id)
            return WorkflowCheckpoint(run_id, RunState(run.state))
        action = actions[-1]
        # Reconcile already-unknown work; expired EXECUTING work is recovered by the effect engine.
        if ActionState(action.state) in {ActionState.UNKNOWN, ActionState.RECONCILING}:
            result = self.consequential.effects.reconcile(action.id)
        elif ActionState(action.state) == ActionState.EXECUTING:
            self.consequential.effects.recover_expired_execution(action.id, now=self.now_fn())
            result = self.repo.get_action_by_id(action.id)
        elif ActionState(action.state) == ActionState.PREPARED and not self.repo.effects_for_action(action.id):
            approval = self.repo.claimed_approval_for_action(action.id)
            if approval is None:
                self.repo.terminate_prepared_without_effect(
                    run_id=run_id, action_id=action.id, approval_id=None,
                    reason="ORPHANED_PREPARED_WITHOUT_CLAIMED_APPROVAL",
                    run_target=RunState.BLOCKED_NEEDS_HUMAN, now=self.now_fn(),
                )
                current = self.repo.get_run(run_id)
                result = self.repo.get_action_by_id(action.id)
                return WorkflowCheckpoint(
                    run_id, RunState(current.state), action_id=result.id,
                    action_state=ActionState(result.state),
                    reasons=("ORPHANED_PREPARED_WITHOUT_CLAIMED_APPROVAL",),
                )
            try:
                return self.execute(
                    run_id=run_id, action_id=action.id, approval_id=approval.id, actor=approval.actor
                )
            except ApprovalContinuationExpired:
                current = self.repo.get_run(run_id)
                result = self.repo.get_action_by_id(action.id)
                return WorkflowCheckpoint(
                    run_id, RunState(current.state), action_id=result.id,
                    action_state=ActionState(result.state),
                    reasons=("PREPARED_CONTINUATION_EXPIRED",),
                )
        else:
            result = action
        if ActionState(result.state) == ActionState.CONFIRMED:
            return self._finalize_confirmed(run_id, result.id)
        current = self.repo.get_run(run_id)
        return WorkflowCheckpoint(run_id, RunState(current.state), action_id=result.id, action_state=ActionState(result.state))
