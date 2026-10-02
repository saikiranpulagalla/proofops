from __future__ import annotations

import hashlib
import json
from typing import Protocol

from app.domain.enums import RunState
from app.planning.errors import PlannerOutputError
from app.planning.models import CompiledPlan, PlanProposal, PlanningContext
from app.planning.validator import PlanCompiler, PlanRejected, PlanValidator
from app.persistence.repository import LedgerRepository


class PlannerPort(Protocol):
    model_name: str
    def propose(self, context: PlanningContext) -> PlanProposal: ...


def planning_input_hash(context: PlanningContext) -> str:
    payload = context.model_dump(mode="json")
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PlanningService:
    """Model proposal boundary.

    It never executes tools and never repairs/retries a rejected model output silently.
    First-attempt quality and safety containment therefore remain measurable.
    """

    def __init__(self, repo: LedgerRepository, planner: PlannerPort, *, validator=None, compiler=None):
        self.repo = repo
        self.planner = planner
        self.validator = validator or PlanValidator()
        self.compiler = compiler or PlanCompiler()

    def propose_and_validate(self, *, run_id: int, context: PlanningContext) -> CompiledPlan:
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if RunState(run.state) != RunState.PLANNING:
            raise RuntimeError(f"run must be PLANNING, found {run.state}")

        input_hash = planning_input_hash(context)
        try:
            proposal = self.planner.propose(context)
        except PlannerOutputError as exc:
            self.repo.record_plan_attempt(
                run_id=run_id,
                model_name=self.planner.model_name,
                input_hash=input_hash,
                proposal_json=exc.raw_output,
                validation_status="REJECTED_SCHEMA",
                issues_json=json.dumps([{"code": "SCHEMA_INVALID", "message": str(exc)}], sort_keys=True),
            )
            raise
        report = self.validator.validate(context, proposal)
        self.repo.record_plan_attempt(
            run_id=run_id,
            model_name=self.planner.model_name,
            input_hash=input_hash,
            proposal_json=proposal.model_dump_json(),
            validation_status="ACCEPTED" if report.valid else "REJECTED",
            issues_json=json.dumps([i.model_dump(mode="json") for i in report.issues], sort_keys=True),
        )
        if not report.valid:
            raise PlanRejected(report)
        return self.compiler.compile(context, proposal, report)
