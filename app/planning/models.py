from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models import BusinessSnapshot, GoalContract


class PlanActionType(StrEnum):
    PREPARE_RENEWAL_OUTREACH = "PREPARE_RENEWAL_OUTREACH"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    SEND_RENEWAL_OUTREACH = "SEND_RENEWAL_OUTREACH"
    VERIFY_OUTREACH = "VERIFY_OUTREACH"
    REQUEST_HUMAN_REVIEW = "REQUEST_HUMAN_REVIEW"
    DEFER = "DEFER"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    # Future capabilities are in the schema so the validator can prove that a model
    # cannot activate functionality that has not been enabled for the current build.
    UPDATE_CRM_STATUS = "UPDATE_CRM_STATUS"
    SCHEDULE_FOLLOWUP = "SCHEDULE_FOLLOWUP"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    action: PlanActionType
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=12)
    depends_on: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("evidence_refs", "depends_on")
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate references are not allowed")
        return value


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretation: str = Field(min_length=1, max_length=800)
    assumptions: tuple[str, ...] = Field(default=(), max_length=8)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_step_ids(self):
        ids = [s.step_id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step_id values must be unique")
        return self


class PlanningContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    goal_contract: GoalContract
    snapshot: BusinessSnapshot
    goal_text: str = ""
    goal_constraints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    already_satisfied_conditions: frozenset[str] = frozenset()
    available_actions: frozenset[PlanActionType] = frozenset({
        PlanActionType.PREPARE_RENEWAL_OUTREACH,
        PlanActionType.REQUEST_APPROVAL,
        PlanActionType.SEND_RENEWAL_OUTREACH,
        PlanActionType.VERIFY_OUTREACH,
        PlanActionType.REQUEST_HUMAN_REVIEW,
        PlanActionType.DEFER,
        PlanActionType.NO_ACTION_REQUIRED,
    })

    @model_validator(mode="after")
    def bind_contract_to_snapshot(self):
        customer = self.snapshot.customer
        if self.goal_contract.subject_customer_id and self.goal_contract.subject_customer_id != customer.customer_id:
            raise ValueError("GoalContract customer does not match planning snapshot")
        if self.goal_contract.subject_renewal_id and self.goal_contract.subject_renewal_id != customer.renewal_id:
            raise ValueError("GoalContract renewal does not match planning snapshot")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("planning evidence_refs must be unique")
        return self


class PlanIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    step_id: str | None = None


class PlanValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    issues: tuple[PlanIssue, ...] = ()
    reachable_conditions: frozenset[str] = frozenset()


class CompiledAction(BaseModel):
    """Trusted semantic action produced from a validated plan.

    Notice what is absent from the model proposal: recipient, logical key and risk are
    derived here from trusted context instead of model-controlled strings.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: str
    logical_key: str
    customer_id: str
    renewal_id: str
    purpose: str
    target: str
    risk: str
    evidence_refs: tuple[str, ...]


class CompiledPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: PlanProposal
    actions: tuple[CompiledAction, ...] = ()
