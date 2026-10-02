from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.adapters.identity_bound_gmail import CRMIdentityBoundGmailProvider
from sqlalchemy import text

from app.api.app import ApiSettings, create_app
from app.api.live_runtime import (
    LiveConfigurationError,
    LiveRuntimeConfig,
    assert_contact_bindings,
    assert_database_revision,
    build_live_runtime,
)
from app.domain.models import CustomerRecord, EmailObservation, utcnow
from app.domain.enums import EmailIntent
from app.persistence.db import make_engine
from app.ports.protocols import ProviderConflict
from app.version import ALEMBIC_HEAD, RELEASE_STAGE, __version__

ROOT_FOR_TESTS = Path(__file__).resolve().parents[1]


def valid_env(**overrides):
    env = {
        "PROOFOPS_DATABASE_URL": "postgresql+psycopg://u:p@db.example/proofops",
        "PROOFOPS_GOOGLE_TOKEN_FILE": "/secrets/token.json",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet-123",
        "PROOFOPS_SHEETS_SHEET_NAME": "Customers",
        "PROOFOPS_GMAIL_SENDER": "proofops-test@example.com",
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS": "proofops-test@example.com,alice@acme.com",
        "PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS": "YES",
        "PROOFOPS_SYNTHETIC_DATASET": "YES",
        "PROOFOPS_CONTACTS_JSON": '{"SMOKE-C001":"alice@acme.com"}',
        "PROOFOPS_RENEWAL_THREADS_JSON": '{"SMOKE-R1":["thread-1"]}',
        "GEMINI_API_KEY": "synthetic-key",
    }
    env.update(overrides)
    return env


def test_live_config_parses_explicit_release_dependencies():
    cfg = LiveRuntimeConfig.from_env(valid_env())
    assert cfg.database_url.startswith("postgresql")
    assert cfg.gmail_sender == "proofops-test@example.com"
    assert cfg.contacts == {"SMOKE-C001": "alice@acme.com"}
    assert cfg.renewal_threads == {"SMOKE-R1": ("thread-1",)}
    assert cfg.gemini_model == "gemini-3.8-flash"
    assert cfg.min_absence_checks == 2


@pytest.mark.parametrize("name", [
    "PROOFOPS_DATABASE_URL",
    "PROOFOPS_GOOGLE_TOKEN_FILE",
    "PROOFOPS_SHEETS_SPREADSHEET_ID",
    "PROOFOPS_GMAIL_SENDER",
    "PROOFOPS_CONTACTS_JSON",
    "GEMINI_API_KEY",
    "PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS",
    "PROOFOPS_SYNTHETIC_DATASET",
])
def test_live_config_fails_closed_when_required_input_missing(name):
    env = valid_env()
    env.pop(name)
    with pytest.raises(LiveConfigurationError):
        LiveRuntimeConfig.from_env(env)


def test_live_config_refuses_sqlite_as_release_database():
    with pytest.raises(LiveConfigurationError, match="PostgreSQL"):
        LiveRuntimeConfig.from_env(valid_env(PROOFOPS_DATABASE_URL="sqlite:///local.sqlite"))


def test_live_config_rejects_invalid_binding_json_and_duplicate_threads():
    with pytest.raises(LiveConfigurationError):
        LiveRuntimeConfig.from_env(valid_env(PROOFOPS_CONTACTS_JSON="[]"))
    with pytest.raises(LiveConfigurationError, match="duplicate Gmail thread"):
        LiveRuntimeConfig.from_env(valid_env(PROOFOPS_RENEWAL_THREADS_JSON='{"SMOKE-R1":["t","t"]}'))


def test_exact_alembic_head_is_required():
    engine = make_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version(version_num) VALUES (:v)"), {"v": ALEMBIC_HEAD})
    assert_database_revision(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='old'"))
    with pytest.raises(LiveConfigurationError, match="revision mismatch"):
        assert_database_revision(engine)


class FakeCRM:
    def __init__(self, records):
        self.records = {r.customer_id: r for r in records}

    def get_customer(self, customer_id):
        return self.records.get(customer_id)


def test_contact_bindings_must_match_crm_and_known_renewal():
    customer = CustomerRecord(
        customer_id="SMOKE-C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="SMOKE-R1", renewal_status="pending", renewal_date=utcnow(), version=1,
    )
    crm = FakeCRM([customer])
    assert_contact_bindings(crm, {"SMOKE-C001": "alice@acme.com"}, {"SMOKE-R1": ("thread-1",)})
    with pytest.raises(LiveConfigurationError, match="disagrees"):
        assert_contact_bindings(crm, {"SMOKE-C001": "mallory@example.com"}, {})
    with pytest.raises(LiveConfigurationError, match="unconfigured renewals"):
        assert_contact_bindings(crm, {"SMOKE-C001": "alice@acme.com"}, {"SMOKE-R999": ("thread-x",)})


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Users:
    def __init__(self, email):
        self.email = email

    def getProfile(self, userId):
        assert userId == "me"
        return _Execute({"emailAddress": self.email})


class FakeGmailService:
    def __init__(self, email):
        self.email = email

    def users(self):
        return _Users(self.email)


class FakePlanner:
    model_name = "fake-live-planner"

    def propose(self, _context):  # pragma: no cover - composition only
        raise AssertionError("planner should not be called while building runtime")


def test_live_runtime_composes_real_adapter_types_without_fake_fallback():
    cfg = LiveRuntimeConfig.from_env(valid_env())
    engine = make_engine("sqlite:///:memory:")
    captured = {}

    def creds(path, *, scopes):
        captured["token"] = path
        captured["scopes"] = tuple(scopes)
        return object()

    def planner_factory(**kwargs):
        captured["planner"] = kwargs
        return FakePlanner()

    runtime = build_live_runtime(
        cfg,
        credential_loader=creds,
        sheets_service_builder=lambda **_: object(),
        gmail_service_builder=lambda **_: FakeGmailService("proofops-test@example.com"),
        planner_factory=planner_factory,
        engine_factory=lambda _url: engine,
        schema_checker=lambda _engine: None,
        binding_checker=lambda _crm, _contacts, _threads: None,
    )
    assert runtime.workflow.crm.__class__.__name__ == "GoogleSheetsCRM"
    assert runtime.workflow.gmail_read.__class__.__name__ == "CRMIdentityBoundGmailProvider"
    assert runtime.workflow.consequential.effects.gmail is runtime.workflow.gmail_read
    assert captured["token"] == "/secrets/token.json"
    assert set(captured["scopes"]) == {
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/gmail.modify",
    }
    assert captured["planner"] == {"api_key": "synthetic-key", "model": "gemini-3.8-flash"}


def test_live_runtime_rejects_authenticated_sender_mismatch():
    cfg = LiveRuntimeConfig.from_env(valid_env())
    engine = make_engine("sqlite:///:memory:")
    with pytest.raises(LiveConfigurationError, match="does not match"):
        build_live_runtime(
            cfg,
            credential_loader=lambda *_args, **_kwargs: object(),
            sheets_service_builder=lambda **_: object(),
            gmail_service_builder=lambda **_: FakeGmailService("different@example.com"),
            planner_factory=lambda **_: FakePlanner(),
            engine_factory=lambda _url: engine,
            schema_checker=lambda _engine: None,
            binding_checker=lambda *_args: None,
        )


def test_release_identity_is_single_source_for_http_health():
    settings = ApiSettings(secret_key="x" * 40, demo_password="long-enough-password", login_enabled=False)
    client = TestClient(create_app(settings=settings))
    health = client.get("/health").json()
    assert health["version"] == __version__ == "1.0.0rc11"
    assert health["stage"] == RELEASE_STAGE == "release-candidate"
    assert client.get("/ready").status_code == 503


def test_release_preflight_require_live_fails_when_live_prerequisites_are_pending(tmp_path):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("PROOFOPS_") and k != "GEMINI_API_KEY"}
    result = subprocess.run(
        [sys.executable, str(ROOT_FOR_TESTS / "scripts" / "qualify_v10.py"), "--require-live"],
        cwd=ROOT_FOR_TESTS,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "live_configuration" in result.stdout
    assert "PENDING" in result.stdout


@pytest.mark.parametrize(
    "script_name",
    [
        "smoke_v10_sheets_live.py",
        "smoke_v10_gmail_live.py",
        "smoke_v10_gemini_live.py",
        "smoke_v10_postgres_live.py",
        "smoke_v10_e2e_live.py",
    ],
)
def test_live_smoke_scripts_refuse_without_explicit_opt_in(script_name):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("PROOFOPS_ALLOW_LIVE_")}
    result = subprocess.run(
        [sys.executable, str(ROOT_FOR_TESTS / "scripts" / script_name)],
        cwd=ROOT_FOR_TESTS,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "Refusing" in result.stderr


def test_live_runtime_requires_explicit_provider_effect_and_synthetic_dataset_flags():
    for key in ("PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS", "PROOFOPS_SYNTHETIC_DATASET"):
        env = valid_env()
        env.pop(key)
        with pytest.raises(LiveConfigurationError):
            LiveRuntimeConfig.from_env(env)


def test_live_config_rejects_non_synthetic_customer_or_renewal_ids():
    with pytest.raises(LiveConfigurationError, match="customer IDs"):
        LiveRuntimeConfig.from_env(valid_env(PROOFOPS_CONTACTS_JSON='{"C001":"alice@acme.com"}'))
    with pytest.raises(LiveConfigurationError, match="renewal IDs"):
        LiveRuntimeConfig.from_env(valid_env(PROOFOPS_RENEWAL_THREADS_JSON='{"R1":["thread"]}'))


class FakeGmailDelegate:
    def __init__(self):
        self.calls = []

    def latest_observation(self, customer_id, *, renewal_id=None):
        self.calls.append((customer_id, renewal_id))
        return EmailObservation(intent=EmailIntent.NONE)

    def send(self, **kwargs):
        return "provider-1"

    def find_by_message_id(self, message_id):
        return []


def test_crm_identity_bound_gmail_rechecks_contact_each_read():
    customer = CustomerRecord(
        customer_id="SMOKE-C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="SMOKE-R1", renewal_status="pending", renewal_date=utcnow(), version=1,
    )
    crm = FakeCRM([customer])
    delegate = FakeGmailDelegate()
    gmail = CRMIdentityBoundGmailProvider(delegate, crm, configured_contacts={"SMOKE-C001": "alice@acme.com"})
    assert gmail.latest_observation("SMOKE-C001", renewal_id="SMOKE-R1").intent == EmailIntent.NONE
    crm.records["SMOKE-C001"] = customer.model_copy(update={"contact_email": "changed@example.com", "version": 2})
    with pytest.raises(ProviderConflict, match="contact changed"):
        gmail.latest_observation("SMOKE-C001", renewal_id="SMOKE-R1")


def test_crm_identity_bound_gmail_rechecks_renewal_identity():
    customer = CustomerRecord(
        customer_id="SMOKE-C001", company="ACME", contact_email="alice@acme.com",
        renewal_id="SMOKE-R1", renewal_status="pending", renewal_date=utcnow(), version=1,
    )
    gmail = CRMIdentityBoundGmailProvider(FakeGmailDelegate(), FakeCRM([customer]), configured_contacts={"SMOKE-C001": "alice@acme.com"})
    with pytest.raises(ProviderConflict, match="renewal identity"):
        gmail.latest_observation("SMOKE-C001", renewal_id="SMOKE-R2")
