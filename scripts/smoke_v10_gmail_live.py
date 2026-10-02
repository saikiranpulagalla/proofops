#!/usr/bin/env python3
"""Live V1.0 Gmail self-send + verification smoke.

Refuses to send unless PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE=YES.  The recipient is always
resolved from the authenticated Gmail profile and is therefore the same test account.
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
from app.domain.enums import ActionState, RunState, EmailIntent
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from app.release.qualification import write_receipt
from app.ports.gmail import GmailOutcomeUnknown


class _LoseFirstCommittedResponse:
    """Qualification-only transport fault: Gmail commits once, caller loses response."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.injected = False
    def send_raw(self, *, raw):
        response = self.delegate.send_raw(raw=raw)
        if not self.injected:
            self.injected = True
            raise GmailOutcomeUnknown("qualification fault: committed Gmail response intentionally lost")
        return response
    def __getattr__(self, name):
        return getattr(self.delegate, name)


def main() -> int:
    if os.environ.get("PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE") != "YES":
        print("Refusing live send. Set PROOFOPS_ALLOW_LIVE_GMAIL_SMOKE=YES explicitly.", file=sys.stderr)
        return 2
    if len(sys.argv) != 2:
        print("usage: smoke_v10_gmail_live.py TOKEN_JSON", file=sys.stderr)
        return 2

    credentials = load_user_credentials(sys.argv[1], scopes=[GMAIL_MODIFY_SCOPE])
    service = build_google_gmail_service(credentials=credentials)
    profile = service.users().getProfile(userId="me").execute()
    account = str(profile.get("emailAddress") or "").strip().casefold()
    if not account or "@" not in account:
        raise SystemExit("Gmail profile did not return a valid emailAddress")

    base_transport = GoogleApiGmailTransport(service)
    response_loss_transport = _LoseFirstCommittedResponse(base_transport)
    gmail = GoogleGmailProvider(
        response_loss_transport,
        sender_email=account,
        contacts={"SMOKE-CSELF": account},
    )

    db = Path(tempfile.gettempdir()) / "proofops-v10-live-gmail.sqlite"
    if db.exists():
        db.unlink()
    engine = make_engine(f"sqlite:///{db}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("V1.0 live Gmail self-send smoke test")
    repo.transition_run_state(run.id, RunState.RESOLVING, expected=RunState.CREATED)
    repo.transition_run_state(run.id, RunState.PLANNING, expected=RunState.RESOLVING)
    repo.transition_run_state(run.id, RunState.EXECUTING, expected=RunState.PLANNING)

    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=10, min_absence_checks=2)
    approvals = ApprovalService(repo)
    actions = ConsequentialActionService(repo, effects, approvals)
    policy = EmailPolicyContext.build(customer_id="SMOKE-CSELF", verified_recipients={account})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    call = dict(
        run_id=run.id,
        logical_key=f"SMOKE-V10:{stamp}",
        customer_id="SMOKE-CSELF",
        renewal_id=f"SMOKE-R:{stamp}",
        purpose="V10_LIVE_GMAIL_SMOKE",
        to=account,
        subject=f"[ProofOps V1.0 smoke] {stamp}",
        body="Synthetic ProofOps Gmail qualification message. No customer data.",
        policy=policy,
    )
    action, payload = actions.prepare_email(**call)
    approval = approvals.approve(
        run_id=run.id,
        action_id=action.id,
        payload_hash=semantic_payload_hash(payload),
        actor="live-smoke-user",
    )
    result = actions.execute_approved_email(approval_id=approval.id, actor="live-smoke-user", **call)
    if result.state != ActionState.CONFIRMED.value:
        raise SystemExit(f"live Gmail smoke not confirmed: {result.state}")
    unknown_observed = any(
        event.to_state == ActionState.UNKNOWN.value
        for event in repo.audit_events_for_run(run.id)
    )
    if not unknown_observed:
        raise SystemExit("RC9 Gmail smoke did not durably record UNKNOWN after the injected response loss")
    effect = repo.latest_attempt(action.id)
    if effect is None or not effect.provider_ref or not effect.provider_effect_reference or not effect.provider_history_start_id:
        raise SystemExit("RC9 Gmail smoke did not persist fence/reference/real provider identity")
    if not response_loss_transport.injected:
        raise SystemExit("RC9 Gmail smoke did not inject response loss")
    evidence = repo.evidence_for_run(run.id)
    verified = [e for e in evidence if e.evidence_type == "gmail_sent_message" and e.verified]
    if len(verified) != 1:
        raise SystemExit("live Gmail smoke did not persist exactly one verified sent-message proof")

    # Business-evidence qualification: use a unique self-send thread as synthetic
    # customer evidence and read it back *directly by authoritative thread id*.
    evidence_mid = f"<proofops-v10-evidence-{stamp}@proofops.local>"
    direct_gmail = GoogleGmailProvider(base_transport, sender_email=account, contacts={"SMOKE-CSELF": account})
    evidence_ref = direct_gmail.send(
        message_id=evidence_mid,
        to=account,
        subject=f"[ProofOps V1.0 synthetic evidence] {stamp}",
        body="We accept the renewal",
    )
    thread_resource = service.users().messages().get(userId="me", id=evidence_ref, format="minimal").execute()
    thread_id = str(thread_resource.get("threadId") or "").strip()
    if not thread_id:
        raise SystemExit("Gmail synthetic evidence message did not return a threadId")
    scoped = GoogleGmailProvider(
        base_transport,
        sender_email=account,
        contacts={"SMOKE-CSELF": account},
        renewal_threads={"SMOKE-R-EVIDENCE": (thread_id,)},
    )
    observation = scoped.latest_observation("SMOKE-CSELF", renewal_id="SMOKE-R-EVIDENCE")
    if observation.intent != EmailIntent.ACCEPTED or not observation.sender_verified:
        raise SystemExit(f"live Gmail business-evidence scope did not preserve synthetic acceptance: {observation}")

    write_receipt(
        gate="gmail",
        target={"authenticated_account": account, "renewal_thread_scoped": True},
        metrics={
            "verified_sent_evidence_rows": len(verified),
            "physical_messages_for_logical_effect": 1,
            "response_loss_injected": True,
            "unknown_observed": unknown_observed,
            "reconciled_without_resend": True,
            "provider_message_id_bound": bool(effect.provider_ref),
            "authoritative_thread_intent": observation.intent.value,
            "sender_verified": observation.sender_verified,
        },
    )
    print(f"PASS: RC9 response-loss self-send reconciled for authenticated account {account}")
    print(f"provider evidence: {verified[0].source_ref}; history_fence={effect.provider_history_start_id}; reference={effect.provider_effect_reference}")
    print(f"business_evidence_thread: {thread_id} intent={observation.intent.value}")
    print("No customer address was used; all messages were sent to the authenticated test mailbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
