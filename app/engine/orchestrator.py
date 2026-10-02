from __future__ import annotations

from datetime import datetime, timezone

from app.domain.enums import RunState
from app.domain.models import GoalContract
from app.engine.completion import CompletionInput, evaluate_goal
from app.persistence.repository import LedgerRepository


class RunCoordinator:
    """Durable run-finalization boundary.

    Completion is derived from GoalContract + durable evidence; callers cannot simply
    assign a terminal state because repository transitions enforce the run state machine.
    """

    def __init__(self, repo: LedgerRepository):
        self.repo = repo

    def finalize(
        self,
        *,
        run_id: int,
        satisfied_conditions: frozenset[str],
        active_forbidden_conditions: frozenset[str] = frozenset(),
        has_unknown_effects: bool = False,
        no_action_required: bool = False,
        conditions_satisfied_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RunState:
        now = now or datetime.now(timezone.utc)
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if not run.goal_contract_json:
            raise RuntimeError("run has no persisted GoalContract")
        contract = GoalContract.model_validate_json(run.goal_contract_json)
        state = CompletionInput(
            satisfied_conditions=satisfied_conditions,
            active_forbidden_conditions=active_forbidden_conditions,
            evidence=tuple(self.repo.evidence_for_run(run_id)),
            has_unknown_effects=has_unknown_effects,
            no_action_required=no_action_required,
            conditions_satisfied_at=conditions_satisfied_at,
            now=now,
        )
        target = evaluate_goal(contract, state)
        current = RunState(run.state)
        if current == target:
            return target
        self.repo.transition_run_state(run_id, target, expected=current)
        return target
