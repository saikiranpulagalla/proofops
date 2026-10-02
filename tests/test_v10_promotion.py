from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.release.qualification import (
    LIVE_GATES,
    build_promotion_evidence,
    safe_database_identity,
    validate_receipt_bundle,
    write_receipt,
)
from app.version import ALEMBIC_HEAD, RELEASE_LABEL, __version__

ROOT = Path(__file__).resolve().parents[1]

ARTIFACT_DIGEST = "a" * 64


def _valid_metrics(gate: str) -> dict:
    return {
        "postgresql": {
            "transaction_readback": True,
            "business_operation_single_owner": True,
            "approval_single_claimant": True,
            "effect_single_active_attempt": True,
            "idempotency_single_creator": True,
        },
        "sheets": {
            "stale_version_rejected": True, "verified_readback": True, "version_delta": 2,
        },
        "gmail": {
            "verified_sent_evidence_rows": 1, "physical_messages_for_logical_effect": 1,
            "response_loss_injected": True, "unknown_observed": True,
            "reconciled_without_resend": True, "provider_message_id_bound": True,
            "authoritative_thread_intent": "ACCEPTED", "sender_verified": True,
        },
        "gemini": {
            "scenario_count": 25, "trials": 2,
            "critical_scenario_count": 10,
            "deterministic_containment_passed": 50, "deterministic_containment_total": 50,
            "critical_semantic_passed": 20, "critical_semantic_total": 20,
            "semantic_passed": 48, "semantic_total": 50, "semantic_rate": 0.96,
        },
        "deployed_e2e": {
            "completed_verified": True, "verified_gmail_evidence_rows": 1,
            "physical_messages_for_logical_effect": 1, "cold_restart": True,
            # RC6 treated a worker-generation increase as restart proof.  That was
            # unsafe because autoscaling can start another worker without rollout.
            "actual_deployment_restart": True, "deployment_revision_changed": True,
            "runtime_payload_attested": True, "deployed_api_initial_effect": True,
            "deployed_source_attested": True, "deployed_environment_attested": True,
            "deployed_configuration_attested": True,
            "response_loss_injected": True, "unknown_observed": True,
            "reconciled_without_resend": True, "accepted_customer_zero_send": True,
            "repeat_goal_state": "DEFERRED",
        },
    }[gate]


def _emit_bundle(tmp_path: Path, *, run_id: str = "run-1") -> Path:
    env = {
        "PROOFOPS_QUALIFICATION_RUN_ID": run_id,
        "PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(tmp_path),
        "PROOFOPS_QUALIFICATION_SIGNING_KEY": "test-signing-key",
        "PROOFOPS_DATABASE_URL": "postgresql://user:secret@db.example/proofops",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet",
        "PROOFOPS_SHEETS_SHEET_NAME": "Customers",
        "PROOFOPS_GMAIL_SENDER": "proofops-test@example.com",
        "PROOFOPS_CONTACTS_JSON": "{\"SMOKE-C001\":\"proofops-test@example.com\"}",
        "PROOFOPS_RENEWAL_THREADS_JSON": "{\"SMOKE-R1\":[]}",
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS": "proofops-test@example.com",
        "PROOFOPS_GEMINI_MODEL": "gemini-3.8-flash",
        "PROOFOPS_E2E_CUSTOMER_ID": "SMOKE-C001",
        "PROOFOPS_QUALIFICATION_RENEWAL_ID": "SMOKE-R1",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT_DIGEST,
        "PROOFOPS_DEPLOYMENT_REVISION": "railway-deployment-test",
    }
    for gate in LIVE_GATES:
        write_receipt(gate=gate, target={"gate": gate}, metrics=_valid_metrics(gate), env=env)
    return tmp_path


def test_receipt_contains_release_identity_but_not_raw_target(tmp_path):
    target = {"spreadsheet_id": "sensitive-sheet-id", "customer": "SMOKE-C001"}
    env = {
        "PROOFOPS_QUALIFICATION_RUN_ID": "run-x",
        "PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(tmp_path),
        "PROOFOPS_QUALIFICATION_SIGNING_KEY": "test-signing-key",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT_DIGEST,
        "PROOFOPS_DEPLOYMENT_REVISION": "railway-deployment-test",
    }
    path = write_receipt(gate=LIVE_GATES[0], target=target, metrics=_valid_metrics(LIVE_GATES[0]), env=env)
    payload = json.loads(path.read_text())
    assert payload["release_label"] == RELEASE_LABEL
    assert payload["package_version"] == __version__
    assert payload["alembic_head"] == ALEMBIC_HEAD
    assert payload["status"] == "PASS"
    assert "sensitive-sheet-id" not in path.read_text()
    assert len(payload["target_fingerprint"]) == 64


def test_receipt_collection_requires_shared_run_id(tmp_path):
    with pytest.raises(RuntimeError, match="QUALIFICATION_RUN_ID"):
        write_receipt(
            gate=LIVE_GATES[0], target={"x": 1}, env={"PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(tmp_path), "PROOFOPS_QUALIFICATION_SIGNING_KEY": "test-signing-key", "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT_DIGEST}
        )


def test_standalone_smoke_can_skip_receipt_collection():
    assert write_receipt(gate=LIVE_GATES[0], target={"x": 1}, env={}) is None


def test_complete_bundle_validates_and_builds_promotion_evidence(tmp_path):
    _emit_bundle(tmp_path, run_id="same-run")
    receipts = validate_receipt_bundle(tmp_path, expected_run_id="same-run", signing_key="test-signing-key")
    assert set(receipts) == set(LIVE_GATES)
    evidence = build_promotion_evidence(receipts)
    assert evidence["promotion_status"] == "READY_FOR_V1.0_TAG"
    assert evidence["qualification_run_id"] == "same-run"
    assert evidence["live_gates"] == {gate: "PASS" for gate in LIVE_GATES}
    assert set(evidence["receipt_sha256"]) == set(LIVE_GATES)


def test_mixed_qualification_runs_are_rejected(tmp_path):
    _emit_bundle(tmp_path, run_id="run-a")
    victim = tmp_path / f"{LIVE_GATES[-1]}.json"
    payload = json.loads(victim.read_text())
    payload["qualification_run_id"] = "run-b"
    victim.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="qualification run"):
        validate_receipt_bundle(tmp_path, expected_run_id="run-a", signing_key="test-signing-key")


def test_wrong_release_or_head_is_rejected(tmp_path):
    _emit_bundle(tmp_path)
    victim = tmp_path / f"{LIVE_GATES[1]}.json"
    payload = json.loads(victim.read_text())
    payload["alembic_head"] = "old"
    victim.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="alembic_head"):
        validate_receipt_bundle(tmp_path, signing_key="test-signing-key")


def test_missing_gate_receipt_is_rejected(tmp_path):
    _emit_bundle(tmp_path)
    (tmp_path / f"{LIVE_GATES[2]}.json").unlink()
    with pytest.raises(ValueError, match="LIVE_GATE_MISSING"):
        validate_receipt_bundle(tmp_path, signing_key="test-signing-key")


def test_non_pass_receipt_is_rejected(tmp_path):
    _emit_bundle(tmp_path)
    victim = tmp_path / f"{LIVE_GATES[3]}.json"
    payload = json.loads(victim.read_text())
    payload["status"] = "FAIL"
    victim.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="status"):
        validate_receipt_bundle(tmp_path, signing_key="test-signing-key")


def test_safe_database_identity_never_contains_password():
    ident = safe_database_identity("postgresql+psycopg://user:super-secret@db.example:5432/proofops")
    rendered = json.dumps(ident)
    assert "super-secret" not in rendered
    assert ident == {
        "scheme": "postgresql+psycopg",
        "host": "db.example",
        "port": 5432,
        "database": "proofops",
    }


def test_promotion_cli_refuses_without_explicit_live_execution():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "promote_v10.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("PROOFOPS_")},
    )
    assert result.returncode == 2
    assert "Refusing live promotion" in result.stderr


def test_promotion_cli_requires_synthetic_identity_before_running_providers(tmp_path):
    env = os.environ.copy()
    env.update({
        "PROOFOPS_GOOGLE_TOKEN_FILE": "/tmp/token.json",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet",
        "PROOFOPS_E2E_CUSTOMER_ID": "REAL-C001",
        "PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID": "SMOKE-C002",
        "PROOFOPS_QUALIFICATION_RENEWAL_ID": "R1",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT_DIGEST,
        "PROOFOPS_DEPLOYMENT_REVISION": "railway-deployment-test",
        "PROOFOPS_RELEASE_SIGNING_KEY_FILE": "/tmp/missing-key.pem",
        "PROOFOPS_RELEASE_PUBLIC_KEY_SHA256": "b" * 64,
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "promote_v10.py"), "--execute-live", "--output-dir", str(tmp_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 2
    assert "must use SMOKE- or DEMO- prefixes" in result.stderr
    assert not (tmp_path / "V1.0_PROMOTION_EVIDENCE.json").exists()

def test_tampered_signed_receipt_is_rejected(tmp_path):
    _emit_bundle(tmp_path, run_id="run-sign")
    victim = tmp_path / f"{LIVE_GATES[0]}.json"
    payload = json.loads(victim.read_text())
    payload["metrics"]["transaction_readback"] = False
    victim.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid signature"):
        validate_receipt_bundle(tmp_path, expected_run_id="run-sign", signing_key="test-signing-key")


def test_validly_signed_receipt_from_different_deployment_is_rejected(tmp_path):
    base = {
        "PROOFOPS_QUALIFICATION_RUN_ID": "run-deploy",
        "PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(tmp_path),
        "PROOFOPS_QUALIFICATION_SIGNING_KEY": "test-signing-key",
        "PROOFOPS_DATABASE_URL": "postgresql://user:secret@db-a.example/proofops",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet",
        "PROOFOPS_SHEETS_SHEET_NAME": "Customers",
        "PROOFOPS_GMAIL_SENDER": "proofops-test@example.com",
        "PROOFOPS_CONTACTS_JSON": '{"SMOKE-C001":"proofops-test@example.com"}',
        "PROOFOPS_RENEWAL_THREADS_JSON": '{"SMOKE-R1":[]}',
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS": "proofops-test@example.com",
        "PROOFOPS_E2E_CUSTOMER_ID": "SMOKE-C001",
        "PROOFOPS_QUALIFICATION_RENEWAL_ID": "SMOKE-R1",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT_DIGEST,
        "PROOFOPS_DEPLOYMENT_REVISION": "railway-deployment-test",
    }
    for gate in LIVE_GATES:
        env = dict(base)
        if gate == LIVE_GATES[-1]:
            env["PROOFOPS_DATABASE_URL"] = "postgresql://user:secret@db-b.example/proofops"
        write_receipt(gate=gate, target={"gate": gate}, metrics=_valid_metrics(gate), env=env)
    with pytest.raises(ValueError, match="deployment configuration"):
        validate_receipt_bundle(tmp_path, expected_run_id="run-deploy", signing_key="test-signing-key")


def test_receipt_from_modified_source_tree_is_rejected(tmp_path):
    _emit_bundle(tmp_path, run_id="run-source")
    victim = tmp_path / f"{LIVE_GATES[0]}.json"
    payload = json.loads(victim.read_text())
    payload["source_fingerprint"] = "0" * 64
    # Even before signature validation, source binding must reject the receipt.
    victim.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="different release source tree"):
        validate_receipt_bundle(tmp_path, expected_run_id="run-source", signing_key="test-signing-key")
