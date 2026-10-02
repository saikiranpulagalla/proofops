from __future__ import annotations

import base64
import json
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from typing import Any

import pytest

from app.adapters.google_gmail import GoogleApiGmailTransport, GoogleGmailProvider, _no_replay_http
from app.domain.enums import ActionState, EmailIntent, RunState
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.persistence.db import create_schema, make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.ports.gmail import GmailOutcomeUnknown, GmailSearchUnavailable, GmailSendRejected
from app.ports.protocols import ProviderDataError, ProviderUnauthorized
from app.security.approval import ApprovalService
from app.security.canonicalization import semantic_payload_hash
from app.security.policy import EmailPolicyContext
from tests.helpers import advance_run_to_executing


def encode_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes(policy=policy.SMTP)).decode("ascii").rstrip("=")


def raw_message(*, provider_ref="m1", sender="alice@acme.com", to="demo@example.com", subject="Renewal", body="hello", message_id="<x@y>", internal_ms=1790841600000, labels=("INBOX",), auto_submitted=None, thread_id=None):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if auto_submitted is not None:
        msg["Auto-Submitted"] = auto_submitted
    msg.set_content(body)
    return {
        "id": provider_ref,
        "threadId": thread_id or f"t-{provider_ref}",
        "labelIds": list(labels),
        "internalDate": str(internal_ms),
        "raw": encode_raw(msg),
    }


@dataclass
class MemoryGmailTransport:
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    send_mode: str = "success"
    search_error: Exception | None = None
    get_error: Exception | None = None
    sent_calls: int = 0
    search_calls: list[tuple[str, tuple[str, ...], int]] = field(default_factory=list)
    _history_id: int = 100

    def send_raw(self, *, raw: str):
        self.sent_calls += 1
        # Decode enough to persist a realistic Gmail raw resource.
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        from email.parser import BytesParser
        mime = BytesParser(policy=policy.default).parsebytes(data)
        provider_ref = f"sent-{self.sent_calls}"
        resource = {
            "id": provider_ref,
            "threadId": f"thread-{self.sent_calls}",
            "labelIds": ["SENT"],
            "internalDate": "1790841600000",
            "raw": raw,
            "history_id": self._history_id + 1,
        }
        if self.send_mode == "reject":
            raise GmailSendRejected("400 invalid")
        if self.send_mode == "unknown_before":
            raise GmailOutcomeUnknown("connection lost")
        self.resources[provider_ref] = resource
        self._history_id += 1
        if self.send_mode == "unknown_after":
            raise GmailOutcomeUnknown("response lost after commit")
        if self.send_mode == "missing_id":
            return {}
        return {"id": provider_ref, "threadId": resource["threadId"], "labelIds": ["SENT"]}

    def search(self, *, query: str, label_ids: tuple[str, ...] = (), max_results: int = 10):
        self.search_calls.append((query, label_ids, max_results))
        if self.search_error:
            raise self.search_error
        out = []
        if query.startswith("rfc822msgid:"):
            wanted = query.split(":", 1)[1]
            from email.parser import BytesParser
            for resource in self.resources.values():
                data = base64.urlsafe_b64decode(resource["raw"] + "=" * (-len(resource["raw"]) % 4))
                mime = BytesParser(policy=policy.default).parsebytes(data)
                if str(mime.get("Message-ID") or "") != wanted:
                    continue
                if label_ids and not set(label_ids).issubset(set(resource.get("labelIds", []))):
                    continue
                out.append({"id": resource["id"], "threadId": resource.get("threadId")})
        elif query.startswith("from:"):
            target = query.split(":", 1)[1].casefold()
            from email.parser import BytesParser
            # Do not parse provider timestamps in the fake transport; adapter owns validation/sorting.
            resources = list(self.resources.values())
            for resource in resources:
                data = base64.urlsafe_b64decode(resource["raw"] + "=" * (-len(resource["raw"]) % 4))
                mime = BytesParser(policy=policy.default).parsebytes(data)
                from email.utils import parseaddr
                _, addr = parseaddr(str(mime.get("From") or ""))
                if addr.casefold() == target:
                    out.append({"id": resource["id"], "threadId": resource.get("threadId")})
        return out[:max_results]

    def get_raw(self, *, message_id: str):
        if self.get_error:
            raise self.get_error
        return dict(self.resources[message_id])

    def current_history_id(self) -> str:
        return str(self._history_id)

    def history_message_refs(self, *, start_history_id: str):
        try:
            start = int(start_history_id)
        except ValueError as exc:  # test double mirrors fail-closed provider behavior
            raise GmailSearchUnavailable("invalid history fence") from exc
        return [
            {"id": resource["id"], "threadId": resource.get("threadId")}
            for resource in self.resources.values()
            if int(resource.get("history_id", 0)) > start
        ]


def provider(transport=None, contacts=None):
    return GoogleGmailProvider(
        transport or MemoryGmailTransport(),
        sender_email="demo@example.com",
        contacts={"C001": "alice@acme.com"} if contacts is None else contacts,
    )


def test_send_roundtrip_preserves_exact_body_and_headers():
    t = MemoryGmailTransport()
    p = provider(t)
    body = "line1\nline2\n"
    ref = p.send(message_id="<proof@example>", to="Alice@ACME.com", subject="Renewal ✓", body=body)
    assert ref == "sent-1"
    found = p.find_by_message_id("<proof@example>")
    assert len(found) == 1
    msg = found[0]
    assert msg.to == ("alice@acme.com",)
    assert msg.subject == "Renewal ✓"
    assert msg.body == body
    assert "SENT" in msg.label_ids
    assert t.search_calls[-1] == ("rfc822msgid:<proof@example>", ("SENT",), 10)


def test_find_by_message_id_ignores_non_sent_match():
    t = MemoryGmailTransport()
    t.resources["x"] = raw_message(message_id="<x@y>", labels=("INBOX",))
    assert provider(t).find_by_message_id("<x@y>") == []


def test_find_by_message_id_preserves_ambiguity_when_two_sent_messages_share_id():
    t = MemoryGmailTransport()
    t.resources["a"] = raw_message(provider_ref="a", message_id="<dup@y>", labels=("SENT",))
    t.resources["b"] = raw_message(provider_ref="b", message_id="<dup@y>", labels=("SENT",))
    found = provider(t).find_by_message_id("<dup@y>")
    assert {m.provider_ref for m in found} == {"a", "b"}


def test_search_candidate_with_wrong_raw_message_id_is_not_accepted():
    t = MemoryGmailTransport(resources={"x": raw_message(provider_ref="x", message_id="<other@y>", labels=("SENT",))})
    t.search = lambda **kwargs: [{"id": "x"}]
    assert provider(t).find_by_message_id("<wanted@y>") == []


def test_send_response_without_provider_id_is_unknown_not_success():
    t = MemoryGmailTransport(send_mode="missing_id")
    with pytest.raises(GmailOutcomeUnknown):
        provider(t).send(message_id="<x@y>", to="alice@acme.com", body="hello")


def test_invalid_message_id_is_definitive_rejection_before_transport():
    t = MemoryGmailTransport()
    with pytest.raises(GmailSendRejected):
        provider(t).send(message_id="bad-id", to="alice@acme.com", body="hello")
    assert t.sent_calls == 0


def test_malformed_raw_provider_response_fails_closed():
    t = MemoryGmailTransport()
    t.resources["x"] = {"id": "x", "raw": "not base64***", "labelIds": ["SENT"]}
    # Search is scripted by a valid raw header, so use a direct malformed read through latest_observation.
    t.search = lambda **kwargs: [{"id": "x"}]
    with pytest.raises(ProviderDataError):
        provider(t).find_by_message_id("<x@y>")


def test_no_inbound_message_is_explicit_none():
    obs = provider(MemoryGmailTransport()).latest_observation("C001")
    assert obs.intent == EmailIntent.NONE
    assert obs.source_ref is None


@pytest.mark.parametrize(
    "body, expected",
    [
        ("We accept the renewal.", EmailIntent.ACCEPTED),
        ("We will not renew this year.", EmailIntent.DECLINED),
        ("Please wait and contact me later.", EmailIntent.DELAY),
        ("Please do not contact us again.", EmailIntent.DO_NOT_CONTACT),
        ("Can we discuss options?", EmailIntent.AMBIGUOUS),
        ("We accept the renewal, but we will not renew.", EmailIntent.AMBIGUOUS),
    ],
)
def test_inbound_intent_classifier_is_conservative(body, expected):
    t = MemoryGmailTransport(resources={"m1": raw_message(body=body)})
    obs = provider(t).latest_observation("C001")
    assert obs.intent == expected
    assert obs.sender_verified is True
    assert obs.source_ref == "m1"
    assert obs.observed_at is not None


def test_quoted_only_acceptance_is_marked_quoted_and_not_trusted_as_acceptance():
    t = MemoryGmailTransport(resources={"m1": raw_message(body="> We accept the renewal.\n> Thanks")})
    obs = provider(t).latest_observation("C001")
    assert obs.quoted_only is True
    assert obs.intent == EmailIntent.AMBIGUOUS


def test_auto_reply_is_flagged_even_if_text_looks_like_acceptance():
    t = MemoryGmailTransport(resources={
        "m1": raw_message(subject="Automatic Reply: Renewal", body="We accept the renewal.", auto_submitted="auto-replied")
    })
    obs = provider(t).latest_observation("C001")
    assert obs.auto_reply is True


def test_contact_mapping_is_required_and_wrong_sender_does_not_verify():
    p = provider(MemoryGmailTransport(), contacts={})
    with pytest.raises(ProviderDataError, match="no verified Gmail contact mapping"):
        p.latest_observation("C001")

    t = MemoryGmailTransport(resources={"m1": raw_message(sender="other@acme.com", body="We accept the renewal.")})
    # Script search result as if provider search returned a candidate; the parser still verifies From.
    t.search = lambda **kwargs: [{"id": "m1"}]
    obs = provider(t).latest_observation("C001")
    assert obs.sender_verified is False


def test_latest_observation_uses_newest_internal_date():
    old = raw_message(provider_ref="old", body="We accept the renewal.", internal_ms=1790000000000)
    new = raw_message(provider_ref="new", body="We will not renew this year.", internal_ms=1791000000000)
    t = MemoryGmailTransport(resources={"old": old, "new": new})
    assert provider(t).latest_observation("C001").intent == EmailIntent.DECLINED


def test_malformed_internal_date_fails_closed():
    bad = raw_message(body="We accept the renewal.")
    bad["internalDate"] = "not-a-number"
    t = MemoryGmailTransport(resources={"m1": bad})
    with pytest.raises(ProviderDataError, match="invalid internalDate"):
        provider(t).latest_observation("C001")


def test_search_failure_is_not_absence():
    t = MemoryGmailTransport(search_error=GmailSearchUnavailable("down"))
    with pytest.raises(GmailSearchUnavailable):
        provider(t).latest_observation("C001")


def _effect_setup(tmp_path, gmail):
    engine = make_engine(f"sqlite:///{tmp_path / 'gmail.sqlite'}")
    create_schema(engine)
    repo = LedgerRepository(make_session_factory(engine))
    run = repo.create_run("Handle ACME renewal")
    advance_run_to_executing(repo, run.id)
    effects = EffectExecutor(repo, gmail, reconciliation_grace_seconds=0, min_absence_checks=2, execution_lease_seconds=1)
    approvals = ApprovalService(repo)
    service = ConsequentialActionService(repo, effects, approvals)
    policy_ctx = EmailPolicyContext.build(customer_id="C001", verified_recipients={"alice@acme.com"})
    return repo, run, effects, approvals, service, policy_ctx


def _authorized_call(repo, run, approvals, service, policy_ctx):
    call = dict(
        run_id=run.id,
        logical_key="R1:initial",
        customer_id="C001",
        renewal_id="R1",
        purpose="INITIAL",
        to="alice@acme.com",
        subject="Renewal",
        body="hello",
        policy=policy_ctx,
    )
    action, payload = service.prepare_email(**call)
    approval = approvals.approve(run_id=run.id, action_id=action.id, payload_hash=semantic_payload_hash(payload), actor="demo-user")
    return call, action, approval


def test_real_adapter_contract_integrates_with_effect_ledger(tmp_path):
    t = MemoryGmailTransport()
    gmail = provider(t)
    repo, run, effects, approvals, service, policy_ctx = _effect_setup(tmp_path, gmail)
    call, action, approval = _authorized_call(repo, run, approvals, service, policy_ctx)
    result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
    assert result.state == ActionState.CONFIRMED.value
    assert t.sent_calls == 1
    evidence = repo.evidence_for_run(run.id)
    assert len(evidence) == 1 and evidence[0].verified


def test_real_adapter_unknown_after_commit_reconciles_without_duplicate(tmp_path):
    t = MemoryGmailTransport(send_mode="unknown_after")
    gmail = provider(t)
    repo, run, effects, approvals, service, policy_ctx = _effect_setup(tmp_path, gmail)
    call, action, approval = _authorized_call(repo, run, approvals, service, policy_ctx)
    result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
    assert result.state == ActionState.CONFIRMED.value
    assert t.sent_calls == 1
    assert len(t.resources) == 1


def test_definitive_send_rejection_fails_action_and_run_without_retry(tmp_path):
    t = MemoryGmailTransport(send_mode="reject")
    gmail = provider(t)
    repo, run, effects, approvals, service, policy_ctx = _effect_setup(tmp_path, gmail)
    call, action, approval = _authorized_call(repo, run, approvals, service, policy_ctx)
    result = service.execute_approved_email(approval_id=approval.id, actor="demo-user", **call)
    assert result.state == ActionState.FAILED.value
    assert repo.get_run(run.id).state == RunState.FAILED.value
    assert t.sent_calls == 1


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


class ExecuteNode:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
    def execute(self):
        if self.error:
            raise self.error
        return self.value


class FakeMessagesApi:
    def __init__(self, *, send_error=None, list_error=None, get_error=None):
        self.send_error, self.list_error, self.get_error = send_error, list_error, get_error
    def send(self, **kwargs): return ExecuteNode({"id": "x"}, self.send_error)
    def list(self, **kwargs): return ExecuteNode({"messages": []}, self.list_error)
    def get(self, **kwargs): return ExecuteNode({"id": "x", "raw": "x"}, self.get_error)


class FakeUsersApi:
    def __init__(self, messages): self._messages = messages
    def messages(self): return self._messages


class FakeService:
    def __init__(self, messages): self._users = FakeUsersApi(messages)
    def users(self): return self._users


@pytest.mark.parametrize("status, exc_type", [(401, ProviderUnauthorized), (403, ProviderUnauthorized), (429, GmailOutcomeUnknown), (500, GmailOutcomeUnknown), (503, GmailOutcomeUnknown), (400, GmailSendRejected)])
def test_google_transport_maps_send_statuses(status, exc_type):
    t = GoogleApiGmailTransport(FakeService(FakeMessagesApi(send_error=FakeHttpError(status))))
    with pytest.raises(exc_type):
        t.send_raw(raw="x")


def test_rc11_transport_does_not_replay_a_post_after_connection_drop():
    """A03: exercise the installed httplib2/google client path, not a mock retry flag."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from googleapiclient.http import HttpRequest

    commits: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            commits.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    http = _no_replay_http()
    try:
        request = HttpRequest(
            http,
            lambda response, content: json.loads(content),
            uri=f"http://127.0.0.1:{server.server_port}/consequential-send",
            method="POST",
            body="synthetic-consequential-payload",
            headers={"content-type": "application/json"},
        )
        with pytest.raises(Exception):
            request.execute(num_retries=0)
        assert commits == [b"synthetic-consequential-payload"]
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class _PagedMessages:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str | None] = []

    def list(self, **kwargs):
        self.calls.append(kwargs.get("pageToken"))
        return ExecuteNode(self.pages[kwargs.get("pageToken")])


class _PagedUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _PagedService:
    def __init__(self, pages):
        self._users = _PagedUsers(_PagedMessages(pages))

    def users(self):
        return self._users


def test_rc11_search_exhausts_pages_before_claiming_cardinality():
    service = _PagedService({
        None: {"messages": [{"id": "first"}], "nextPageToken": "second"},
        "second": {"messages": [{"id": "second"}]},
    })
    assert GoogleApiGmailTransport(service).search(query="in:sent", max_results=10) == [{"id": "first"}, {"id": "second"}]
    assert service._users._messages.calls == [None, "second"]


def test_rc11_search_repeated_page_token_fails_closed():
    service = _PagedService({
        None: {"messages": [{"id": "first"}], "nextPageToken": "again"},
        "again": {"messages": [{"id": "second"}], "nextPageToken": "again"},
    })
    with pytest.raises(GmailSearchUnavailable, match="token repeated"):
        GoogleApiGmailTransport(service).search(query="in:sent", max_results=10)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_google_transport_search_errors_are_unavailable_not_absence(status):
    t = GoogleApiGmailTransport(FakeService(FakeMessagesApi(list_error=FakeHttpError(status))))
    with pytest.raises(GmailSearchUnavailable):
        t.search(query="x")
