from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.enums import ActionState, RunState
from app.domain.state_machine import IllegalTransition
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.models import ActionORM
from app.persistence.repository import LedgerRepository


def test_logical_action_unique_under_concurrency(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME renewal")

    def create():
        row, _ = repo.get_or_create_action(
            run_id=run.id,
            logical_key="R2026-C001:initial_outreach",
            action_type="SEND_EMAIL",
            target="alice@acme.com",
            payload_hash="a" * 64,
            payload_json='{"x":1}',
            initial_state=ActionState.PROPOSED,
        )
        return row.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: create(), range(20)))
    assert len(set(ids)) == 1


def test_sqlite_foreign_keys_are_enforced(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fk.sqlite'}")
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        s.add(ActionORM(
            run_id=999,
            logical_key="bad",
            action_type="SEND_EMAIL",
            target="x@example.com",
            payload_hash="a" * 64,
            payload_json='{}',
            state=ActionState.PROPOSED.value,
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_repository_cannot_bypass_action_state_machine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'state.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("run")
    action, _ = repo.get_or_create_action(
        run_id=run.id, logical_key="R1:initial", action_type="SEND_EMAIL",
        target="a@example.com", payload_hash="a"*64, payload_json='{}', initial_state=ActionState.PROPOSED,
    )
    with pytest.raises(IllegalTransition):
        repo.transition_action_state(action.id, ActionState.CONFIRMED)


def test_repository_cannot_bypass_run_state_machine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'runstate.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("run")
    with pytest.raises(IllegalTransition):
        repo.transition_run_state(run.id, RunState.COMPLETED_VERIFIED)
