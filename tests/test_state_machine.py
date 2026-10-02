import pytest

from app.domain.enums import ActionState, RunState
from app.domain.state_machine import IllegalTransition, ensure_action_transition, ensure_run_transition


def test_run_happy_transition_is_legal():
    ensure_run_transition(RunState.CREATED, RunState.RESOLVING)
    ensure_run_transition(RunState.RESOLVING, RunState.PLANNING)
    ensure_run_transition(RunState.PLANNING, RunState.EXECUTING)
    ensure_run_transition(RunState.EXECUTING, RunState.COMPLETED_VERIFIED)


@pytest.mark.parametrize("source,target", [
    (RunState.CREATED, RunState.COMPLETED_VERIFIED),
    (RunState.UNKNOWN, RunState.EXECUTING),
    (RunState.COMPLETED_VERIFIED, RunState.EXECUTING),
    (RunState.CANCELED, RunState.RESOLVING),
])
def test_illegal_run_transitions_blocked(source, target):
    with pytest.raises(IllegalTransition):
        ensure_run_transition(source, target)


def test_unknown_action_must_reconcile_before_retry():
    with pytest.raises(IllegalTransition):
        ensure_action_transition(ActionState.UNKNOWN, ActionState.EXECUTING)
    ensure_action_transition(ActionState.UNKNOWN, ActionState.RECONCILING)


def test_failed_action_is_terminal_and_not_retryable():
    with pytest.raises(IllegalTransition):
        ensure_action_transition(ActionState.FAILED, ActionState.EXECUTING)
