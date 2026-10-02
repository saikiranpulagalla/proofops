from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.ports.gmail import GmailOutcomeUnknown, GmailSearchUnavailable, GmailSendRejected
from app.ports.protocols import ProviderDataError, ProviderUnauthorized
from app.domain.enums import ActionState, RunState
from app.persistence.repository import ActionExecutionError, LedgerRepository
from app.security.canonicalization import canonical_email_payload, canonical_json, semantic_payload_hash


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_message_id(logical_key: str, payload_hash: str) -> str:
    return f"<proofops-{sha256_text(logical_key + ':' + payload_hash)[:40]}@proofops.local>"


def effect_reference(logical_key: str, payload_hash: str) -> str:
    """Opaque, stable provider-observable correlation marker; never a database ID."""
    return "PO-" + sha256_text("effect-reference:" + logical_key + ":" + payload_hash)[:32]


class EffectExecutor:
    """Executes only already-authorized PREPARED actions.

    This class cannot create actions or approvals. ConsequentialActionService is the
    production entry point that performs policy checks and atomically claims approval.
    """

    def __init__(
        self,
        repo: LedgerRepository,
        gmail,
        *,
        reconciliation_grace_seconds: int = 30,
        min_absence_checks: int = 2,
        execution_lease_seconds: int = 30,
        now_fn=utcnow,
    ):
        self.repo = repo
        self.gmail = gmail
        self.reconciliation_grace_seconds = max(0, reconciliation_grace_seconds)
        self.min_absence_checks = max(1, min_absence_checks)
        self.execution_lease_seconds = max(1, execution_lease_seconds)
        self.now_fn = now_fn

    def _payload(self, action) -> dict:
        payload = json.loads(action.payload_json)
        if semantic_payload_hash(payload) != action.payload_hash:
            raise RuntimeError("persisted action payload hash mismatch")
        return payload

    def _verify_message(self, action, msg, *, reference: str | None = None) -> tuple[bool, str, str]:
        expected = self._payload(action)
        expected_to = tuple(expected.get("to", ()))
        # A plain-text sender must not accept a conflicting HTML alternative merely
        # because the parser happened to choose the approved plain part.  Normalize
        # line endings/NFC with the same canonicalizer used for effect payloads, then
        # require every recipient-visible alternative to agree exactly.
        variants = tuple(getattr(msg, "body_variants", ()) or (msg.body,))
        normalized_variants = {
            canonical_email_payload(
                customer_id="", renewal_id="", purpose="", to=(), subject="", body_text=value
            )["body_text"]
            for value in variants
        }
        if len(normalized_variants) != 1:
            return False, action.payload_hash, ""
        body = next(iter(normalized_variants))
        if reference:
            # A history/search candidate is only evidence if Gmail reports it as
            # sent by the authenticated identity.  Legacy deterministic providers
            # do not expose sender metadata and use the pre-RC9 RFC-ID path.
            expected_sender = getattr(self.gmail, "sender_email", None)
            if expected_sender and msg.from_email != expected_sender:
                return False, action.payload_hash, ""
            marker = "\n\nReference: " + reference
            if not body.endswith(marker):
                return False, action.payload_hash, ""
            body = body[:-len(marker)]
        observed = canonical_email_payload(
            customer_id=expected.get("customer_id", ""),
            renewal_id=expected.get("renewal_id", ""),
            purpose=expected.get("purpose", ""),
            to=msg.to,
            cc=msg.cc,
            bcc=msg.bcc,
            subject=msg.subject,
            body_text=body,
            thread_id=expected.get("thread_id"),
            attachments=expected.get("attachments", ()),
        )
        observed_hash = semantic_payload_hash(observed)
        exact = (
            tuple(observed.get("to", ())) == expected_to
            and observed.get("subject") == expected.get("subject")
            and observed.get("body_text") == expected.get("body_text")
            and observed_hash == action.payload_hash
        )
        return exact, action.payload_hash, observed_hash

    def _persist_confirmed_proof(self, *, action, effect, msg, observed_at: datetime):
        ok, expected_hash, observed_hash = self._verify_message(action, msg, reference=effect.provider_effect_reference)
        if not ok:
            return False
        self.repo.bind_effect_provider_observation(
            effect.id, provider_message_id=msg.provider_ref,
            provider_thread_id=msg.thread_id, provider_rfc_message_id=msg.message_id,
        )
        payload = self._payload(action)
        self.repo.confirm_effect_with_evidence(
            action_id=action.id,
            effect_id=effect.id,
            provider_ref=msg.provider_ref,
            customer_id=payload.get("customer_id"),
            renewal_id=payload.get("renewal_id"),
            evidence_type="gmail_sent_message",
            source_ref=msg.provider_ref,
            expected=expected_hash,
            observed=observed_hash,
            observed_at=observed_at,
            approval_id=effect.approval_id,
            now=observed_at,
        )
        return True

    def _matches_for_effect(self, effect):
        """Return fully enumerated evidence for this durable effect attempt.

        New attempts always persist a history fence and opaque reference before the
        provider can be invoked.  If a provider advertises only part of that protocol,
        fail closed rather than silently using caller-selected RFC Message-ID as the
        authority.  The RFC lookup remains only for historical attempts which cannot
        have an RC9+ fence.
        """
        if effect.provider_history_start_id or effect.provider_effect_reference:
            finder = getattr(self.gmail, "find_by_effect_reference", None)
            if not effect.provider_history_start_id or not effect.provider_effect_reference or not callable(finder):
                raise GmailSearchUnavailable("durable provider reconciliation capability is unavailable")
            return finder(
                history_start_id=effect.provider_history_start_id,
                effect_reference=effect.provider_effect_reference,
            )
        return self.gmail.find_by_message_id(effect.external_key)

    def ensure_confirmed_evidence(self, action_id: int, *, now: datetime | None = None) -> bool:
        """Repair the rare state where provider/action confirmation outlived proof persistence.

        Older builds could commit CONFIRMED and then crash before the evidence row was
        stored.  A confirmed action without its verified provider evidence is therefore
        not terminal proof; re-read Gmail using the durable deterministic external key.
        """
        now = now or self.now_fn()
        action = self.repo.get_action_by_id(action_id)
        if action is None:
            raise KeyError(action_id)
        if ActionState(action.state) != ActionState.CONFIRMED:
            return False
        effect = self.repo.latest_attempt(action_id)
        if effect is None or ActionState(effect.state) != ActionState.CONFIRMED:
            return False
        existing = self.repo.verified_evidence_for_effect(effect.id, "gmail_sent_message")
        if existing:
            return True
        try:
            matches = self._matches_for_effect(effect)
        except GmailSearchUnavailable:
            return False
        if len(matches) != 1:
            return False
        return self._persist_confirmed_proof(action=action, effect=effect, msg=matches[0], observed_at=now)

    def _execute_started_attempt(self, *, action, effect):
        """Execute one already-durable EXECUTING provider attempt.

        The caller must have persisted the effect before this method can touch Gmail. This
        makes the crash boundary explicit: once provider code is reachable there is always
        a durable effect row that recovery can reconcile.
        """
        if effect.action_id != action.id:
            raise ActionExecutionError("effect belongs to another action")
        if ActionState(action.state) != ActionState.EXECUTING or ActionState(effect.state) != ActionState.EXECUTING:
            raise ActionExecutionError("provider attempt is not durably EXECUTING")
        payload = self._payload(action)
        external_key = effect.external_key
        reference = effect_reference(action.logical_key, action.payload_hash)
        history_fence = getattr(self.gmail, "history_fence", None)
        finder = getattr(self.gmail, "find_by_effect_reference", None)
        if not callable(history_fence) or not callable(finder):
            # New consequential effects must not use the legacy RFC-ID-only recovery
            # path.  The durable effect exists, so keep it unresolved for an operator
            # instead of dispatching a send we cannot reconcile safely.
            self.repo.mark_action_and_effect_unknown(
                action.id, effect.id, "required Gmail history/reference reconciliation capability is unavailable"
            )
            self._run_to_unknown(action.run_id)
            return self.repo.get_action_by_id(action.id)
        try:
            fence = history_fence()
            if not fence:
                raise GmailSearchUnavailable("Gmail omitted pre-send history fence")
            effect = self.repo.set_effect_provider_fence(
                effect.id, history_start_id=str(fence), effect_reference=reference
            )
        except GmailSearchUnavailable as exc:
            self.repo.mark_action_and_effect_unknown(action.id, effect.id, f"pre-send history fence unavailable: {exc}")
            self._run_to_unknown(action.run_id)
            return self.repo.get_action_by_id(action.id)
        try:
            send_args = dict(message_id=external_key, to=payload["to"][0], subject=payload.get("subject", ""), body=payload.get("body_text", ""))
            if effect.provider_effect_reference:
                send_args["effect_reference"] = reference
            provider_ref = self.gmail.send(**send_args)
        except GmailOutcomeUnknown as exc:
            self.repo.mark_action_and_effect_unknown(action.id, effect.id, str(exc))
            self._run_to_unknown(action.run_id)
            return self.reconcile(action.id)
        except (GmailSendRejected, ProviderUnauthorized, ProviderDataError) as exc:
            self.repo.transition_attempt(effect.id, target=ActionState.FAILED, last_error=str(exc))
            self.repo.transition_action_state(action.id, ActionState.FAILED, expected=ActionState.EXECUTING)
            run = self.repo.get_run(action.run_id)
            if run and RunState(run.state) == RunState.EXECUTING:
                self.repo.transition_run_state(action.run_id, RunState.FAILED, expected=RunState.EXECUTING)
            return self.repo.get_action_by_id(action.id)

        # A provider response is not business proof. Read back and verify exact intent.
        try:
            matches = self._matches_for_effect(effect)
        except GmailSearchUnavailable as exc:
            self.repo.mark_action_and_effect_unknown(action.id, effect.id, f"verification search failed: {exc}")
            self._run_to_unknown(action.run_id)
            return self.repo.get_action_by_id(action.id)

        if len(matches) == 1 and matches[0].provider_ref == provider_ref:
            if self._persist_confirmed_proof(
                action=action,
                effect=effect,
                msg=matches[0],
                observed_at=self.now_fn(),
            ):
                return self.repo.get_action_by_id(action.id)

        self.repo.mark_action_and_effect_unknown(action.id, effect.id, "post-send verification did not prove exact intended message")
        self._run_to_unknown(action.run_id)
        return self.reconcile(action.id)

    def execute_started_email(self, *, action_id: int, effect_id: int):
        action = self.repo.get_action_by_id(action_id)
        effect = self.repo.get_effect(effect_id)
        if action is None or effect is None:
            raise KeyError(action_id, effect_id)
        return self._execute_started_attempt(action=action, effect=effect)

    def execute_prepared_email(self, *, action_id: int, approval_id: int | None = None):
        action = self.repo.get_action_by_id(action_id)
        if action is None:
            raise KeyError(action_id)
        state = ActionState(action.state)
        if state == ActionState.CONFIRMED:
            self.ensure_confirmed_evidence(action.id, now=self.now_fn())
            return action
        if state in {ActionState.UNKNOWN, ActionState.RECONCILING}:
            return self.reconcile(action.id)
        if state == ActionState.RETRYABLE:
            # RC10 could persist RETRYABLE after an empty search and then permit a
            # continuation send.  An old row must not preserve that unsafe authority.
            latest = self.repo.latest_attempt(action.id)
            if latest is None:
                raise ActionExecutionError("retryable action has no durable effect attempt")
            self.repo.mark_action_and_effect_unknown(
                action.id, latest.id, "RC11 blocks resend of an unresolved consequential effect"
            )
            self._run_to_unknown(action.run_id)
            return self.reconcile(action.id)
        if state != ActionState.PREPARED:
            raise ActionExecutionError(f"action is not prepared for execution: {state}")
        if state == ActionState.PREPARED and approval_id is None:
            raise ActionExecutionError("consequential email action has no claimed approval")

        external_key = deterministic_message_id(action.logical_key, action.payload_hash)
        effect = self.repo.start_attempt(
            action.id,
            "gmail",
            external_key,
            approval_id=approval_id,
            lease_seconds=self.execution_lease_seconds,
        )
        action = self.repo.get_action_by_id(action.id)
        return self._execute_started_attempt(action=action, effect=effect)

    def _run_to_unknown(self, run_id: int) -> None:
        run = self.repo.get_run(run_id)
        if run and RunState(run.state) == RunState.EXECUTING:
            self.repo.transition_run_state(run_id, RunState.UNKNOWN, expected=RunState.EXECUTING)

    def _run_begin_reconcile(self, run_id: int) -> None:
        run = self.repo.get_run(run_id)
        if run is None:
            return
        state = RunState(run.state)
        if state == RunState.UNKNOWN:
            self.repo.transition_run_state(run_id, RunState.RECONCILING, expected=RunState.UNKNOWN)
        elif state != RunState.RECONCILING:
            raise ActionExecutionError(f"run cannot reconcile from {state}")

    def _run_finish_reconcile(self, run_id: int, *, unresolved: bool = False, failed: bool = False) -> None:
        run = self.repo.get_run(run_id)
        if run is None or RunState(run.state) != RunState.RECONCILING:
            return
        target = RunState.FAILED if failed else (RunState.UNKNOWN if unresolved else RunState.EXECUTING)
        self.repo.transition_run_state(run_id, target, expected=RunState.RECONCILING)

    def reconcile(self, action_id: int, *, now: datetime | None = None):
        now = now or self.now_fn()
        action = self.repo.get_action_by_id(action_id)
        if action is None:
            raise KeyError(action_id)
        latest = self.repo.latest_attempt(action.id)
        if latest is None:
            raise RuntimeError("cannot reconcile without an attempt")
        state = ActionState(action.state)
        if state == ActionState.CONFIRMED:
            return action
        if state == ActionState.EXECUTING:
            # Only recovery may convert an expired EXECUTING lease to UNKNOWN first.
            raise ActionExecutionError("live EXECUTING action cannot be reconciled before its lease expires")
        if state not in {ActionState.UNKNOWN, ActionState.RECONCILING}:
            return action

        self._run_begin_reconcile(action.run_id)
        if state == ActionState.UNKNOWN:
            self.repo.begin_reconciliation(action.id, latest.id)
        latest = self.repo.latest_attempt(action.id)
        try:
            matches = self._matches_for_effect(latest)
        except GmailSearchUnavailable as exc:
            self.repo.finish_reconciliation(action.id, latest.id, ActionState.UNKNOWN, error=f"reconciliation search failed: {exc}")
            self._run_finish_reconcile(action.run_id, unresolved=True)
            return self.repo.get_action_by_id(action.id)

        if len(matches) == 1:
            msg = matches[0]
            ok, _expected_hash, _observed_hash = self._verify_message(action, msg, reference=latest.provider_effect_reference)
            if ok:
                # Keep both objects in RECONCILING until proof persistence succeeds;
                # confirmation + evidence + approval consumption then commit atomically.
                self._persist_confirmed_proof(action=action, effect=latest, msg=msg, observed_at=now)
                self._run_finish_reconcile(action.run_id, unresolved=False)
            else:
                # Same deterministic transport key but different content is a safety anomaly.
                self.repo.finish_reconciliation(action.id, latest.id, ActionState.FAILED, error="message identity matched but semantic payload did not")
                self._run_finish_reconcile(action.run_id, failed=True)
            return self.repo.get_action_by_id(action.id)

        if len(matches) > 1:
            self.repo.finish_reconciliation(action.id, latest.id, ActionState.UNKNOWN, error="ambiguous reconciliation: multiple provider effects")
            self._run_finish_reconcile(action.run_id, unresolved=True)
            return self.repo.get_action_by_id(action.id)

        # A successful empty provider read is not proof that a prior consequential
        # call did not commit.  Eventual consistency, index lag, expired history and
        # lost responses are all plausible.  Time and repetition may guide operator
        # follow-up but can never authorize a second send for this logical effect.
        self.repo.finish_reconciliation(
            action.id,
            latest.id,
            ActionState.UNKNOWN,
            error="no uniquely verified provider candidate; outcome remains unresolved and resend is prohibited",
        )
        self._run_finish_reconcile(action.run_id, unresolved=True)
        return self.repo.get_action_by_id(action.id)

    def recover_expired_executions(self, *, now: datetime | None = None) -> list[int]:
        now = now or self.now_fn()
        recovered: list[int] = []
        for effect in self.repo.expired_executing_attempts(now=now):
            action = self.repo.get_action_by_id(effect.action_id)
            if action is None:
                continue
            # A crashed worker may have called the provider. Treat the outcome as uncertain,
            # never as a safe retry, even if the crash happened before the actual provider call.
            self.repo.mark_action_and_effect_unknown(action.id, effect.id, "execution lease expired after process interruption")
            self._run_to_unknown(action.run_id)
            self.reconcile(action.id, now=now)
            recovered.append(action.id)
        return recovered

    def recover_expired_execution(self, action_id: int, *, now: datetime | None = None) -> bool:
        """Recover only the requested run/action; global sweeps belong to a recovery worker."""
        now = now or self.now_fn()
        effect = self.repo.expired_executing_attempt_for_action(action_id, now=now)
        if effect is None:
            return False
        action = self.repo.get_action_by_id(action_id)
        if action is None:
            return False
        self.repo.mark_action_and_effect_unknown(
            action.id, effect.id, "execution lease expired after process interruption"
        )
        self._run_to_unknown(action.run_id)
        self.reconcile(action.id, now=now)
        return True
