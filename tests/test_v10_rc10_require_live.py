from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.release import qualification
from app.release.qualification import LIVE_GATES, validate_receipt_bundle, validate_required_live_qualification, write_receipt
from scripts import qualify_v10


ARTIFACT = "a" * 64


def metrics(gate: str) -> dict:
    return {
        "postgresql": {"transaction_readback": True, "business_operation_single_owner": True, "approval_single_claimant": True, "effect_single_active_attempt": True, "idempotency_single_creator": True},
        "sheets": {"stale_version_rejected": True, "verified_readback": True, "version_delta": 2},
        "gmail": {"verified_sent_evidence_rows": 1, "physical_messages_for_logical_effect": 1, "response_loss_injected": True, "unknown_observed": True, "reconciled_without_resend": True, "provider_message_id_bound": True, "authoritative_thread_intent": "ACCEPTED", "sender_verified": True},
        "gemini": {"scenario_count": 25, "trials": 2, "critical_scenario_count": 10, "deterministic_containment_passed": 50, "deterministic_containment_total": 50, "critical_semantic_passed": 20, "critical_semantic_total": 20, "semantic_passed": 48, "semantic_total": 50, "semantic_rate": 0.96},
        "deployed_e2e": {"completed_verified": True, "verified_gmail_evidence_rows": 1, "physical_messages_for_logical_effect": 1, "cold_restart": True, "actual_deployment_restart": True, "deployment_revision_changed": True, "runtime_payload_attested": True, "deployed_api_initial_effect": True, "deployed_source_attested": True, "deployed_environment_attested": True, "deployed_configuration_attested": True, "response_loss_injected": True, "unknown_observed": True, "reconciled_without_resend": True, "accepted_customer_zero_send": True, "repeat_goal_state": "DEFERRED"},
    }[gate]


def env_for(tmp_path: Path) -> dict[str, str]:
    return {
        "PROOFOPS_QUALIFICATION_RUN_ID": "rc10-test-run",
        "PROOFOPS_QUALIFICATION_RECEIPT_DIR": str(tmp_path),
        "PROOFOPS_QUALIFICATION_SIGNING_KEY": "test-signing-key",
        "PROOFOPS_DATABASE_URL": "postgresql://user:secret@db.example/proofops",
        "PROOFOPS_SHEETS_SPREADSHEET_ID": "sheet", "PROOFOPS_SHEETS_SHEET_NAME": "Customers",
        "PROOFOPS_GMAIL_SENDER": "proofops-test@example.com",
        "PROOFOPS_CONTACTS_JSON": '{"SMOKE-C001":"proofops-test@example.com"}',
        "PROOFOPS_RENEWAL_THREADS_JSON": '{"SMOKE-R1":[]}',
        "PROOFOPS_ALLOWED_TEST_RECIPIENTS": "proofops-test@example.com",
        "PROOFOPS_GEMINI_MODEL": "gemini-3.8-flash",
        "PROOFOPS_E2E_CUSTOMER_ID": "SMOKE-C001", "PROOFOPS_QUALIFICATION_RENEWAL_ID": "SMOKE-R1",
        "PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST": ARTIFACT,
        "PROOFOPS_DEPLOYMENT_REVISION": "railway-deployment-rc10-test",
    }


def bundle(tmp_path: Path) -> dict[str, str]:
    env = env_for(tmp_path)
    for gate in LIVE_GATES:
        write_receipt(gate=gate, target={"gate": gate}, metrics=metrics(gate), env=env)
    return env


def test_zero_receipts_and_config_only_are_hard_failures(tmp_path):
    env = env_for(tmp_path)
    with pytest.raises(ValueError, match="LIVE_GATE_MISSING: postgresql"):
        validate_required_live_qualification(env)


def test_cli_require_live_rejects_complete_configuration_without_evidence(tmp_path, monkeypatch, capsys):
    token = tmp_path / "google-token.json"
    token.write_text("{}", encoding="utf-8")
    env = env_for(tmp_path / "receipts")
    env.update({
        "PROOFOPS_RUNTIME": "live", "PROOFOPS_GOOGLE_TOKEN_FILE": str(token),
        "PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS": "YES", "PROOFOPS_SYNTHETIC_DATASET": "YES",
        "PROOFOPS_QUALIFICATION_MODE": "YES", "PROOFOPS_QUALIFICATION_FAULT_MODE": "gmail_commit_then_lose_first_response",
        "GEMINI_API_KEY": "configured-test-key", "PROOFOPS_DEPLOY_RESTART_COMMAND": "safe-redeploy",
        "PROOFOPS_DEPLOY_RESTART_COMMAND_ID": "test-redeploy", "PROOFOPS_DEPLOYED_BASE_URL": "https://example.invalid",
        "PROOFOPS_UI_PASSWORD": "configured-test-password",
    })
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["qualify_v10.py", "--require-live"])
    assert qualify_v10.main() == 1
    assert "LIVE_GATE_MISSING: postgresql" in capsys.readouterr().out


@pytest.mark.parametrize("missing", LIVE_GATES)
def test_each_missing_gate_hard_fails(tmp_path, missing):
    env = bundle(tmp_path)
    (tmp_path / f"{missing}.json").unlink()
    with pytest.raises(ValueError, match=f"LIVE_GATE_MISSING: {missing}"):
        validate_required_live_qualification(env)


def test_blocked_gemini_is_not_evidence(tmp_path):
    env = bundle(tmp_path)
    path = tmp_path / "gemini.json"
    receipt = json.loads(path.read_text())
    receipt["status"] = "BLOCKED_EXTERNAL_QUOTA"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid status"):
        validate_required_live_qualification(env)


def test_rc11_current_qualification_session_rejects_prior_valid_bundle(tmp_path):
    env = bundle(tmp_path)
    stale_session = dict(env, PROOFOPS_QUALIFICATION_RUN_ID="new-authoritative-session")
    with pytest.raises(ValueError, match="qualification run"):
        validate_required_live_qualification(stale_session)


def test_rc11_gemini_undercoverage_is_rejected_even_when_ratios_are_perfect(tmp_path):
    env = bundle(tmp_path)
    path = tmp_path / "gemini.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["metrics"].update(
        deterministic_containment_passed=1, deterministic_containment_total=1,
        critical_semantic_passed=1, critical_semantic_total=1,
        semantic_passed=1, semantic_total=1, semantic_rate=1.0,
    )
    unsigned = dict(receipt)
    unsigned.pop("signature_hmac_sha256", None)
    receipt["signature_hmac_sha256"] = qualification._receipt_signature(
        unsigned, env["PROOFOPS_QUALIFICATION_SIGNING_KEY"]
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen corpus"):
        validate_required_live_qualification(env)


def test_rc11_duplicate_json_keys_fail_before_receipt_signature_interpretation(tmp_path):
    env = bundle(tmp_path)
    path = tmp_path / "gmail.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"status": "PASS"', '"status": "FAIL", "status": "PASS"', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        validate_required_live_qualification(env)


@pytest.mark.parametrize("field", ["source_fingerprint", "runtime_payload_fingerprint", "deployment_artifact_digest"])
def test_identity_mismatch_is_hard_failure(tmp_path, field):
    env = bundle(tmp_path)
    path = tmp_path / "gmail.json"
    receipt = json.loads(path.read_text())
    receipt[field] = "b" * 64
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_required_live_qualification(env)


def test_rc9_receipt_cannot_qualify_rc10(tmp_path):
    env = bundle(tmp_path)
    path = tmp_path / "gmail.json"
    receipt = json.loads(path.read_text())
    receipt["package_version"] = "1.0.0rc9"
    receipt["release_label"] = "V1.0.0-rc9"
    unsigned = dict(receipt)
    unsigned.pop("signature_hmac_sha256", None)
    receipt["signature_hmac_sha256"] = qualification._receipt_signature(unsigned, env["PROOFOPS_QUALIFICATION_SIGNING_KEY"])
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release_label"):
        validate_required_live_qualification(env)


def test_deployment_revision_mismatch_is_hard_failure(tmp_path):
    env = bundle(tmp_path)
    stale_env = dict(env, PROOFOPS_DEPLOYMENT_REVISION="railway-deployment-other")
    with pytest.raises(ValueError, match="deployment revision"):
        validate_required_live_qualification(stale_env)


def test_tamper_and_unknown_duplicate_file_hard_fail(tmp_path):
    env = bundle(tmp_path)
    path = tmp_path / "gmail.json"
    receipt = json.loads(path.read_text())
    receipt["metrics"]["physical_messages_for_logical_effect"] = 2
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid signature"):
        validate_required_live_qualification(env)
    bundle(tmp_path)
    (tmp_path / "gmail.conflict.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="DUPLICATE_OR_UNKNOWN"):
        validate_required_live_qualification(env)


def test_all_five_valid_pass_without_promotion_evidence(tmp_path):
    env = bundle(tmp_path)
    receipts = validate_required_live_qualification(env)
    assert set(receipts) == set(LIVE_GATES)
    assert not (tmp_path / "V1.0_PROMOTION_EVIDENCE.json").exists()
    assert validate_receipt_bundle(tmp_path, signing_key="test-signing-key", env=env)
