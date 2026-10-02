#!/usr/bin/env python3
"""Live V0.7 Gmail smoke test.

Safety properties:
- refuses to run unless PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE=YES
- resolves the authenticated Gmail address via getProfile
- sends only to that same authenticated address
- uses a synthetic subject/body and local temporary ledger

Usage:
  PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE=YES python scripts/smoke_v07_gmail_live.py token.json
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.google_auth import GMAIL_MODIFY_SCOPE, load_user_credentials
from app.adapters.google_gmail import GoogleApiGmailTransport, GoogleGmailProvider, build_google_gmail_service
from app.domain.enums import ActionState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from app.domain.enums import RunState


def main() -> int:
    if os.environ.get("PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE") != "YES":
        print("Refusing live send. Set PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE=YES explicitly.", file=sys.stderr)
        return 2
    if len(sys.argv) != 2:
        print("usage: smoke_v07_gmail_live.py TOKEN_JSON", file=sys.stderr)
        return 2

    credentials = load_user_credentials(sys.argv[1], scopes=[GMAIL_MODIFY_SCOPE])
    service = build_google_gmail_service(credentials=credentials)
    profile = service.users().getProfile(userId="me").execute()
    account = str(profile.get("emailAddress") or "").strip().casefold()
    if not account:
        raise SystemExit("Gmail profile did not return emailAddress")

    gmail = GoogleGmailProvider(
        GoogleApiGmailTransport(service),
        sender_email=account,
        contacts={"CSELF": account},
    )

    db = Path(tempfile.gettempdir()) / "proofops-v07-live-gmail.sqlite"
    if db.exists():
        db.unlink()
    engine = make_engine(f"sqlite:///{db}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("V0.7 live Gmail self-send smoke test")
    repo.transition_run_state(run.id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(run.id, RunState.PLANNING, expected=RunState.RESOLVING)
    repo.transition_run_state(run.id, RunState.EXECUTING, expected=RunState.PLANNING)

    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=10, min_absence_checks=2)
    approvals = ApprovalService(repo)
    actions = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="CSELF", verified_recipients={account})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    call = dict(
        run_id=run.id,
        logical_key=f"SMOKE:{stamp}",
        customer_id="CSELF",
        renewal_id=f"SMOKE:{stamp}",
        purpose="V07_LIVE_SMOKE",
        to=account,
        subject=f"[ProofOps V0.7 smoke] {stamp}",
        body="Synthetic ProofOps Gmail integration smoke test. No customer data.",
        policy=policy,
    )
    action, payload = actions.prepare_email(**call)
    approval = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=semantic_payload_hash(payload),
        actor="live-smoke-user",
    )
    result = actions.execute_approved_email(
        approval_id=approval.id,
        actor="live-smoke-user",
        **call,
    )
    if result.state != ActionState.CONFIRMED.value:
        raise SystemExit(f"live Gmail smoke not confirmed: {result.state}")
    evidence = repo.evidence_for_run(run.id)
    if len(evidence) != 1 or not evidence[0].verified:
        raise SystemExit("live Gmail smoke did not persist exactly one verified evidence record")

    print(f"PASS: self-send confirmed for {account}")
    print(f"provider evidence: {evidence[0].source_ref}")
    print("No customer address was used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
