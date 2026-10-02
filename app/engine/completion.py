from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.enums import RunState
from app.domain.models import EvidenceFact, GoalContract


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("completion timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CompletionInput:
    satisfied_conditions: frozenset[str]
    active_forbidden_conditions: frozenset[str]
    evidence: tuple[EvidenceFact, ...]
    has_unknown_effects: bool = False
    no_action_required: bool = False
    conditions_satisfied_at: datetime | None = None
    now: datetime | None = None


def _valid_evidence(contract: GoalContract, fact: EvidenceFact, now: datetime) -> bool:
    if not fact.verified:
        return False
    if contract.subject_customer_id and fact.customer_id != contract.subject_customer_id:
        return False
    if contract.subject_renewal_id and fact.renewal_id != contract.subject_renewal_id:
        return False
    observed = _aware(fact.observed_at)
    future_skew = (observed - now).total_seconds()
    if future_skew > contract.temporal.max_future_skew_seconds:
        return False
    max_age = contract.temporal.evidence_max_age_seconds
    if max_age is not None and max(0.0, (now - observed).total_seconds()) > max_age:
        return False
    return True


def evaluate_goal(contract: GoalContract, state: CompletionInput) -> RunState:
    now = _aware(state.now or datetime.now(timezone.utc))
    if state.has_unknown_effects:
        return RunState.UNKNOWN
    if any(c in state.active_forbidden_conditions for c in contract.forbidden_conditions):
        return RunState.BLOCKED_NEEDS_HUMAN

    required_satisfied = set(contract.required_conditions).issubset(state.satisfied_conditions)
    if not required_satisfied:
        if contract.temporal.deadline and now > contract.temporal.deadline:
            return RunState.FAILED
        return RunState.UNKNOWN

    required_evidence_times: list[datetime] = []
    for requirement in contract.evidence_requirements:
        candidates = [f for f in state.evidence if f.evidence_type == requirement and _valid_evidence(contract, f, now)]
        if not candidates:
            if contract.temporal.deadline and now > contract.temporal.deadline:
                return RunState.FAILED
            return RunState.UNKNOWN
        required_evidence_times.append(min(_aware(f.observed_at) for f in candidates))

    if contract.temporal.deadline:
        satisfied_at = _aware(state.conditions_satisfied_at) if state.conditions_satisfied_at is not None else None
        # Completion cannot predate the evidence required to prove it. This prevents a caller
        # from back-dating `conditions_satisfied_at` while using evidence observed after the deadline.
        if required_evidence_times:
            evidence_ready_at = max(required_evidence_times)
            satisfied_at = evidence_ready_at if satisfied_at is None else max(satisfied_at, evidence_ready_at)
        if satisfied_at is None:
            if now > contract.temporal.deadline:
                return RunState.FAILED
        elif satisfied_at > contract.temporal.deadline:
            return RunState.FAILED

    if state.no_action_required:
        return RunState.COMPLETED_NO_ACTION_REQUIRED
    return RunState.COMPLETED_VERIFIED
