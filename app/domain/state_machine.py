from dataclasses import dataclass

from .enums import ActionState, RunState


class IllegalTransition(ValueError):
    pass


RUN_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.CREATED: {RunState.RESOLVING, RunState.CANCELED, RunState.FAILED},
    RunState.RESOLVING: {RunState.PLANNING, RunState.BLOCKED_NEEDS_HUMAN, RunState.FAILED, RunState.CANCELED},
    RunState.PLANNING: {RunState.EXECUTING, RunState.BLOCKED_NEEDS_HUMAN, RunState.DEFERRED, RunState.COMPLETED_NO_ACTION_REQUIRED, RunState.FAILED, RunState.CANCELED},
    RunState.EXECUTING: {RunState.WAITING_APPROVAL, RunState.RECONCILING, RunState.COMPLETED_VERIFIED, RunState.COMPLETED_NO_ACTION_REQUIRED, RunState.BLOCKED_NEEDS_HUMAN, RunState.UNKNOWN, RunState.FAILED, RunState.CANCELED},
    RunState.WAITING_APPROVAL: {RunState.EXECUTING, RunState.BLOCKED_NEEDS_HUMAN, RunState.CANCELED, RunState.FAILED},
    RunState.RECONCILING: {RunState.EXECUTING, RunState.COMPLETED_VERIFIED, RunState.COMPLETED_NO_ACTION_REQUIRED, RunState.UNKNOWN, RunState.BLOCKED_NEEDS_HUMAN, RunState.FAILED, RunState.CANCELED},
    RunState.UNKNOWN: {RunState.RECONCILING, RunState.BLOCKED_NEEDS_HUMAN, RunState.FAILED, RunState.CANCELED},
    RunState.DEFERRED: {RunState.RESOLVING, RunState.CANCELED},
    RunState.BLOCKED_NEEDS_HUMAN: {RunState.RESOLVING, RunState.PLANNING, RunState.CANCELED, RunState.FAILED},
    RunState.COMPLETED_VERIFIED: set(),
    RunState.COMPLETED_NO_ACTION_REQUIRED: set(),
    RunState.CANCELED: set(),
    RunState.FAILED: set(),
}


ACTION_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.PROPOSED: {ActionState.POLICY_CHECKED, ActionState.REJECTED, ActionState.CANCELED},
    ActionState.POLICY_CHECKED: {ActionState.APPROVAL_REQUIRED, ActionState.PREPARED, ActionState.REJECTED, ActionState.CANCELED},
    ActionState.APPROVAL_REQUIRED: {ActionState.APPROVED, ActionState.REJECTED, ActionState.CANCELED, ActionState.INVALIDATED},
    ActionState.APPROVED: {ActionState.PREPARED, ActionState.INVALIDATED, ActionState.CANCELED},
    ActionState.PREPARED: {ActionState.EXECUTING, ActionState.CANCELED, ActionState.INVALIDATED},
    ActionState.EXECUTING: {ActionState.CONFIRMED, ActionState.UNKNOWN, ActionState.FAILED},
    ActionState.UNKNOWN: {ActionState.RECONCILING, ActionState.FAILED},
    ActionState.RECONCILING: {ActionState.CONFIRMED, ActionState.RETRYABLE, ActionState.UNKNOWN, ActionState.FAILED},
    # Historical RC10 retryable rows are upgraded into UNKNOWN, never resumed into a
    # second consequential send.  New RC11 reconciliation never emits RETRYABLE.
    ActionState.RETRYABLE: {ActionState.UNKNOWN, ActionState.APPROVAL_REQUIRED, ActionState.CANCELED, ActionState.FAILED},
    ActionState.CONFIRMED: set(),
    ActionState.REJECTED: set(),
    ActionState.FAILED: set(),
    ActionState.CANCELED: set(),
    ActionState.INVALIDATED: {ActionState.APPROVAL_REQUIRED, ActionState.CANCELED},
}


def ensure_run_transition(current: RunState, target: RunState) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise IllegalTransition(f"illegal run transition: {current} -> {target}")


def ensure_action_transition(current: ActionState, target: ActionState) -> None:
    if target not in ACTION_TRANSITIONS[current]:
        raise IllegalTransition(f"illegal action transition: {current} -> {target}")


@dataclass
class StateMachine:
    run_state: RunState = RunState.CREATED

    def transition(self, target: RunState) -> None:
        ensure_run_transition(self.run_state, target)
        self.run_state = target
