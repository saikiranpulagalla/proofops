from app.planning.models import PlanProposal, PlanningContext


class FakePlanner:
    model_name = "fake-planner-v0.5"

    def __init__(self, proposal: PlanProposal):
        self.proposal = proposal
        self.calls = 0

    def propose(self, context: PlanningContext) -> PlanProposal:
        self.calls += 1
        return self.proposal
