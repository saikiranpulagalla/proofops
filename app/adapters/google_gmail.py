from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any, Protocol

from app.domain.enums import EmailIntent
from app.domain.models import EmailObservation
from app.ports.gmail import GmailMessage, GmailOutcomeUnknown, GmailSearchUnavailable, GmailSendRejected
from app.ports.protocols import ProviderDataError, ProviderUnauthorized
from app.security.canonicalization import normalize_email, normalize_recipients


class GmailTransport(Protocol):
    def send_raw(self, *, raw: str) -> dict[str, Any]: ...
    def search(self, *, query: str, label_ids: tuple[str, ...] = (), max_results: int = 10) -> list[dict[str, Any]]: ...
    def get_raw(self, *, message_id: str) -> dict[str, Any]: ...
    def thread_message_refs(self, *, thread_id: str) -> list[dict[str, Any]]: ...
    def current_history_id(self) -> str: ...
    def history_message_refs(self, *, start_history_id: str) -> list[dict[str, Any]]: ...


def _no_replay_http():
    """Build an httplib2 client that dispatches an unsafe request at most once.

    httplib2 retries a first ``BadStatusLine`` by resetting its loop counter even
    when Google ``HttpRequest.execute(num_retries=0)`` was requested.  That is
    acceptable for idempotent reads, but not for Gmail's consequential POST.  This
    narrow transport keeps httplib2's connection/TLS setup while replacing the
    connection dispatch primitive with one attempt.  Any response ambiguity reaches
    ``send_raw`` and becomes ``GmailOutcomeUnknown`` for durable reconciliation.
    """
    try:
        import httplib2
    except ImportError as exc:  # pragma: no cover - optional Google runtime package
        raise RuntimeError("httplib2 is required for the real Gmail transport") from exc

    class NoReplayHttp(httplib2.Http):
        def request(self, uri, method="GET", body=None, headers=None, redirections=httplib2.DEFAULT_MAX_REDIRECTS, connection_type=None):
            # A redirect is another dispatch.  Gmail API POSTs must surface it to the
            # application rather than silently follow it.
            if method.upper() not in self.safe_methods:
                redirections = 0
            return super().request(
                uri, method=method, body=body, headers=headers,
                redirections=redirections, connection_type=connection_type,
            )

        def _conn_request(self, conn, request_uri, method, body, headers):
            try:
                if conn.sock is None:
                    conn.connect()
                conn.request(method, request_uri, body, headers)
                response = conn.getresponse()
                content = b"" if method == "HEAD" else response.read()
                if method == "HEAD":
                    conn.close()
                response = httplib2.Response(response)
                if method != "HEAD":
                    content = httplib2._decompressContent(response, content, self.limit_kwargs)
                return response, content
            except Exception:
                conn.close()
                raise

    return NoReplayHttp()


class GoogleApiGmailTransport:
    """Thin Google client wrapper with explicit uncertainty semantics.

    A timeout/network failure or 429/5xx during send is conservatively treated as
    outcome-unknown because ProofOps cannot prove whether Gmail committed the effect.
    Search/read failures never become message absence.
    """

    def __init__(self, service: Any):
        self.service = service

    @staticmethod
    def _status(exc: Exception) -> int | None:
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status or getattr(exc, "status_code", None)

    def send_raw(self, *, raw: str) -> dict[str, Any]:
        try:
            return (
                self.service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
        except Exception as exc:
            status = self._status(exc)
            if status in {401, 403}:
                raise ProviderUnauthorized(f"Gmail authorization failed ({status})") from exc
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise GmailOutcomeUnknown(f"Gmail send outcome unknown ({status})") from exc
            if status is not None:
                raise GmailSendRejected(f"Gmail rejected send ({status}): {exc}") from exc
            # Connection reset, timeout, DNS/socket failure, etc. may happen after dispatch.
            raise GmailOutcomeUnknown(f"Gmail send transport outcome unknown: {exc}") from exc

    def search(self, *, query: str, label_ids: tuple[str, ...] = (), max_results: int = 10) -> list[dict[str, Any]]:
        try:
            out: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_tokens: set[str] = set()
            token: str | None = None
            while True:
                if token is not None:
                    if token in seen_tokens:
                        raise GmailSearchUnavailable("Gmail search pagination token repeated")
                    seen_tokens.add(token)
                response = (
                    self.service.users()
                    .messages()
                    .list(
                        userId="me",
                        q=query,
                        labelIds=list(label_ids) or None,
                        maxResults=max_results,
                        includeSpamTrash=False,
                        pageToken=token,
                    )
                    .execute()
                )
                messages = response.get("messages", [])
                if not isinstance(messages, list):
                    raise GmailSearchUnavailable("Gmail search response has invalid messages field")
                for item in messages:
                    if not isinstance(item, dict):
                        raise GmailSearchUnavailable("Gmail search returned an invalid message reference")
                    message_id = str(item.get("id") or "").strip()
                    # Leave missing ids to the provider parser, but avoid counting a
                    # duplicated valid object from two pages as ambiguous evidence.
                    if message_id and message_id in seen_ids:
                        continue
                    if message_id:
                        seen_ids.add(message_id)
                    out.append(dict(item))
                next_token = response.get("nextPageToken")
                if not next_token:
                    return out
                if not isinstance(next_token, str):
                    raise GmailSearchUnavailable("Gmail search returned an invalid continuation token")
                token = next_token
        except Exception as exc:
            if isinstance(exc, GmailSearchUnavailable):
                raise
            status = self._status(exc)
            if status in {401, 403}:
                raise GmailSearchUnavailable(f"Gmail search authorization failed ({status})") from exc
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise GmailSearchUnavailable(f"Gmail search unavailable ({status})") from exc
            raise GmailSearchUnavailable(f"Gmail search failed: {exc}") from exc

    def get_raw(self, *, message_id: str) -> dict[str, Any]:
        try:
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
        except Exception as exc:
            status = self._status(exc)
            if status in {401, 403}:
                raise GmailSearchUnavailable(f"Gmail read authorization failed ({status})") from exc
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise GmailSearchUnavailable(f"Gmail read unavailable ({status})") from exc
            raise GmailSearchUnavailable(f"Gmail read failed: {exc}") from exc

    def thread_message_refs(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Read the authoritative Gmail thread directly, avoiding sender-search truncation."""
        try:
            response = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="minimal")
                .execute()
            )
            messages = response.get("messages", [])
            if not isinstance(messages, list):
                raise GmailSearchUnavailable("Gmail thread response has invalid messages field")
            return [m for m in messages if isinstance(m, dict)]
        except GmailSearchUnavailable:
            raise
        except Exception as exc:
            status = self._status(exc)
            if status in {401, 403, 404}:
                raise GmailSearchUnavailable(f"Gmail thread unavailable ({status})") from exc
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise GmailSearchUnavailable(f"Gmail thread unavailable ({status})") from exc
            raise GmailSearchUnavailable(f"Gmail thread read failed: {exc}") from exc

    def current_history_id(self) -> str:
        try:
            value = self.service.users().getProfile(userId="me").execute().get("historyId")
            if not value: raise ProviderDataError("Gmail profile omitted historyId")
            return str(value)
        except (ProviderDataError,): raise
        except Exception as exc: raise GmailSearchUnavailable(f"Gmail history fence failed: {exc}") from exc

    def history_message_refs(self, *, start_history_id: str) -> list[dict[str, Any]]:
        """Exhaust all post-fence history pages; history results alone are never proof."""
        try:
            out: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_tokens: set[str] = set()
            token: str | None = None
            while True:
                if token is not None:
                    if token in seen_tokens:
                        raise GmailSearchUnavailable("Gmail history pagination token repeated")
                    seen_tokens.add(token)
                response = (
                    self.service.users().history().list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        pageToken=token,
                    ).execute()
                )
                history = response.get("history", [])
                if not isinstance(history, list):
                    raise GmailSearchUnavailable("Gmail history response has invalid history field")
                for item in history:
                    if not isinstance(item, dict):
                        raise GmailSearchUnavailable("Gmail history returned an invalid entry")
                    additions = item.get("messagesAdded", [])
                    if not isinstance(additions, list):
                        raise GmailSearchUnavailable("Gmail history returned invalid message additions")
                    for added in additions:
                        if not isinstance(added, dict):
                            raise GmailSearchUnavailable("Gmail history returned an invalid message addition")
                        message = added.get("message") or {}
                        if not isinstance(message, dict):
                            raise GmailSearchUnavailable("Gmail history returned an invalid message reference")
                        message_id = str(message.get("id") or "").strip()
                        if message_id and message_id in seen_ids:
                            continue
                        if message_id:
                            seen_ids.add(message_id)
                        if message:
                            out.append(dict(message))
                next_token = response.get("nextPageToken")
                if not next_token:
                    return out
                if not isinstance(next_token, str):
                    raise GmailSearchUnavailable("Gmail history returned an invalid continuation token")
                token = next_token
        except Exception as exc:
            status=self._status(exc)
            if status == 404: raise GmailSearchUnavailable("Gmail history fence expired") from exc
            raise GmailSearchUnavailable(f"Gmail history query failed: {exc}") from exc


def build_google_gmail_service(*, credentials: Any):
    try:
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
    except ImportError as exc:  # pragma: no cover - optional runtime package
        raise RuntimeError("Install ProofOps with the 'google' extra to use the real Gmail adapter") from exc
    # Credentials are refreshed before request dispatch when needed.  Disable the
    # post-401 replay layer for this service; a consequential POST with an ambiguous
    # response must be reconciled by ProofOps, not resent below its durable ledger.
    http = AuthorizedHttp(credentials, http=_no_replay_http(), max_refresh_attempts=0)
    return build("gmail", "v1", http=http, cache_discovery=False)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._quote_depth = 0
        self.quoted_seen = False

    @staticmethod
    def _is_quote_container(tag: str, attrs) -> bool:
        attrs_map = {str(k).casefold(): str(v or "").casefold() for k, v in attrs}
        classes = set(attrs_map.get("class", "").split())
        ident = attrs_map.get("id", "")
        if tag.casefold() == "blockquote":
            return True
        if classes & {"gmail_quote", "gmail_attr", "yahoo_quoted", "moz-cite-prefix"}:
            return True
        if ident in {"divrplyfwdmsg", "appendonsend"}:
            return True
        return False

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._quote_depth:
            self._quote_depth += 1
            return
        if self._is_quote_container(tag, attrs):
            self._quote_depth = 1
            self.quoted_seen = True
            return
        if tag.casefold() in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._quote_depth:
            self._quote_depth -= 1
            return
        if tag.casefold() in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._quote_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts).strip()


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ProviderDataError("Gmail returned invalid base64url raw message") from exc


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _addresses(header_value: str | None) -> tuple[str, ...]:
    if not header_value:
        return ()
    return normalize_recipients(addr for _, addr in getaddresses([header_value]) if addr)


def _extract_body_variants(msg) -> tuple[str, ...]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = (msg,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                plain_parts.append(part.get_content())
            except Exception:
                continue
        elif ctype == "text/html":
            try:
                parser = _HTMLText()
                parser.feed(part.get_content())
                html_parts.append(parser.text())
            except Exception:
                continue
    variants: list[str] = []
    if plain_parts:
        variants.append("\n".join(plain_parts))
    if html_parts:
        variants.append("\n".join(html_parts))
    return tuple(variants)


def _extract_body(msg) -> str:
    variants = _extract_body_variants(msg)
    return variants[0] if variants else ""


def _extract_inbound_text(msg) -> tuple[str, bool]:
    """Return current-author text and whether the message was quote-only.

    Plain text uses conventional quote markers. HTML removes common Gmail/Outlook
    quoted-history containers before intent classification, preventing old quoted text
    from being treated as fresh customer intent.
    """
    plain_parts: list[str] = []
    html_parts: list[tuple[str, bool]] = []
    parts = msg.walk() if msg.is_multipart() else (msg,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                plain_parts.append(part.get_content())
            except Exception:
                continue
        elif ctype == "text/html":
            try:
                parser = _HTMLText()
                parser.feed(part.get_content())
                html_parts.append((parser.text(), parser.quoted_seen))
            except Exception:
                continue
    if plain_parts:
        return _nonquoted_text("\n".join(plain_parts))
    if html_parts:
        visible = "\n".join(text for text, _ in html_parts if text).strip()
        quoted_seen = any(seen for _, seen in html_parts)
        return visible, bool(quoted_seen and not visible)
    return "", False


def _nonquoted_text(body: str) -> tuple[str, bool]:
    lines: list[str] = []
    quoted_seen = False
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            quoted_seen = True
            continue
        if re.match(r"^On .+ wrote:$", stripped, flags=re.IGNORECASE) or stripped.casefold() == "-----original message-----":
            quoted_seen = True
            break
        lines.append(line)
    visible = "\n".join(lines).strip()
    return visible, bool(quoted_seen and not visible)


def _classify_intent(text: str) -> EmailIntent:
    lowered = " ".join(text.casefold().split())
    if not lowered:
        return EmailIntent.AMBIGUOUS

    groups: dict[EmailIntent, tuple[str, ...]] = {
        EmailIntent.DO_NOT_CONTACT: (
            "do not contact", "don't contact", "stop emailing", "do not email", "unsubscribe me",
        ),
        EmailIntent.DECLINED: (
            "will not renew", "won't renew", "not renewing", "decline the renewal", "declining the renewal",
        ),
        EmailIntent.DELAY: (
            "contact me later", "reach out later", "contact us later", "reach out next month", "please wait",
            "not ready yet",
        ),
        EmailIntent.ACCEPTED: (
            "we accept the renewal", "renewal accepted", "we have renewed", "we've renewed", "we will renew",
        ),
    }
    hits = [intent for intent, phrases in groups.items() if any(p in lowered for p in phrases)]
    if len(hits) == 1:
        return hits[0]
    return EmailIntent.AMBIGUOUS


@dataclass(frozen=True)
class GmailContact:
    customer_id: str
    email: str


class GoogleGmailProvider:
    """Real Gmail read/send adapter with deterministic reconciliation identity."""

    def __init__(
        self,
        transport: GmailTransport,
        *,
        sender_email: str,
        contacts: dict[str, str] | None = None,
        renewal_threads: dict[str, tuple[str, ...]] | None = None,
        read_search_limit: int = 20,
    ):
        sender = normalize_email(sender_email)
        if not sender:
            raise ValueError("sender_email is required")
        self.transport = transport
        self.sender_email = sender
        self.contacts = {k: normalize_email(v) for k, v in (contacts or {}).items() if v and v.strip()}
        self.renewal_threads = {
            str(k): tuple(str(t) for t in v if str(t).strip())
            for k, v in (renewal_threads or {}).items()
        }
        self.read_search_limit = max(1, min(int(read_search_limit), 100))

    @staticmethod
    def _build_raw(*, sender: str, message_id: str, to: str, subject: str, body: str, effect_reference: str | None = None) -> str:
        if not message_id.startswith("<") or not message_id.endswith(">"):
            raise GmailSendRejected("deterministic RFC822 Message-ID must be enclosed in angle brackets")
        target = normalize_email(to)
        if not target:
            raise GmailSendRejected("recipient is required")
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = sender
        msg["To"] = target
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        # Encode the body explicitly so round-tripping preserves exact text including
        # terminal newlines instead of EmailMessage.set_content() adding one.
        if effect_reference:
            body = body + "\n\nReference: " + effect_reference
            msg["X-ProofOps-Effect-Reference"] = effect_reference
        msg["Content-Type"] = 'text/plain; charset="utf-8"'
        msg["Content-Transfer-Encoding"] = "base64"
        msg.set_payload(base64.b64encode(body.encode("utf-8")).decode("ascii"))
        return _b64url_encode(msg.as_bytes(policy=policy.SMTP))

    @staticmethod
    def _parse_raw(resource: dict[str, Any]) -> GmailMessage:
        provider_ref = str(resource.get("id") or "").strip()
        raw = resource.get("raw")
        if not provider_ref or not isinstance(raw, str) or not raw:
            raise ProviderDataError("Gmail raw message response is missing id/raw")
        msg = BytesParser(policy=policy.default).parsebytes(_b64url_decode(raw))
        message_id = str(msg.get("Message-ID") or "").strip()
        if not message_id:
            raise ProviderDataError("Gmail message has no RFC822 Message-ID")
        variants = _extract_body_variants(msg)
        return GmailMessage(
            provider_ref=provider_ref,
            message_id=message_id,
            from_email=normalize_email(parseaddr(str(msg.get("From") or ""))[1]),
            to=_addresses(msg.get("To")),
            cc=_addresses(msg.get("Cc")),
            bcc=_addresses(msg.get("Bcc")),
            subject=str(msg.get("Subject") or ""),
            body=variants[0] if variants else "",
            body_variants=variants,
            thread_id=str(resource.get("threadId") or "").strip() or None,
            label_ids=tuple(str(x) for x in resource.get("labelIds", []) if x),
        )

    def send(self, *, message_id: str, to: str, subject: str = "", body: str, effect_reference: str | None = None) -> str:
        raw = self._build_raw(
            sender=self.sender_email,
            message_id=message_id,
            to=to,
            subject=subject,
            body=body, effect_reference=effect_reference,
        )
        response = self.transport.send_raw(raw=raw)
        provider_ref = str(response.get("id") or "").strip()
        if not provider_ref:
            # A response with no provider ID cannot be verified as a definite success.
            raise GmailOutcomeUnknown("Gmail send response did not contain a message id")
        return provider_ref

    def find_by_message_id(self, message_id: str) -> list[GmailMessage]:
        refs = self.transport.search(
            query=f"rfc822msgid:{message_id}",
            label_ids=("SENT",),
            max_results=10,
        )
        messages: list[GmailMessage] = []
        for ref in refs:
            provider_ref = str(ref.get("id") or "").strip()
            if not provider_ref:
                raise ProviderDataError("Gmail search returned a message without id")
            parsed = self._parse_raw(self.transport.get_raw(message_id=provider_ref))
            # Defense in depth: search result must actually carry the requested header and SENT label.
            if parsed.message_id == message_id and "SENT" in parsed.label_ids:
                messages.append(parsed)
        return messages

    def history_fence(self) -> str | None:
        # Deterministic/offline transports predate Gmail history.  They retain the
        # legacy RFC-ID path; real Google transport always supplies the fence.
        method = getattr(self.transport, "current_history_id", None)
        return str(method()) if method else None

    def find_by_effect_reference(self, *, history_start_id: str, effect_reference: str) -> list[GmailMessage]:
        try:
            refs=self.transport.history_message_refs(start_history_id=history_start_id)
        except GmailSearchUnavailable as exc:
            # Gmail history retention is bounded.  A stale fence never authorizes a
            # resend: use search only to discover candidates, which still undergo
            # exact sender/reference/payload verification in the effect executor.
            if "expired" not in str(exc).casefold():
                raise
            refs=self.transport.search(
                query=f'in:sent "Reference: {effect_reference}"',
                label_ids=("SENT",), max_results=100,
            )
        found=[]; seen=set()
        for ref in refs:
            mid=str(ref.get("id") or "")
            if not mid or mid in seen: continue
            seen.add(mid); msg=self._parse_raw(self.transport.get_raw(message_id=mid))
            if "SENT" in msg.label_ids and (f"Reference: {effect_reference}" in msg.body): found.append(msg)
        return found

    def latest_observation(self, customer_id: str, *, renewal_id: str | None = None) -> EmailObservation:
        contact = self.contacts.get(customer_id)
        if not contact:
            raise ProviderDataError(f"no verified Gmail contact mapping for customer {customer_id!r}")

        allowed_threads: tuple[str, ...] | None = None
        if renewal_id is not None:
            if renewal_id not in self.renewal_threads:
                # Missing scope configuration is not evidence of absence.
                return EmailObservation(
                    intent=EmailIntent.AMBIGUOUS,
                    source_ref=f"unscoped:{customer_id}:{renewal_id}",
                    sender_verified=False,
                )
            allowed_threads = tuple(self.renewal_threads[renewal_id])
            if not allowed_threads:
                # Empty configuration is an explicit operator assertion, not provider
                # proof.  Keep it usable for the dedicated synthetic initial-outreach
                # scenario, but label provenance honestly so audit/completion never calls
                # it a Gmail-observed absence.
                return EmailObservation(
                    intent=EmailIntent.NONE,
                    source_ref=f"operator-asserted-no-prior-thread:{customer_id}:{renewal_id}",
                    observed_at=datetime.now(timezone.utc),
                    sender_verified=False,
                    provenance="operator_assertion",
                )

            # Production transports expose direct authoritative-thread reads. This
            # avoids losing renewal evidence behind an arbitrary top-N sender search.
            if hasattr(self.transport, "thread_message_refs"):
                refs: list[dict[str, Any]] = []
                for thread_id in allowed_threads:
                    thread_refs = self.transport.thread_message_refs(thread_id=thread_id)
                    for ref in thread_refs:
                        if str(ref.get("threadId") or thread_id) == thread_id:
                            item = dict(ref)
                            item.setdefault("threadId", thread_id)
                            refs.append(item)
            else:
                # Backward-compatible fallback for deterministic test transports only.
                refs = self.transport.search(query=f"from:{contact}", max_results=self.read_search_limit)
                refs = [r for r in refs if str(r.get("threadId") or "") in set(allowed_threads)]
            if not refs:
                # A successfully-read configured thread with zero message refs carries no
                # customer intent. Real Gmail 404/authorization/provider failures are
                # raised by the transport and therefore never collapse into NONE.
                return EmailObservation(
                    intent=EmailIntent.NONE,
                    source_ref=f"authoritative-thread-no-messages:{customer_id}:{renewal_id}",
                    observed_at=datetime.now(timezone.utc),
                    sender_verified=True,
                )
        else:
            refs = self.transport.search(query=f"from:{contact}", max_results=self.read_search_limit)
            if not refs:
                return EmailObservation(intent=EmailIntent.NONE)

        resources = [self.transport.get_raw(message_id=str(ref.get("id") or "")) for ref in refs if ref.get("id")]
        if not resources:
            return EmailObservation(
                intent=EmailIntent.AMBIGUOUS if renewal_id is not None else EmailIntent.NONE,
                source_ref=(f"configured-thread-unreadable:{customer_id}:{renewal_id}" if renewal_id is not None else None),
                sender_verified=False,
            )

        def internal_date_ms(resource: dict[str, Any]) -> int:
            try:
                return int(resource.get("internalDate") or 0)
            except (TypeError, ValueError) as exc:
                raise ProviderDataError("Gmail message has invalid internalDate") from exc

        resources.sort(key=internal_date_ms, reverse=True)
        first_problem: EmailObservation | None = None
        newest_observed_at: datetime | None = None

        # Business truth comes from the newest *verified inbound customer* message,
        # not simply the newest message in the thread (which may be our own reply).
        for resource in resources:
            parsed = self._parse_raw(resource)
            raw_bytes = _b64url_decode(str(resource["raw"]))
            mime = BytesParser(policy=policy.default).parsebytes(raw_bytes)
            _, sender_addr = parseaddr(str(mime.get("From") or ""))
            sender_verified = normalize_email(sender_addr) == contact
            internal_ms = internal_date_ms(resource)
            observed_at = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc) if internal_ms > 0 else None
            if newest_observed_at is None and observed_at is not None:
                newest_observed_at = observed_at
            auto_submitted = str(mime.get("Auto-Submitted") or "").strip().casefold()
            subject = str(mime.get("Subject") or "").casefold()
            auto_reply = (auto_submitted not in {"", "no"}) or any(
                x in subject for x in ("out of office", "automatic reply", "auto-reply")
            )
            visible, quoted_only = _extract_inbound_text(mime)
            observation = EmailObservation(
                intent=_classify_intent(visible),
                message_id=parsed.message_id,
                source_ref=parsed.provider_ref,
                observed_at=observed_at,
                sender_verified=sender_verified,
                quoted_only=quoted_only,
                auto_reply=auto_reply,
            )
            if not sender_verified:
                if first_problem is None:
                    first_problem = observation
                continue
            if auto_reply or quoted_only:
                if first_problem is None:
                    first_problem = observation
                continue
            return observation

        if first_problem is not None:
            return first_problem

        # Authoritative thread(s) exist but contain no verified inbound customer
        # message. This is provider-derived absence, not a configuration assertion.
        return EmailObservation(
            intent=EmailIntent.NONE,
            source_ref=(
                f"authoritative-threads-no-customer-message:{customer_id}:{renewal_id}"
                if renewal_id is not None else None
            ),
            observed_at=newest_observed_at or datetime.now(timezone.utc),
            sender_verified=True,
        )
