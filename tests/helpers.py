from app.domain.enums import RunState


def advance_run_to_executing(repo, run_id: int):
    repo.transition_run_state(run_id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(run_id, RunState.PLANNING, expected=RunState.RESOLVING)
    return repo.transition_run_state(run_id, RunState.EXECUTING, expected=RunState.PLANNING)
