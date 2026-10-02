from __future__ import annotations

from app.planning.models import PlanActionType, PlanProposal, PlanStep, PlanningContext


class BoundedRulePlanner:
    """Deterministic planner used only for offline V0.8 composition qualification/demo.

    It exercises the exact PlanningService/validator/compiler boundary without pretending
    to qualify live Gemini behavior.
    """

    model_name = "bounded-rule-planner-v0.8"

    def propose(self, context: PlanningContext) -> PlanProposal:
        evidence = tuple(context.evidence_refs)
        required = set(context.goal_contract.required_conditions)
        if "human_review_requested" in required:
            return PlanProposal(
                interpretation="business evidence requires a human decision",
                steps=(PlanStep(step_id="s1", action=PlanActionType.REQUEST_HUMAN_REVIEW,
                                reason="deterministic conflict/safety boundary", evidence_refs=evidence),),
            )
        if "deferred" in required:
            return PlanProposal(
                interpretation="renewal work is deferred",
                steps=(PlanStep(step_id="s1", action=PlanActionType.DEFER,
                                reason="customer/business state requires deferral", evidence_refs=evidence),),
            )
        if "no_action_required" in required:
            return PlanProposal(
                interpretation="renewal already has a terminal outcome",
                steps=(PlanStep(step_id="s1", action=PlanActionType.NO_ACTION_REQUIRED,
                                reason="no external side effect is needed", evidence_refs=evidence),),
            )
        crm_ref = next((r for r in evidence if r.startswith("crm:")), evidence[0])
        return PlanProposal(
            interpretation="one approved, verified renewal outreach is required",
            steps=(
                PlanStep(step_id="s1", action=PlanActionType.PREPARE_RENEWAL_OUTREACH,
                         reason="prepare deterministic outreach", evidence_refs=(crm_ref,)),
                PlanStep(step_id="s2", action=PlanActionType.REQUEST_APPROVAL,
                         reason="external communication requires human approval", evidence_refs=(crm_ref,), depends_on=("s1",)),
                PlanStep(step_id="s3", action=PlanActionType.SEND_RENEWAL_OUTREACH,
                         reason="renewal is due and no terminal response exists", evidence_refs=evidence, depends_on=("s2",)),
                PlanStep(step_id="s4", action=PlanActionType.VERIFY_OUTREACH,
                         reason="prove the intended message exists", evidence_refs=(crm_ref,), depends_on=("s3",)),
            ),
        )
