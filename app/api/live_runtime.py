from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import text

from app.adapters.gemini_planner import GeminiPlanner
from app.adapters.google_auth import GMAIL_MODIFY_SCOPE, SHEETS_READ_WRITE_SCOPE, load_user_credentials
from app.adapters.google_gmail import GoogleApiGmailTransport, GoogleGmailProvider, build_google_gmail_service
from app.ports.gmail import GmailOutcomeUnknown
from app.adapters.identity_bound_gmail import CRMIdentityBoundGmailProvider
from app.adapters.google_sheets import GoogleApiSheetsTransport, GoogleSheetsCRM, SheetsCRMConfig, build_google_sheets_service
from app.api.app import ApiRuntime
from app.engine.consequential import ConsequentialActionService
from app.engine.effects import EffectExecutor
from app.engine.orchestrator import RunCoordinator
from app.engine.workflow import RenewalWorkflowV08
from app.persistence.db import make_engine, make_session_factory
from app.persistence.repository import LedgerRepository
from app.planning.service import PlanningService
from app.security.approval import ApprovalService
from app.security.canonicalization import normalize_email
from app.version import ALEMBIC_HEAD


class LiveConfigurationError(RuntimeError):
    pass


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if not value:
        raise LiveConfigurationError(f"{name} is required for live runtime")
    return value


def _json_object(env: Mapping[str, str], name: str, *, required: bool = False) -> dict[str, Any]:
    raw = str(env.get(name, "")).strip()
    if not raw:
        if required:
            raise LiveConfigurationError(f"{name} is required for live runtime")
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveConfigurationError(f"{name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise LiveConfigurationError(f"{name} must be a JSON object")
    return value


@dataclass(frozen=True)
class LiveRuntimeConfig:
    database_url: str
    token_file: str
    spreadsheet_id: str
    sheet_name: str
    gmail_sender: str
    contacts: dict[str, str]
    renewal_threads: dict[str, tuple[str, ...]]
    allowed_test_recipients: frozenset[str]
    gemini_api_key: str
    gemini_model: str = "gemini-3.8-flash"
    reconciliation_grace_seconds: int = 10
    min_absence_checks: int = 2
    approval_continuation_ttl_seconds: int = 900
    qualification_mode: bool = False
    qualification_fault_mode: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LiveRuntimeConfig":
        env = os.environ if env is None else env
        database_url = _required(env, "PROOFOPS_DATABASE_URL")
        # V1.0 live qualification is intentionally tied to the target Postgres path.
        # SQLite remains supported for tests/demo mode but cannot masquerade as the
        # release deployment database.
        if not database_url.startswith("postgresql"):
            raise LiveConfigurationError("PROOFOPS_DATABASE_URL must use PostgreSQL in live runtime")

        token_file = _required(env, "PROOFOPS_GOOGLE_TOKEN_FILE")
        spreadsheet_id = _required(env, "PROOFOPS_SHEETS_SPREADSHEET_ID")
        sheet_name = str(env.get("PROOFOPS_SHEETS_SHEET_NAME", "Customers")).strip() or "Customers"
        sender = normalize_email(_required(env, "PROOFOPS_GMAIL_SENDER"))
        if not sender:
            raise LiveConfigurationError("PROOFOPS_GMAIL_SENDER must be a valid email address")

        if str(env.get("PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS", "")).strip() != "YES":
            raise LiveConfigurationError("PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS=YES is required for live runtime")
        if str(env.get("PROOFOPS_SYNTHETIC_DATASET", "")).strip() != "YES":
            raise LiveConfigurationError("PROOFOPS_SYNTHETIC_DATASET=YES is required for the qualified live runtime")

        contacts_raw = _json_object(env, "PROOFOPS_CONTACTS_JSON", required=True)
        contacts: dict[str, str] = {}
        for customer_id, value in contacts_raw.items():
            cid = str(customer_id).strip()
            email = normalize_email(str(value))
            if not cid or not email:
                raise LiveConfigurationError("PROOFOPS_CONTACTS_JSON contains an invalid customer/email mapping")
            if not cid.startswith(("SMOKE-", "DEMO-")):
                raise LiveConfigurationError("qualified live customer IDs must start with SMOKE- or DEMO-")
            contacts[cid] = email
        if not contacts:
            raise LiveConfigurationError("PROOFOPS_CONTACTS_JSON must contain at least one synthetic customer")

        allowed_raw = str(env.get("PROOFOPS_ALLOWED_TEST_RECIPIENTS", "")).strip()
        allowed_test_recipients = frozenset(
            filter(None, (normalize_email(x) for x in allowed_raw.split(",")))
        )
        if not allowed_test_recipients:
            raise LiveConfigurationError("PROOFOPS_ALLOWED_TEST_RECIPIENTS is required for qualified live runtime")
        if sender not in allowed_test_recipients:
            raise LiveConfigurationError("authenticated Gmail sender must be in PROOFOPS_ALLOWED_TEST_RECIPIENTS")
        disallowed = sorted(set(contacts.values()) - set(allowed_test_recipients))
        if disallowed:
            raise LiveConfigurationError(f"configured contact recipients are not allowlisted: {disallowed}")
        qualification_mode = str(env.get("PROOFOPS_QUALIFICATION_MODE", "")).strip() == "YES"
        qualification_fault_mode = str(env.get("PROOFOPS_QUALIFICATION_FAULT_MODE", "")).strip() or None
        if qualification_fault_mode and not qualification_mode:
            raise LiveConfigurationError("qualification fault mode is allowed only when PROOFOPS_QUALIFICATION_MODE=YES")
        if qualification_fault_mode not in {None, "gmail_commit_then_lose_first_response"}:
            raise LiveConfigurationError("unsupported PROOFOPS_QUALIFICATION_FAULT_MODE")
        if qualification_mode:
            # Final V1 qualification is deliberately self-send-only.  A SMOKE label
            # or operator allowlist is not sufficient proof that an address is synthetic.
            if allowed_test_recipients != frozenset({sender}):
                raise LiveConfigurationError("qualification mode requires the authenticated Gmail sender to be the only allowed recipient")
            wrong = sorted(email for email in contacts.values() if email != sender)
            if wrong:
                raise LiveConfigurationError("qualification mode requires every synthetic CRM contact to equal the authenticated Gmail test mailbox")

        threads_raw = _json_object(env, "PROOFOPS_RENEWAL_THREADS_JSON")
        renewal_threads: dict[str, tuple[str, ...]] = {}
        for renewal_id, values in threads_raw.items():
            rid = str(renewal_id).strip()
            if not rid or not isinstance(values, list):
                raise LiveConfigurationError("PROOFOPS_RENEWAL_THREADS_JSON values must be arrays")
            if not rid.startswith(("SMOKE-", "DEMO-")):
                raise LiveConfigurationError("qualified live renewal IDs must start with SMOKE- or DEMO-")
            cleaned = tuple(str(v).strip() for v in values if str(v).strip())
            if len(set(cleaned)) != len(cleaned):
                raise LiveConfigurationError(f"duplicate Gmail thread id for renewal {rid!r}")
            renewal_threads[rid] = cleaned

        model = str(env.get("PROOFOPS_GEMINI_MODEL", "gemini-3.8-flash")).strip()
        if not model:
            raise LiveConfigurationError("PROOFOPS_GEMINI_MODEL cannot be blank")
        api_key = _required(env, "GEMINI_API_KEY")

        try:
            grace = int(str(env.get("PROOFOPS_RECONCILIATION_GRACE_SECONDS", "10")))
            checks = int(str(env.get("PROOFOPS_MIN_ABSENCE_CHECKS", "2")))
            continuation_ttl = int(str(env.get("PROOFOPS_APPROVAL_CONTINUATION_TTL_SECONDS", "900")))
        except ValueError as exc:
            raise LiveConfigurationError("reconciliation settings must be integers") from exc
        if grace < 10 or checks < 2:
            raise LiveConfigurationError("live reconciliation grace must be >=10 seconds and min absence checks must be >=2")
        if continuation_ttl < 60 or continuation_ttl > 3600:
            raise LiveConfigurationError("live approval continuation TTL must be between 60 and 3600 seconds")

        return cls(
            database_url=database_url,
            token_file=token_file,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            gmail_sender=sender,
            contacts=contacts,
            renewal_threads=renewal_threads,
            allowed_test_recipients=allowed_test_recipients,
            gemini_api_key=api_key,
            gemini_model=model,
            reconciliation_grace_seconds=grace,
            min_absence_checks=checks,
            approval_continuation_ttl_seconds=continuation_ttl,
            qualification_mode=qualification_mode,
            qualification_fault_mode=qualification_fault_mode,
        )


def assert_database_revision(engine, expected_revision: str = ALEMBIC_HEAD) -> None:
    """Fail closed unless the database has exactly the qualified Alembic head."""
    try:
        with engine.connect() as conn:
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        raise LiveConfigurationError(f"database schema revision could not be verified: {exc}") from exc
    if revision != expected_revision:
        raise LiveConfigurationError(
            f"database schema revision mismatch: expected {expected_revision}, found {revision}"
        )



def assert_contact_bindings(crm: GoogleSheetsCRM, contacts: Mapping[str, str], renewal_threads: Mapping[str, tuple[str, ...]]) -> None:
    """Verify configured Gmail identities are the same identities in the CRM snapshot.

    Live runtime must never read inbound mail for one contact while the trusted CRM would
    send to another.  This startup check is read-only and fails closed on disagreement.
    """
    configured_renewals: set[str] = set()
    for customer_id, configured_email in contacts.items():
        customer = crm.get_customer(customer_id)
        if customer is None:
            raise LiveConfigurationError(f"configured Gmail customer {customer_id!r} is missing from Sheets CRM")
        crm_email = normalize_email(customer.contact_email or "")
        if not crm_email or crm_email != normalize_email(configured_email):
            raise LiveConfigurationError(f"Gmail contact binding disagrees with CRM for customer {customer_id!r}")
        if not customer.customer_id.startswith(("SMOKE-", "DEMO-")) or not customer.renewal_id.startswith(("SMOKE-", "DEMO-")):
            raise LiveConfigurationError("qualified live CRM identities must use SMOKE-/DEMO- prefixes")
        configured_renewals.add(customer.renewal_id)
    unknown_thread_bindings = set(renewal_threads) - configured_renewals
    if unknown_thread_bindings:
        raise LiveConfigurationError(
            f"Gmail renewal-thread bindings reference unconfigured renewals: {sorted(unknown_thread_bindings)}"
        )
    missing_thread_bindings = configured_renewals - set(renewal_threads)
    if missing_thread_bindings:
        raise LiveConfigurationError(
            "every qualified live renewal requires an explicit Gmail thread binding; "
            f"use an empty list only after verifying no prior conversation exists: {sorted(missing_thread_bindings)}"
        )

def _verified_profile_email(gmail_service: Any, expected: str) -> str:
    try:
        profile = gmail_service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise LiveConfigurationError(f"Gmail profile verification failed: {exc}") from exc
    actual = normalize_email(str(profile.get("emailAddress") or ""))
    if not actual:
        raise LiveConfigurationError("Gmail profile did not return a valid emailAddress")
    if actual != normalize_email(expected):
        raise LiveConfigurationError(
            "PROOFOPS_GMAIL_SENDER does not match the authenticated Google account"
        )
    return actual


class QualificationCommitThenLoseFirstResponse:
    """Qualification-only fault: commit one Gmail send, then lose its response.

    The first reconciliation lookup is also hidden once so the API must expose UNKNOWN
    before a later resume finds the real message. All other provider behavior delegates
    unchanged.
    """

    def __init__(self, delegate):
        self.delegate = delegate
        self.injected = False
        self._hide_searches_remaining = 0

    def send(self, **kwargs):
        provider_ref = self.delegate.send(**kwargs)
        if not self.injected:
            self.injected = True
            self._hide_searches_remaining = 1
            raise GmailOutcomeUnknown("qualification fault: Gmail committed but response was lost")
        return provider_ref

    def find_by_message_id(self, message_id):
        if self._hide_searches_remaining > 0:
            self._hide_searches_remaining -= 1
            return []
        return self.delegate.find_by_message_id(message_id)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


def build_live_runtime(
    config: LiveRuntimeConfig,
    *,
    credential_loader: Callable[..., Any] = load_user_credentials,
    sheets_service_builder: Callable[..., Any] = build_google_sheets_service,
    gmail_service_builder: Callable[..., Any] = build_google_gmail_service,
    planner_factory: Callable[..., Any] = GeminiPlanner,
    engine_factory: Callable[[str], Any] = make_engine,
    schema_checker: Callable[[Any], None] = assert_database_revision,
    binding_checker: Callable[[GoogleSheetsCRM, Mapping[str, str], Mapping[str, tuple[str, ...]]], None] = assert_contact_bindings,
) -> ApiRuntime:
    """Compose the real provider runtime without a fake-provider fallback.

    The dependency arguments exist for deterministic qualification tests.  The default
    production path uses the actual Google/Gemini adapters and exact database revision.
    """
    engine = engine_factory(config.database_url)
    schema_checker(engine)
    repo = LedgerRepository(make_session_factory(engine))

    credentials = credential_loader(
        config.token_file,
        scopes=[SHEETS_READ_WRITE_SCOPE, GMAIL_MODIFY_SCOPE],
    )
    sheets_service = sheets_service_builder(credentials=credentials)
    gmail_service = gmail_service_builder(credentials=credentials)
    sender = _verified_profile_email(gmail_service, config.gmail_sender)

    crm = GoogleSheetsCRM(
        GoogleApiSheetsTransport(sheets_service),
        SheetsCRMConfig(spreadsheet_id=config.spreadsheet_id, sheet_name=config.sheet_name),
    )
    binding_checker(crm, config.contacts, config.renewal_threads)

    gmail_transport = GoogleGmailProvider(
        GoogleApiGmailTransport(gmail_service),
        sender_email=sender,
        contacts=config.contacts,
        renewal_threads=config.renewal_threads,
    )
    gmail = CRMIdentityBoundGmailProvider(
        gmail_transport, crm, configured_contacts=config.contacts,
        allowed_recipients=config.allowed_test_recipients,
    )

    planner = planner_factory(api_key=config.gemini_api_key, model=config.gemini_model)
    approvals = ApprovalService(repo, continuation_ttl_seconds=config.approval_continuation_ttl_seconds)
    effect_gmail = gmail
    if config.qualification_fault_mode == "gmail_commit_then_lose_first_response":
        effect_gmail = QualificationCommitThenLoseFirstResponse(gmail)
    effects = EffectExecutor(
        repo,
        effect_gmail,
        reconciliation_grace_seconds=config.reconciliation_grace_seconds,
        min_absence_checks=config.min_absence_checks,
    )
    consequential = ConsequentialActionService(repo, effects, approvals)
    workflow = RenewalWorkflowV08(
        repo=repo,
        crm=crm,
        gmail_read=gmail,
        planning=PlanningService(repo, planner),
        consequential=consequential,
        approvals=approvals,
        coordinator=RunCoordinator(repo),
    )

    def provider_probe() -> dict[str, str]:
        # This endpoint is explicitly separate from the load-balancer readiness probe.
        # It performs live provider checks and may consume a tiny Gemini request.
        first_customer_id = next(iter(config.contacts))
        customer = crm.get_customer(first_customer_id)
        if customer is None:
            raise LiveConfigurationError("Sheets provider probe could not read configured synthetic customer")
        _verified_profile_email(gmail_service, config.gmail_sender)
        raw_transport = gmail_transport.transport
        if hasattr(raw_transport, "thread_message_refs"):
            for renewal_id, thread_ids in config.renewal_threads.items():
                for thread_id in thread_ids:
                    refs = raw_transport.thread_message_refs(thread_id=thread_id)
                    if not refs:
                        raise LiveConfigurationError(
                            f"configured Gmail thread {thread_id!r} for renewal {renewal_id!r} is unavailable or empty"
                        )
        probe = getattr(planner, "probe", None)
        if probe is None:
            raise LiveConfigurationError("Gemini planner has no readiness probe")
        probe()
        return {"postgresql": "ok", "sheets": "ok", "gmail": "ok", "gemini": "ok"}

    return ApiRuntime(workflow=workflow, repo=repo, approvals=approvals, provider_probe=provider_probe)
