from datetime import datetime, timedelta, timezone

from app.domain.enums import RunState
from app.domain.models import EvidenceFact, GoalContract, TemporalCondition
from app.engine.completion import CompletionInput, evaluate_goal


def contract(deadline=None, max_age=3600):
    return GoalContract(
        subject_customer_id="C001",
        subject_renewal_id="R1",
        required_conditions=("renewal_state_known", "followup_handled"),
        forbidden_conditions=("do_not_contact_violation",),
        evidence_requirements=("crm", "communication"),
        temporal=TemporalCondition(deadline=deadline, evidence_max_age_seconds=max_age),
    )


def evidence(kind, *, age_seconds=0, customer="C001", renewal="R1", verified=True, future_seconds=0, observed_at=None):
    now = datetime.now(timezone.utc)
    return EvidenceFact(
        evidence_type=kind,
        source_ref=f"{kind}:1",
        customer_id=customer,
        renewal_id=renewal,
        observed_at=observed_at or (now - timedelta(seconds=age_seconds) + timedelta(seconds=future_seconds)),
        verified=verified,
    )


def complete_input(**kwargs):
    now = kwargs.pop("now", datetime.now(timezone.utc))
    return CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm"), evidence("communication")),
        conditions_satisfied_at=kwargs.pop("conditions_satisfied_at", now),
        now=now,
        **kwargs,
    )


def test_verified_completion_requires_conditions_and_evidence():
    assert evaluate_goal(contract(), complete_input()) == RunState.COMPLETED_VERIFIED


def test_unknown_effect_prevents_completion():
    assert evaluate_goal(contract(), complete_input(has_unknown_effects=True)) == RunState.UNKNOWN


def test_forbidden_condition_blocks():
    state = complete_input()
    state = CompletionInput(
        satisfied_conditions=state.satisfied_conditions,
        active_forbidden_conditions=frozenset({"do_not_contact_violation"}),
        evidence=state.evidence,
        conditions_satisfied_at=state.conditions_satisfied_at,
        now=state.now,
    )
    assert evaluate_goal(contract(), state) == RunState.BLOCKED_NEEDS_HUMAN


def test_deadline_is_enforced_when_completion_occurs_late():
    now = datetime.now(timezone.utc)
    assert evaluate_goal(
        contract(now - timedelta(seconds=10)),
        complete_input(now=now, conditions_satisfied_at=now - timedelta(seconds=1)),
    ) == RunState.FAILED


def test_completion_proven_before_deadline_stays_valid_when_evaluated_later():
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(seconds=5)
    proof_time = deadline - timedelta(seconds=5)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", observed_at=proof_time), evidence("communication", observed_at=proof_time)),
        conditions_satisfied_at=proof_time,
        now=now,
    )
    assert evaluate_goal(contract(deadline, max_age=3600), state) == RunState.COMPLETED_VERIFIED


def test_stale_evidence_cannot_complete():
    now = datetime.now(timezone.utc)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", age_seconds=7200), evidence("communication")),
        conditions_satisfied_at=now,
        now=now,
    )
    assert evaluate_goal(contract(max_age=3600), state) == RunState.UNKNOWN


def test_future_evidence_beyond_skew_cannot_complete():
    now = datetime.now(timezone.utc)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", future_seconds=600), evidence("communication")),
        conditions_satisfied_at=now,
        now=now,
    )
    assert evaluate_goal(contract(), state) == RunState.UNKNOWN


def test_wrong_customer_evidence_cannot_complete():
    now = datetime.now(timezone.utc)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", customer="C999"), evidence("communication")),
        conditions_satisfied_at=now,
        now=now,
    )
    assert evaluate_goal(contract(), state) == RunState.UNKNOWN


def test_goal_contract_is_deeply_immutable():
    c = contract()
    try:
        c.temporal.evidence_max_age_seconds = 1
    except Exception:
        pass
    assert c.temporal.evidence_max_age_seconds == 3600


def test_unscoped_evidence_cannot_complete_scoped_goal():
    now = datetime.now(timezone.utc)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", customer=None), evidence("communication")),
        conditions_satisfied_at=now,
        now=now,
    )
    assert evaluate_goal(contract(), state) == RunState.UNKNOWN


def test_wrong_renewal_evidence_cannot_complete():
    now = datetime.now(timezone.utc)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm", renewal="R0"), evidence("communication")),
        conditions_satisfied_at=now,
        now=now,
    )
    assert evaluate_goal(contract(), state) == RunState.UNKNOWN


def test_evidence_after_deadline_cannot_be_backdated_by_conditions_timestamp():
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(minutes=1)
    state = CompletionInput(
        satisfied_conditions=frozenset({"renewal_state_known", "followup_handled"}),
        active_forbidden_conditions=frozenset(),
        evidence=(evidence("crm"), evidence("communication")),
        conditions_satisfied_at=deadline - timedelta(seconds=10),
        now=now,
    )
    assert evaluate_goal(contract(deadline, max_age=3600), state) == RunState.FAILED
