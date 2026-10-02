from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.adapters.bounded_rule_planner import BoundedRulePlanner
from app.adapters.fake_business import FakeCRM, FakeGmailRead
from app.adapters.fake_gmail import FakeGmailProvider
from app.api.app import ApiRuntime, ApiSettings, create_app
from app.domain.models import CustomerRecord
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import RenewalWorkflowV08
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def run_demo(database_path: Path) -> dict[str, int | str]:
    """Run the authenticated API demonstration and release all DB ownership.

    This function deliberately owns both the TestClient lifespan and the
    SQLAlchemy engine.  Windows cannot remove an open SQLite database, so the
    ownership order is part of the demo's release contract.
    """
    engine = make_engine(f"sqlite:///{database_path}")
    client: TestClient | None = None
    try:
        create_schema(engine)
        repo = LedgerRepository(make_session_factory(engine), now_fn=lambda: NOW)
        crm = FakeCRM([CustomerRecord(
            customer_id="C001", company="ACME", contact_email="alice@acme.com",
            renewal_id="R2026-C001", renewal_status="pending",
            renewal_date=NOW + timedelta(days=14), version=1,
        )])
        gmail = FakeGmailProvider()
        approvals = ApprovalService(repo, now_fn=lambda: NOW)
        effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2, now_fn=lambda: NOW)
        workflow = RenewalWorkflowV08(
            repo=repo, crm=crm, gmail_read=FakeGmailRead(),
            planning=PlanningService(repo, BoundedRulePlanner()),
            consequential=ConsequentialActionService(repo, effects, approvals),
            approvals=approvals, coordinator=RunCoordinator(repo), now_fn=lambda: NOW,
        )
        app = create_app(
            runtime=ApiRuntime(workflow=workflow, repo=repo, approvals=approvals),
            settings=ApiSettings(
                secret_key="0123456789abcdef0123456789abcdef",
                demo_password="strong-demo-password",
                actor="judge",
                allowed_origin="http://testserver",
            ),
        )
        with TestClient(app) as client:
            login = client.post("/api/session", json={"password":"strong-demo-password"})
            csrf = login.json()["csrf_token"]
            h = lambda key: {"X-CSRF-Token": csrf, "Idempotency-Key": key}
            created = client.post("/api/runs", headers=h("create-1"), json={
                "goal_text":"Verify ACME renewal outreach",
                "company":"ACME",
                "deadline":(NOW+timedelta(days=7)).isoformat(),
            })
            cp = created.json()
            approved = client.post(f"/api/runs/{cp['run_id']}/approve", headers=h("approve-1"), json={"action_id":cp["action_id"]}).json()
            body={"action_id":cp["action_id"],"approval_id":approved["approval_id"]}
            first = client.post(f"/api/runs/{cp['run_id']}/execute", headers=h("execute-1"), json=body)
            replay = client.post(f"/api/runs/{cp['run_id']}/execute", headers=h("execute-1"), json=body)
            view = client.get(f"/api/runs/{cp['run_id']}").json()
            print("session actor       ", login.json()["actor"])
            print("run owner/state     ", view["run"]["id"], view["run"]["state"])
            print("execute status      ", first.json()["run_state"])
            print("idempotent replay   ", replay.headers.get("Idempotent-Replay"))
            print("physical messages   ", len(gmail.messages))
            print("evidence rows       ", len(view["evidence"]))
            print("timeline events     ", len(view["timeline"]))
            assert replay.headers.get("Idempotent-Replay") == "true"
            assert len(gmail.messages) == 1
            assert view["run"]["state"] == "COMPLETED_VERIFIED"
            print("PASS: authenticated API + CSRF + idempotent execute + one external effect")
            return {"state": view["run"]["state"], "messages": len(gmail.messages), "evidence": len(view["evidence"])}
    finally:
        # TestClient exits before engine disposal; neither failure nor success
        # may leave a SQLite handle open when TemporaryDirectory removes it.
        engine.dispose()


def main() -> int:
    with TemporaryDirectory() as td:
        run_demo(Path(td) / "api-demo.sqlite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
