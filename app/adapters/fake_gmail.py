from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.ports.gmail import GmailMessage, GmailOutcomeUnknown, GmailSearchUnavailable


# Backward-compatible names kept for existing tests and fault-injection scripts.
class TimeoutBeforeSuccess(GmailOutcomeUnknown):
    pass


class TimeoutAfterSuccess(GmailOutcomeUnknown):
    pass


class TransientProviderError(GmailOutcomeUnknown):
    pass


class SearchProviderError(GmailSearchUnavailable):
    pass


@dataclass(frozen=True)
class SentMessage:
    provider_ref: str
    message_id: str
    to: str
    subject: str
    body: str
    visible_after_search: int = 0
    history_id: int = 0

    def as_gmail_message(self) -> GmailMessage:
        return GmailMessage(
            provider_ref=self.provider_ref,
            message_id=self.message_id,
            to=(self.to,),
            cc=(),
            bcc=(),
            subject=self.subject,
            body=self.body,
            from_email="proofops@example.com",
            label_ids=("SENT",),
        )


class FakeGmailProvider:
    def __init__(self):
        self._messages: list[SentMessage] = []
        self._lock = Lock()
        self.mode = "success"
        self.calls = 0
        self.search_calls = 0
        self.visibility_delay_searches = 0
        self.search_failures_remaining = 0
        self._history_id = 100
        self.sender_email = "proofops@example.com"

    def configure(self, mode: str, *, visibility_delay_searches: int = 0, search_failures: int = 0) -> None:
        self.mode = mode
        self.visibility_delay_searches = max(0, visibility_delay_searches)
        self.search_failures_remaining = max(0, search_failures)

    def send(self, *, message_id: str, to: str, subject: str = "", body: str, effect_reference: str | None = None) -> str:
        with self._lock:
            self.calls += 1
            if self.mode == "timeout_before_success":
                raise TimeoutBeforeSuccess("request timed out before provider commit")
            if self.mode == "503_before_success":
                raise TransientProviderError("503")

            provider_ref = f"msg-{len(self._messages) + 1}"
            visible_after = self.search_calls + self.visibility_delay_searches
            self._history_id += 1
            if effect_reference:
                body = body + "\n\nReference: " + effect_reference
            self._messages.append(SentMessage(provider_ref, message_id, to, subject, body, visible_after, self._history_id))

            if self.mode == "timeout_after_success":
                raise TimeoutAfterSuccess("provider committed but response was lost")
            return provider_ref

    def find_by_message_id(self, message_id: str) -> list[GmailMessage]:
        with self._lock:
            self.search_calls += 1
            if self.search_failures_remaining > 0:
                self.search_failures_remaining -= 1
                raise SearchProviderError("search unavailable")
            return [
                m.as_gmail_message()
                for m in self._messages
                if m.message_id == message_id and self.search_calls > m.visible_after_search
            ]

    def history_fence(self) -> str:
        with self._lock:
            return str(self._history_id)

    def find_by_effect_reference(self, *, history_start_id: str, effect_reference: str) -> list[GmailMessage]:
        with self._lock:
            self.search_calls += 1
            if self.search_failures_remaining > 0:
                self.search_failures_remaining -= 1
                raise SearchProviderError("search unavailable")
            try:
                fence = int(history_start_id)
            except ValueError as exc:
                raise SearchProviderError("invalid history fence") from exc
            marker = "Reference: " + effect_reference
            return [
                m.as_gmail_message()
                for m in self._messages
                if m.history_id > fence and marker in m.body and self.search_calls > m.visible_after_search
            ]

    def preload(self, *, message_id: str, to: str, subject: str, body: str) -> SentMessage:
        with self._lock:
            provider_ref = f"msg-{len(self._messages) + 1}"
            self._history_id += 1
            msg = SentMessage(provider_ref, message_id, to, subject, body, 0, self._history_id)
            self._messages.append(msg)
            return msg

    @property
    def messages(self) -> tuple[SentMessage, ...]:
        with self._lock:
            return tuple(self._messages)
