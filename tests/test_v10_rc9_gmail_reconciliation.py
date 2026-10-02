"""RC9 regression: Gmail may rewrite caller RFC Message-ID after committing a send."""
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import base64

from app.adapters.google_gmail import GoogleGmailProvider
from app.adapters.identity_bound_gmail import CRMIdentityBoundGmailProvider
from app.domain.enums import ActionState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from tests.helpers import advance_run_to_executing


class RewriteAfterCommitTransport:
    def __init__(self): self.resources={}; self.sent=0
    def current_history_id(self): return "100"
    def history_message_refs(self, *, start_history_id): return [{"id": x, "threadId": r["threadId"]} for x,r in self.resources.items()]
    def send_raw(self, *, raw):
        self.sent += 1; mime=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)))
        del mime["Message-ID"]; mime["Message-ID"]="<gmail-rewritten-%s@mail.gmail.com>" % self.sent
        rid="gmail-%s" % self.sent
        self.resources[rid]={"id":rid,"threadId":"thread-1","labelIds":["SENT"],"raw":base64.urlsafe_b64encode(mime.as_bytes(policy=policy.SMTP)).decode().rstrip("=")}
        from app.ports.gmail import GmailOutcomeUnknown
        raise GmailOutcomeUnknown("response lost after provider commit")
    def get_raw(self, *, message_id): return dict(self.resources[message_id])
    def search(self, **kwargs): return []


class ReconciliationTransport(RewriteAfterCommitTransport):
    """Configurable post-fence Gmail fake; all candidates are real MIME messages."""
    def __init__(self, *, commit=True, response_lost=True, history_expired=False):
        super().__init__(); self.commit=commit; self.response_lost=response_lost; self.history_expired=history_expired
    def send_raw(self, *, raw):
        self.sent += 1
        if self.commit:
            self._store(raw, "gmail-1")
        if self.response_lost:
            from app.ports.gmail import GmailOutcomeUnknown
            raise GmailOutcomeUnknown("response lost")
        return {"id":"gmail-1"}
    def _store(self, raw, rid, *, mutate_body=None, mutate_reference=None):
        mime=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)))
        if mutate_body is not None:
            mime.set_payload(base64.b64encode(mutate_body.encode()).decode())
        if mutate_reference is not None:
            text=base64.b64decode(str(mime.get_payload())).decode().replace("Reference: "+mime["X-ProofOps-Effect-Reference"], "Reference: "+mutate_reference)
            mime.replace_header("X-ProofOps-Effect-Reference", mutate_reference)
            mime.set_payload(base64.b64encode(text.encode()).decode())
        del mime["Message-ID"]; mime["Message-ID"]="<provider-%s@mail.gmail.com>" % rid
        self.resources[rid]={"id":rid,"threadId":"thread-"+rid,"labelIds":["SENT"],"raw":base64.urlsafe_b64encode(mime.as_bytes(policy=policy.SMTP)).decode().rstrip("=")}
    def history_message_refs(self, *, start_history_id):
        if self.history_expired:
            from app.ports.gmail import GmailSearchUnavailable
            raise GmailSearchUnavailable("Gmail history fence expired")
        return super().history_message_refs(start_history_id=start_history_id)
    def search(self, **kwargs): return [{"id": k, "threadId": v["threadId"]} for k,v in self.resources.items()]


def _execute(tmp_path, transport):
    engine=make_engine(f"sqlite:///{tmp_path/'ledger.sqlite'}"); create_schema(engine); repo=LedgerRepository(make_session_factory(engine))
    run=repo.create_run("Handle ACME renewal"); advance_run_to_executing(repo,run.id)
    gmail=GoogleGmailProvider(transport,sender_email="self@example.com",contacts={"C":"self@example.com"})
    effects=EffectExecutor(repo,gmail,reconciliation_grace_seconds=30,min_absence_checks=2); approvals=ApprovalService(repo); service=ConsequentialActionService(repo,effects,approvals)
    call=dict(run_id=run.id,logical_key="R:initial",customer_id="C",renewal_id="R",purpose="INITIAL",to="self@example.com",subject="Renewal",body="hello",policy=EmailPolicyContext.build(customer_id="C",verified_recipients={"self@example.com"}))
    action,payload=service.prepare_email(**call); approval=approvals.approve(run_id=run.id,action_id=action.id,payload_hash=semantic_payload_hash(payload),actor="rc9")
    result=service.execute_approved_email(approval_id=approval.id,actor="rc9",**call)
    return repo, action, result


def test_rewritten_rfc_id_response_loss_reconciles_one_physical_send(tmp_path):
    engine=make_engine(f"sqlite:///{tmp_path/'ledger.sqlite'}"); create_schema(engine); repo=LedgerRepository(make_session_factory(engine))
    run=repo.create_run("Handle ACME renewal"); advance_run_to_executing(repo,run.id)
    transport=RewriteAfterCommitTransport(); gmail=GoogleGmailProvider(transport,sender_email="self@example.com",contacts={"C":"self@example.com"})
    effects=EffectExecutor(repo,gmail,reconciliation_grace_seconds=30,min_absence_checks=2); approvals=ApprovalService(repo); service=ConsequentialActionService(repo,effects,approvals)
    call=dict(run_id=run.id,logical_key="R:initial",customer_id="C",renewal_id="R",purpose="INITIAL",to="self@example.com",subject="Renewal",body="hello",policy=EmailPolicyContext.build(customer_id="C",verified_recipients={"self@example.com"}))
    action,payload=service.prepare_email(**call); approval=approvals.approve(run_id=run.id,action_id=action.id,payload_hash=semantic_payload_hash(payload),actor="rc9")
    result=service.execute_approved_email(approval_id=approval.id,actor="rc9",**call)
    effect=repo.latest_attempt(action.id)
    assert result.state == ActionState.CONFIRMED.value and transport.sent == 1
    assert effect.provider_ref == "gmail-1" and effect.provider_rfc_message_id != effect.external_key
    assert effect.provider_history_start_id == "100" and effect.provider_effect_reference.startswith("PO-")


def test_zero_match_stays_unknown_without_blind_resend(tmp_path):
    transport=ReconciliationTransport(commit=False)
    repo, action, result=_execute(tmp_path, transport)
    assert result.state == ActionState.UNKNOWN.value and transport.sent == 1
    assert repo.latest_attempt(action.id).provider_ref is None


def test_ambiguous_matching_candidates_fail_closed(tmp_path):
    transport=ReconciliationTransport()
    original=transport.send_raw
    def duplicate(*, raw):
        try: original(raw=raw)
        except Exception:
            transport._store(raw, "gmail-2")
            raise
    transport.send_raw=duplicate
    repo, action, result=_execute(tmp_path, transport)
    assert result.state == ActionState.UNKNOWN.value and transport.sent == 1
    assert repo.latest_attempt(action.id).provider_ref is None


def test_wrong_reference_same_subject_recipient_is_not_reconciled(tmp_path):
    transport=ReconciliationTransport()
    original=transport.send_raw
    def tampered(*, raw):
        transport.sent += 1; transport._store(raw, "gmail-1", mutate_reference="PO-wrong")
        from app.ports.gmail import GmailOutcomeUnknown
        raise GmailOutcomeUnknown("response lost")
    transport.send_raw=tampered
    repo, action, result=_execute(tmp_path, transport)
    assert result.state == ActionState.UNKNOWN.value and repo.latest_attempt(action.id).provider_ref is None


def test_correct_reference_wrong_payload_never_confirms(tmp_path):
    transport=ReconciliationTransport()
    def tampered(*, raw):
        transport.sent += 1; transport._store(raw, "gmail-1", mutate_body="tampered\n\nReference: PO-placeholder")
        # restore the real marker while retaining a different body
        rawmsg=transport.resources["gmail-1"]["raw"]
        parsed=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(rawmsg+"="*(-len(rawmsg)%4)))
        ref=parsed["X-ProofOps-Effect-Reference"]; parsed.set_payload(base64.b64encode(("tampered\n\nReference: "+ref).encode()).decode())
        transport.resources["gmail-1"]["raw"]=base64.urlsafe_b64encode(parsed.as_bytes(policy=policy.SMTP)).decode().rstrip("=")
        from app.ports.gmail import GmailOutcomeUnknown
        raise GmailOutcomeUnknown("response lost")
    transport.send_raw=tampered
    repo, action, result=_execute(tmp_path, transport)
    assert result.state != ActionState.CONFIRMED.value and repo.latest_attempt(action.id).provider_ref is None


def test_expired_history_fence_uses_exact_reference_fallback(tmp_path):
    transport=ReconciliationTransport(history_expired=True)
    repo, action, result=_execute(tmp_path, transport)
    assert result.state == ActionState.CONFIRMED.value
    assert repo.latest_attempt(action.id).provider_ref == "gmail-1"


def test_normal_response_binds_real_provider_message_id(tmp_path):
    transport=ReconciliationTransport(response_lost=False)
    repo, action, result=_execute(tmp_path, transport)
    effect=repo.latest_attempt(action.id)
    assert result.state == ActionState.CONFIRMED.value and effect.provider_ref == "gmail-1" and transport.sent == 1


def test_rc11_conflicting_html_alternative_never_confirms_plaintext_payload(tmp_path):
    """A07: every recipient-visible alternative must agree with the approved body."""
    transport = ReconciliationTransport()

    def conflicting_alternative(*, raw):
        transport.sent += 1
        original = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        )
        message = EmailMessage(policy=policy.SMTP)
        for name in ("From", "To", "Subject", "Message-ID", "X-ProofOps-Effect-Reference"):
            if original.get(name):
                message[name] = original[name]
        message.set_content(original.get_content())
        message.add_alternative("<p>Send a different commercial offer now.</p>", subtype="html")
        message.replace_header("Message-ID", "<provider-conflicting@mail.gmail.com>")
        transport.resources["gmail-1"] = {
            "id": "gmail-1", "threadId": "thread-gmail-1", "labelIds": ["SENT"],
            "raw": base64.urlsafe_b64encode(message.as_bytes(policy=policy.SMTP)).decode().rstrip("="),
        }
        from app.ports.gmail import GmailOutcomeUnknown
        raise GmailOutcomeUnknown("response lost")

    transport.send_raw = conflicting_alternative
    repo, action, result = _execute(tmp_path, transport)
    assert result.state != ActionState.CONFIRMED.value
    assert repo.latest_attempt(action.id).provider_ref is None


def test_rc11_production_identity_wrapper_preserves_required_reconciliation_capabilities(tmp_path):
    """A02: the live composition cannot silently downgrade to RFC-ID recovery."""
    transport = ReconciliationTransport()
    bare = GoogleGmailProvider(transport, sender_email="self@example.com", contacts={"C": "self@example.com"})
    wrapped = CRMIdentityBoundGmailProvider(
        bare, None, configured_contacts={"C": "self@example.com"}, allowed_recipients={"self@example.com"},
    )
    assert wrapped.history_fence() == "100"
    repo, action, result = _execute_wrapped(tmp_path, wrapped)
    effect = repo.latest_attempt(action.id)
    assert result.state == ActionState.CONFIRMED.value
    assert transport.sent == 1
    assert effect.provider_history_start_id == "100"
    assert effect.provider_effect_reference.startswith("PO-")
    assert effect.provider_rfc_message_id != effect.external_key


def _execute_wrapped(tmp_path, gmail):
    engine = make_engine(f"sqlite:///{tmp_path/'wrapped.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME renewal")
    advance_run_to_executing(repo, run.id)
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=30, min_absence_checks=2)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)
    call = dict(run_id=run.id, logical_key="R:initial", customer_id="C", renewal_id="R", purpose="INITIAL", to="self@example.com", subject="Renewal", body="hello", policy=EmailPolicyContext.build(customer_id="C", verified_recipients={"self@example.com"}))
    action, payload = service.prepare_email(**call)
    approval = approvals.approve(run_id=run.id, action_id=action.id, payload_hash=semantic_payload_hash(payload), actor="rc11")
    return repo, action, service.execute_approved_email(approval_id=approval.id, actor="rc11", **call)
