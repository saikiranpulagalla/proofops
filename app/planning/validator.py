from __future__ import annotations

from collections import Counter

from app.domain.enums import EmailIntent, RiskLevel
from app.planning.models import (
    CompiledAction,
    CompiledPlan,
    PlanActionType,
    PlanIssue,
    PlanProposal,
    PlanValidationReport,
    PlanningContext,
)


CONSEQUENTIAL = {
    PlanActionType.SEND_RENEWAL_OUTREACH,
    PlanActionType.UPDATE_CRM_STATUS,
    PlanActionType.SCHEDULE_FOLLOWUP,
}
TERMINAL_DECISIONS = {
    PlanActionType.REQUEST_HUMAN_REVIEW,
    PlanActionType.DEFER,
    PlanActionType.NO_ACTION_REQUIRED,
}

# Conditions are deterministic capabilities of actions, not claims supplied by the LLM.
ACTION_CONDITIONS: dict[PlanActionType, frozenset[str]] = {
    PlanActionType.SEND_RENEWAL_OUTREACH: frozenset({"outreach_sent", "followup_handled"}),
    PlanActionType.VERIFY_OUTREACH: frozenset({"outreach_verified"}),
    PlanActionType.REQUEST_HUMAN_REVIEW: frozenset({"human_review_requested"}),
    PlanActionType.DEFER: frozenset({"deferred"}),
    PlanActionType.NO_ACTION_REQUIRED: frozenset({"no_action_required"}),
    PlanActionType.UPDATE_CRM_STATUS: frozenset({"crm_updated"}),
    PlanActionType.SCHEDULE_FOLLOWUP: frozenset({"followup_scheduled", "followup_handled"}),
    PlanActionType.PREPARE_RENEWAL_OUTREACH: frozenset(),
    PlanActionType.REQUEST_APPROVAL: frozenset(),
}


class PlanRejected(ValueError):
    def __init__(self, report: PlanValidationReport):
        self.report = report
        super().__init__("; ".join(i.code for i in report.issues) or "plan rejected")


def _issue(issues: list[PlanIssue], code: str, message: str, step_id: str | None = None) -> None:
    issues.append(PlanIssue(code=code, message=message, step_id=step_id))


class PlanValidator:
    def validate(self, context: PlanningContext, proposal: PlanProposal) -> PlanValidationReport:
        issues: list[PlanIssue] = []
        steps = list(proposal.steps)
        ids = {s.step_id for s in steps}
        known_evidence = set(context.evidence_refs)
        actions = [s.action for s in steps]
        counts = Counter(actions)

        # Structural graph checks.
        index = {s.step_id: i for i, s in enumerate(steps)}
        for step in steps:
            if step.action not in context.available_actions:
                _issue(issues, "ACTION_NOT_AVAILABLE", f"{step.action} is not enabled in this build", step.step_id)
            unknown = set(step.evidence_refs) - known_evidence
            if unknown:
                _issue(issues, "HALLUCINATED_EVIDENCE", f"unknown evidence refs: {sorted(unknown)}", step.step_id)
            for dep in step.depends_on:
                if dep not in ids:
                    _issue(issues, "UNKNOWN_DEPENDENCY", f"dependency {dep!r} does not exist", step.step_id)
                elif dep == step.step_id or index[dep] >= index[step.step_id]:
                    _issue(issues, "INVALID_DEPENDENCY_ORDER", f"dependency {dep!r} must precede the step", step.step_id)

        # Plans ending in a deterministic terminal decision must not also perform writes.
        if any(a in TERMINAL_DECISIONS for a in actions) and any(a in CONSEQUENTIAL for a in actions):
            _issue(issues, "TERMINAL_WITH_CONSEQUENTIAL_ACTION", "terminal decision cannot be mixed with a consequential action")
        if sum(counts[a] for a in TERMINAL_DECISIONS) > 1:
            _issue(issues, "MULTIPLE_TERMINAL_DECISIONS", "plan contains conflicting terminal decisions")

        # Model assumptions are allowed for explanation, but not as a basis for side effects.
        if proposal.assumptions and any(a in CONSEQUENTIAL for a in actions):
            _issue(issues, "UNRESOLVED_ASSUMPTIONS", "consequential plan cannot depend on unresolved assumptions")

        snapshot = context.snapshot
        customer = snapshot.customer
        conflicts = set(snapshot.conflicts)

        # Deterministic safety/business constraints override the planner.
        if conflicts and any(a in CONSEQUENTIAL for a in actions):
            _issue(issues, "CONFLICT_REQUIRES_HUMAN", "unresolved business conflict blocks consequential actions")
        if "NO_EXTERNAL_CONTACT" in set(context.goal_constraints) and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "USER_FORBIDS_EXTERNAL_CONTACT", "the user goal explicitly forbids external contact")
        if customer.do_not_contact and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "DO_NOT_CONTACT", "customer is marked do-not-contact")
        if not customer.contact_email and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "MISSING_CONTACT", "outreach requires a verified contact")
        if snapshot.email_intent == EmailIntent.DO_NOT_CONTACT and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "EMAIL_DO_NOT_CONTACT", "recent customer communication prohibits outreach")
        if snapshot.email_intent == EmailIntent.ACCEPTED and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "ALREADY_ACCEPTED", "accepted renewal must not receive initial outreach")
        if snapshot.email_intent == EmailIntent.DECLINED and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "ALREADY_DECLINED", "declined renewal must not receive outreach")
        if snapshot.email_intent == EmailIntent.DELAY and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "CUSTOMER_REQUESTED_DELAY", "delay request blocks outreach")
        if snapshot.meeting_exists and PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            _issue(issues, "FOLLOWUP_ALREADY_EXISTS", "existing renewal follow-up makes initial outreach unnecessary")

        # A send plan is a fixed safety protocol. The LLM may decide that outreach is
        # needed, but it cannot omit approval or verification stages.
        if PlanActionType.SEND_RENEWAL_OUTREACH in actions:
            for required in (
                PlanActionType.PREPARE_RENEWAL_OUTREACH,
                PlanActionType.REQUEST_APPROVAL,
                PlanActionType.VERIFY_OUTREACH,
            ):
                if counts[required] != 1:
                    _issue(issues, "INCOMPLETE_SEND_PROTOCOL", f"send plan requires exactly one {required}")
            if counts[PlanActionType.SEND_RENEWAL_OUTREACH] != 1:
                _issue(issues, "DUPLICATE_SEND", "send plan must contain exactly one logical send")
            required_order = [
                PlanActionType.PREPARE_RENEWAL_OUTREACH,
                PlanActionType.REQUEST_APPROVAL,
                PlanActionType.SEND_RENEWAL_OUTREACH,
                PlanActionType.VERIFY_OUTREACH,
            ]
            if all(a in actions for a in required_order):
                positions = [actions.index(a) for a in required_order]
                if positions != sorted(positions):
                    _issue(issues, "UNSAFE_SEND_ORDER", "prepare -> approval -> send -> verify order is required")
            send_step = next((s for s in steps if s.action == PlanActionType.SEND_RENEWAL_OUTREACH), None)
            if send_step and not send_step.evidence_refs:
                _issue(issues, "CONSEQUENTIAL_WITHOUT_EVIDENCE", "send decision must cite trusted evidence", send_step.step_id)

        # Terminal decisions should be evidence-grounded too; this prevents a model from
        # declaring no-action/defer/human-review without pointing to the observed basis.
        for step in steps:
            if step.action in TERMINAL_DECISIONS and not step.evidence_refs:
                _issue(issues, "DECISION_WITHOUT_EVIDENCE", "terminal decision must cite trusted evidence", step.step_id)

        reachable = set(context.already_satisfied_conditions)
        for action in actions:
            reachable.update(ACTION_CONDITIONS[action])
        missing_conditions = set(context.goal_contract.required_conditions) - reachable
        if missing_conditions:
            _issue(issues, "GOAL_NOT_REACHABLE", f"plan cannot satisfy required conditions: {sorted(missing_conditions)}")

        return PlanValidationReport(valid=not issues, issues=tuple(issues), reachable_conditions=frozenset(reachable))


class PlanCompiler:
    def compile(self, context: PlanningContext, proposal: PlanProposal, report: PlanValidationReport) -> CompiledPlan:
        if not report.valid:
            raise PlanRejected(report)
        actions: list[CompiledAction] = []
        for step in proposal.steps:
            if step.action != PlanActionType.SEND_RENEWAL_OUTREACH:
                continue
            customer = context.snapshot.customer
            if not customer.contact_email:
                raise RuntimeError("validated send lost its trusted contact")
            # Semantic identity, target and risk are deterministic. The LLM never supplies them.
            actions.append(CompiledAction(
                action_type="SEND_EMAIL",
                logical_key=f"{customer.renewal_id}:initial_outreach",
                customer_id=customer.customer_id,
                renewal_id=customer.renewal_id,
                purpose="INITIAL_RENEWAL_OUTREACH",
                target=customer.contact_email,
                risk=RiskLevel.EXTERNAL_WRITE.value,
                evidence_refs=step.evidence_refs,
            ))
        return CompiledPlan(proposal=proposal, actions=tuple(actions))
