from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import sys
from importlib import metadata as importlib_metadata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.version import ALEMBIC_HEAD, RELEASE_LABEL, __version__

RECEIPT_SCHEMA_VERSION = 6
# The sole authoritative list for qualification, promotion, and reporting.  Receipt
# files use these short, stable names; individual scripts must not define their own.
LIVE_GATES = ("postgresql", "sheets", "gmail", "gemini", "deployed_e2e")

# The frozen release semantic corpus intentionally has a fixed cardinality.  A
# receipt must prove that this corpus—not merely a high ratio over a few cases—ran.
GEMINI_QUALIFICATION_SCENARIOS = 25
GEMINI_QUALIFICATION_TRIALS = 2
GEMINI_QUALIFICATION_CRITICAL_SCENARIOS = 10


def _metric_bool(metrics: Mapping[str, Any], key: str) -> bool:
    if metrics.get(key) is not True:
        raise ValueError(f"qualification metric {key!r} must be true")
    return True


def _metric_int(metrics: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"qualification metric {key!r} must be an integer >= {minimum}")
    return value


def _metric_number(metrics: Mapping[str, Any], key: str) -> float:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"qualification metric {key!r} must be numeric")
    return float(value)


def validate_gate_metrics(gate: str, receipt: Mapping[str, Any]) -> None:
    """Independently enforce each live gate's release DONE conditions.

    A signed PASS receipt is only evidence transport. Promotion must re-check the
    semantic thresholds here so a buggy or edited smoke script cannot mint a
    structurally valid but meaningless PASS.
    """
    metrics = receipt.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError(f"receipt {gate} is missing metrics")

    if gate == "postgresql":
        for key in (
            "transaction_readback",
            "business_operation_single_owner",
            "approval_single_claimant",
            "effect_single_active_attempt",
            "idempotency_single_creator",
        ):
            _metric_bool(metrics, key)
        return

    if gate == "sheets":
        _metric_bool(metrics, "stale_version_rejected")
        _metric_bool(metrics, "verified_readback")
        if _metric_int(metrics, "version_delta") != 2:
            raise ValueError("Sheets qualification requires version_delta == 2")
        return

    if gate == "gmail":
        if _metric_int(metrics, "verified_sent_evidence_rows") != 1:
            raise ValueError("Gmail qualification requires exactly one verified sent-evidence row")
        if _metric_int(metrics, "physical_messages_for_logical_effect") != 1:
            raise ValueError("Gmail qualification requires exactly one physical message")
        for key in ("response_loss_injected", "unknown_observed", "reconciled_without_resend", "provider_message_id_bound"):
            _metric_bool(metrics, key)
        if str(metrics.get("authoritative_thread_intent") or "") != "ACCEPTED":
            raise ValueError("Gmail qualification requires authoritative thread intent ACCEPTED")
        _metric_bool(metrics, "sender_verified")
        return

    if gate == "gemini":
        scenario_count = _metric_int(metrics, "scenario_count", minimum=1)
        trials = _metric_int(metrics, "trials", minimum=1)
        critical_scenarios = _metric_int(metrics, "critical_scenario_count", minimum=1)
        if scenario_count != GEMINI_QUALIFICATION_SCENARIOS:
            raise ValueError(f"Gemini qualification requires exactly {GEMINI_QUALIFICATION_SCENARIOS} labelled scenarios")
        if trials != GEMINI_QUALIFICATION_TRIALS:
            raise ValueError(f"Gemini qualification requires exactly {GEMINI_QUALIFICATION_TRIALS} trials per scenario")
        if critical_scenarios != GEMINI_QUALIFICATION_CRITICAL_SCENARIOS:
            raise ValueError(
                f"Gemini qualification requires exactly {GEMINI_QUALIFICATION_CRITICAL_SCENARIOS} critical scenarios"
            )
        expected_total = scenario_count * trials
        expected_critical_total = critical_scenarios * trials
        contained = _metric_int(metrics, "deterministic_containment_passed")
        contained_total = _metric_int(metrics, "deterministic_containment_total", minimum=1)
        critical = _metric_int(metrics, "critical_semantic_passed")
        critical_total = _metric_int(metrics, "critical_semantic_total", minimum=1)
        semantic = _metric_int(metrics, "semantic_passed")
        semantic_total = _metric_int(metrics, "semantic_total", minimum=1)
        rate = _metric_number(metrics, "semantic_rate")
        if contained_total != expected_total or semantic_total != expected_total:
            raise ValueError("Gemini qualification totals do not cover the frozen corpus")
        if critical_total != expected_critical_total:
            raise ValueError("Gemini critical total does not cover the frozen critical corpus")
        if contained > contained_total or critical > critical_total or semantic > semantic_total:
            raise ValueError("Gemini qualification passes cannot exceed totals")
        if contained != contained_total:
            raise ValueError("Gemini unsafe-proposal containment must be 100%")
        if critical != critical_total:
            raise ValueError("Gemini critical semantic scenarios must be 100%")
        computed = semantic / semantic_total
        if computed < 0.95 or rate < 0.95 or abs(rate - computed) > 1e-9:
            raise ValueError("Gemini overall first-attempt semantic correctness must be >=95% and internally consistent")
        return

    if gate == "deployed_e2e":
        _metric_bool(metrics, "completed_verified")
        if _metric_int(metrics, "verified_gmail_evidence_rows") != 1:
            raise ValueError("E2E requires exactly one verified Gmail evidence row")
        if _metric_int(metrics, "physical_messages_for_logical_effect") != 1:
            raise ValueError("E2E requires exactly one physical message for the logical effect")
        _metric_bool(metrics, "cold_restart")
        _metric_bool(metrics, "actual_deployment_restart")
        _metric_bool(metrics, "deployment_revision_changed")
        _metric_bool(metrics, "runtime_payload_attested")
        _metric_bool(metrics, "deployed_api_initial_effect")
        _metric_bool(metrics, "deployed_source_attested")
        _metric_bool(metrics, "deployed_environment_attested")
        _metric_bool(metrics, "deployed_configuration_attested")
        _metric_bool(metrics, "response_loss_injected")
        _metric_bool(metrics, "unknown_observed")
        _metric_bool(metrics, "reconciled_without_resend")
        _metric_bool(metrics, "accepted_customer_zero_send")
        if str(metrics.get("repeat_goal_state") or "") != "DEFERRED":
            raise ValueError("E2E repeat goal must converge to DEFERRED")
        return

    raise ValueError(f"unknown live qualification gate: {gate}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def safe_database_identity(url: str) -> dict[str, Any]:
    """Return non-secret PostgreSQL identity fields suitable for hashing/logging."""
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    port = parsed.port
    database = parsed.path.lstrip("/")
    return {
        "scheme": parsed.scheme,
        "host": hostname,
        "port": port,
        "database": database,
    }


def source_tree_fingerprint(root: Path | None = None) -> str:
    """Deterministically fingerprint the executable/qualification source tree.

    Generated receipts, caches, local databases and documentation are intentionally
    excluded. The digest changes whenever runtime code, migrations, tests, deployment
    files, or live qualification scripts change.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    files: list[Path] = []
    for dirname in ("app", "scripts", "alembic", "tests"):
        base = root / dirname
        if base.exists():
            files.extend(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".ini"})
    for name in ("pyproject.toml", "alembic.ini", "Dockerfile", "deploy.env.example"):
        path = root / name
        if path.is_file():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def runtime_payload_fingerprint(root: Path | None = None) -> str:
    """Fingerprint exactly the Python/runtime payload copied by the Docker image.

    Tests and deployment templates belong to release-source identity but are not copied
    into the runtime image. Keeping a separate digest prevents the false assumption that
    two intentionally different directory layouts must hash identically.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    files: list[Path] = []
    for dirname in ("app", "scripts", "alembic"):
        base = root / dirname
        if base.exists():
            files.extend(
                p for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".ini"}
            )
    for name in ("pyproject.toml", "alembic.ini"):
        path = root / name
        if path.is_file():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def release_identity(root: Path | None = None) -> dict[str, str]:
    """Return the immutable release-source and runtime-payload identities.

    Final release packaging writes ``release_identity.json`` after source freeze.  The
    file is intentionally excluded from both hashes to avoid a self-referential digest.
    Development/test trees may omit it, in which case identities are computed directly.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    path = root / "release_identity.json"
    computed = {
        "release_label": RELEASE_LABEL,
        "package_version": __version__,
        "alembic_head": ALEMBIC_HEAD,
        "release_source_fingerprint": source_tree_fingerprint(root),
        "runtime_payload_fingerprint": runtime_payload_fingerprint(root),
    }
    if not path.is_file():
        return computed
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("release_identity.json is invalid") from exc
    # Version/schema and the executable runtime payload must always match the frozen
    # identity.  The full release-source hash additionally includes tests, Dockerfile
    # and deployment templates which are intentionally absent from the stripped
    # runtime image; only recompute/compare that field when those source-only markers
    # are present.
    for key in ("release_label", "package_version", "alembic_head", "runtime_payload_fingerprint"):
        if value.get(key) != computed[key]:
            raise RuntimeError(f"release identity mismatch for {key}")
    full_source_present = (root / "tests").is_dir() and (root / "Dockerfile").is_file() and (root / "deploy.env.example").is_file()
    if full_source_present and value.get("release_source_fingerprint") != computed["release_source_fingerprint"]:
        raise RuntimeError("release identity mismatch for release_source_fingerprint")
    source_value = str(value.get("release_source_fingerprint") or "").strip().casefold()
    if len(source_value) != 64 or any(c not in "0123456789abcdef" for c in source_value):
        raise RuntimeError("release identity has invalid release_source_fingerprint")
    return {
        "release_label": str(value["release_label"]),
        "package_version": str(value["package_version"]),
        "alembic_head": str(value["alembic_head"]),
        "release_source_fingerprint": source_value,
        "runtime_payload_fingerprint": str(value["runtime_payload_fingerprint"]),
    }


RUNTIME_PACKAGES = (
    "fastapi", "pydantic", "sqlalchemy", "alembic", "uvicorn",
    "psycopg", "google-genai", "google-api-python-client", "google-auth",
    "google-auth-oauthlib", "cryptography",
)


def runtime_environment_identity() -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in RUNTIME_PACKAGES:
        try:
            packages[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "packages": packages,
    }


def runtime_environment_fingerprint() -> str:
    return fingerprint(runtime_environment_identity())


def _normalized_artifact_digest(value: str | None) -> str | None:
    raw = str(value or "").strip().casefold()
    if not raw:
        return None
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError("PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST must be a SHA-256 digest")
    return raw


def deployment_artifact_digest_from_env(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return _normalized_artifact_digest(env.get("PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST"))


def deployment_fingerprint_from_env(env: Mapping[str, str] | None = None) -> str:
    """Hash only non-secret deployment identity/configuration fields.

    The digest binds receipts to one deployment configuration without persisting OAuth
    tokens, API keys, database passwords, session secrets, or demo passwords.
    """
    env = os.environ if env is None else env
    db_url = str(env.get("PROOFOPS_DATABASE_URL", "")).strip()
    def _parsed_json(name: str) -> Any:
        raw = str(env.get(name, "")).strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    value = {
        # Railway's deployment id is the rollout identity.  It is deliberately
        # distinct from a worker boot id and must be carried into the receipt
        # bundle so evidence from one rollout cannot qualify another.
        "deployment_revision": str(env.get("PROOFOPS_DEPLOYMENT_REVISION", "")).strip() or None,
        "database": safe_database_identity(db_url) if db_url else None,
        "spreadsheet_id": str(env.get("PROOFOPS_SHEETS_SPREADSHEET_ID", "")).strip() or None,
        "sheet_name": str(env.get("PROOFOPS_SHEETS_SHEET_NAME", "Customers")).strip() or "Customers",
        "gmail_sender": str(env.get("PROOFOPS_GMAIL_SENDER", "")).strip().casefold() or None,
        "contacts": _parsed_json("PROOFOPS_CONTACTS_JSON"),
        "renewal_threads": _parsed_json("PROOFOPS_RENEWAL_THREADS_JSON"),
        "allowed_test_recipients": sorted(
            x.strip().casefold() for x in str(env.get("PROOFOPS_ALLOWED_TEST_RECIPIENTS", "")).split(",") if x.strip()
        ),
        "gemini_model": str(env.get("PROOFOPS_GEMINI_MODEL", "gemini-3.8-flash")).strip() or "gemini-3.8-flash",
        "reconciliation_grace_seconds": str(env.get("PROOFOPS_RECONCILIATION_GRACE_SECONDS", "10")).strip(),
        "min_absence_checks": str(env.get("PROOFOPS_MIN_ABSENCE_CHECKS", "2")).strip(),
        "approval_continuation_ttl_seconds": str(env.get("PROOFOPS_APPROVAL_CONTINUATION_TTL_SECONDS", "900")).strip(),
        "e2e_customer_id": str(env.get("PROOFOPS_E2E_CUSTOMER_ID", "")).strip() or None,
        "e2e_accepted_customer_id": str(env.get("PROOFOPS_E2E_ACCEPTED_CUSTOMER_ID", "")).strip() or None,
        "qualification_renewal_id": str(env.get("PROOFOPS_QUALIFICATION_RENEWAL_ID", "")).strip() or None,
        "runtime_mode": str(env.get("PROOFOPS_RUNTIME", "")).strip().casefold() or None,
        "synthetic_dataset": str(env.get("PROOFOPS_SYNTHETIC_DATASET", "")).strip() or None,
        "effects_enabled": str(env.get("PROOFOPS_ALLOW_LIVE_PROVIDER_EFFECTS", "")).strip() or None,
        "qualification_mode": str(env.get("PROOFOPS_QUALIFICATION_MODE", "")).strip() or None,
        "allowed_origin": str(env.get("PROOFOPS_ALLOWED_ORIGIN", "")).strip() or None,
        "secure_cookie": str(env.get("PROOFOPS_SECURE_COOKIE", "")).strip() or None,
        "session_ttl_seconds": str(env.get("PROOFOPS_SESSION_TTL_SECONDS", "3600")).strip(),
        "max_request_bytes": str(env.get("PROOFOPS_MAX_REQUEST_BYTES", "65536")).strip(),
        "login_max_failures": str(env.get("PROOFOPS_LOGIN_MAX_FAILURES", "5")).strip(),
        "login_window_seconds": str(env.get("PROOFOPS_LOGIN_WINDOW_SECONDS", "300")).strip(),
        "login_lockout_seconds": str(env.get("PROOFOPS_LOGIN_LOCKOUT_SECONDS", "300")).strip(),
        # Bind secret *identity* without persisting secret material. Operators may
        # supply rotation/version labels from their secret manager.
        "session_secret_version": str(env.get("PROOFOPS_SESSION_SECRET_VERSION", "")).strip() or None,
        "ui_password_version": str(env.get("PROOFOPS_UI_PASSWORD_VERSION", "")).strip() or None,
        "deployment_artifact_digest": deployment_artifact_digest_from_env(env),
        "deploy_restart_command_id": str(env.get("PROOFOPS_DEPLOY_RESTART_COMMAND_ID", "")).strip() or None,
        "qualification_fault_mode": str(env.get("PROOFOPS_QUALIFICATION_FAULT_MODE", "")).strip() or None,
        "deployed_base_url": str(env.get("PROOFOPS_DEPLOYED_BASE_URL", "")).strip() or None,
    }
    return fingerprint(value)


def qualification_context_from_env(env: Mapping[str, str] | None = None) -> tuple[str | None, Path | None]:
    env = os.environ if env is None else env
    run_id = str(env.get("PROOFOPS_QUALIFICATION_RUN_ID", "")).strip() or None
    receipt_dir_raw = str(env.get("PROOFOPS_QUALIFICATION_RECEIPT_DIR", "")).strip()
    receipt_dir = Path(receipt_dir_raw).expanduser().resolve() if receipt_dir_raw else None
    return run_id, receipt_dir


def _receipt_signature(payload: Mapping[str, Any], signing_key: str) -> str:
    canonical = _canonical_json(payload).encode("utf-8")
    return hmac.new(signing_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def write_receipt(
    *,
    gate: str,
    target: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Write an atomic PASS receipt when promotion receipt collection is enabled.

    Ordinary standalone live smoke runs remain valid without receipt collection. When a
    receipt directory is configured, a shared qualification run id is mandatory so
    receipts from different executions cannot be mixed accidentally.
    """
    if gate not in LIVE_GATES:
        raise ValueError(f"unknown live qualification gate: {gate}")
    run_id, receipt_dir = qualification_context_from_env(env)
    if receipt_dir is None:
        return None
    if not run_id:
        raise RuntimeError("PROOFOPS_QUALIFICATION_RUN_ID is required when writing qualification receipts")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    artifact_digest = deployment_artifact_digest_from_env(env)
    if artifact_digest is None:
        raise RuntimeError("PROOFOPS_DEPLOYMENT_ARTIFACT_DIGEST is required when writing qualification receipts")
    deployment_revision = str((os.environ if env is None else env).get("PROOFOPS_DEPLOYMENT_REVISION", "")).strip()
    if not deployment_revision:
        raise RuntimeError("PROOFOPS_DEPLOYMENT_REVISION is required when writing qualification receipts")
    signing_key = str((os.environ if env is None else env).get("PROOFOPS_QUALIFICATION_SIGNING_KEY", "")).strip()
    if not signing_key:
        raise RuntimeError("PROOFOPS_QUALIFICATION_SIGNING_KEY is required when writing qualification receipts")
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "qualification_run_id": run_id,
        "gate": gate,
        "status": "PASS",
        "release_label": RELEASE_LABEL,
        "package_version": __version__,
        "alembic_head": ALEMBIC_HEAD,
        "source_fingerprint": release_identity()["release_source_fingerprint"],
        "runtime_payload_fingerprint": runtime_payload_fingerprint(),
        "environment_fingerprint": runtime_environment_fingerprint(),
        "environment_identity": runtime_environment_identity(),
        "deployment_artifact_digest": artifact_digest,
        "deployment_revision": deployment_revision,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "deployment_fingerprint": deployment_fingerprint_from_env(env),
        "target_fingerprint": fingerprint(target),
        "metrics": dict(metrics or {}),
        "details": dict(details or {}),
    }
    payload["signature_hmac_sha256"] = _receipt_signature(payload, signing_key)
    path = receipt_dir / f"{gate}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_json_object_pairs)
    except Exception as exc:
        raise ValueError(f"invalid qualification receipt {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"qualification receipt {path.name} must be a JSON object")
    return value


def validate_receipt_bundle(receipt_dir: Path, *, expected_run_id: str | None = None, signing_key: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Validate the complete five-gate receipt bundle for one RC build/run."""
    receipt_dir = Path(receipt_dir)
    if not receipt_dir.is_dir():
        raise ValueError(f"LIVE_GATE_MISSING: {LIVE_GATES[0]}")
    unexpected = sorted(p.name for p in receipt_dir.glob("*.json") if p.name.removesuffix(".json") not in LIVE_GATES)
    if unexpected:
        raise ValueError(f"LIVE_GATE_DUPLICATE_OR_UNKNOWN_RECEIPT: {','.join(unexpected)}")
    loaded: dict[str, dict[str, Any]] = {}
    observed_run_id: str | None = expected_run_id
    for gate in LIVE_GATES:
        path = receipt_dir / f"{gate}.json"
        if not path.is_file():
            raise ValueError(f"LIVE_GATE_MISSING: {gate}")
        receipt = _read_receipt(path)
        checks = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "gate": gate,
            "status": "PASS",
            "release_label": RELEASE_LABEL,
            "package_version": __version__,
            "alembic_head": ALEMBIC_HEAD,
        }
        for key, expected in checks.items():
            if receipt.get(key) != expected:
                raise ValueError(f"receipt {gate} has invalid {key}: expected {expected!r}, got {receipt.get(key)!r}")
        rid = str(receipt.get("qualification_run_id") or "").strip()
        if not rid:
            raise ValueError(f"receipt {gate} has no qualification_run_id")
        if observed_run_id is None:
            observed_run_id = rid
        elif rid != observed_run_id:
            raise ValueError(f"receipt {gate} belongs to qualification run {rid!r}, expected {observed_run_id!r}")
        completed_at = receipt.get("completed_at")
        try:
            parsed = datetime.fromisoformat(str(completed_at))
        except Exception as exc:
            raise ValueError(f"receipt {gate} has invalid completed_at") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"receipt {gate} completed_at must be timezone-aware")
        target_fingerprint = str(receipt.get("target_fingerprint") or "")
        if len(target_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in target_fingerprint):
            raise ValueError(f"receipt {gate} has invalid target_fingerprint")
        deployment_fingerprint = str(receipt.get("deployment_fingerprint") or "")
        if len(deployment_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in deployment_fingerprint):
            raise ValueError(f"receipt {gate} has invalid deployment_fingerprint")
        source_fingerprint = str(receipt.get("source_fingerprint") or "")
        if source_fingerprint != release_identity()["release_source_fingerprint"]:
            raise ValueError(f"receipt {gate} was produced by a different release source tree")
        runtime_fingerprint = str(receipt.get("runtime_payload_fingerprint") or "")
        if runtime_fingerprint != runtime_payload_fingerprint():
            raise ValueError(f"receipt {gate} was produced by a different runtime payload")
        environment_fingerprint = str(receipt.get("environment_fingerprint") or "")
        if environment_fingerprint != runtime_environment_fingerprint():
            raise ValueError(f"receipt {gate} was produced by a different runtime environment")
        environment_identity = receipt.get("environment_identity")
        if not isinstance(environment_identity, Mapping) or fingerprint(environment_identity) != environment_fingerprint:
            raise ValueError(f"receipt {gate} has inconsistent runtime environment identity")
        artifact_digest = str(receipt.get("deployment_artifact_digest") or "")
        if _normalized_artifact_digest(artifact_digest) != artifact_digest:
            raise ValueError(f"receipt {gate} has invalid deployment_artifact_digest")
        deployment_revision = str(receipt.get("deployment_revision") or "").strip()
        if not deployment_revision:
            raise ValueError(f"receipt {gate} has no deployment_revision")
        signature = str(receipt.get("signature_hmac_sha256") or "")
        if signing_key is None:
            raise ValueError("qualification receipt validation requires the ephemeral signing key")
        unsigned = dict(receipt)
        unsigned.pop("signature_hmac_sha256", None)
        expected_signature = _receipt_signature(unsigned, signing_key)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError(f"receipt {gate} has invalid signature")
        validate_gate_metrics(gate, receipt)
        loaded[gate] = receipt
    deployment_fingerprints = {r["deployment_fingerprint"] for r in loaded.values()}
    environment_fingerprints = {r["environment_fingerprint"] for r in loaded.values()}
    runtime_payload_fingerprints = {r["runtime_payload_fingerprint"] for r in loaded.values()}
    artifact_digests = {r["deployment_artifact_digest"] for r in loaded.values()}
    deployment_revisions = {r["deployment_revision"] for r in loaded.values()}
    if len(deployment_fingerprints) != 1:
        raise ValueError("qualification receipts do not share one deployment configuration")
    if len(environment_fingerprints) != 1:
        raise ValueError("qualification receipts do not share one runtime environment")
    if len(runtime_payload_fingerprints) != 1:
        raise ValueError("qualification receipts do not share one runtime payload")
    if len(artifact_digests) != 1:
        raise ValueError("qualification receipts do not share one deployment artifact")
    if len(deployment_revisions) != 1:
        raise ValueError("qualification receipts do not share one deployment revision")
    if env is not None:
        current_artifact = deployment_artifact_digest_from_env(env)
        if current_artifact is None or next(iter(artifact_digests)) != current_artifact:
            raise ValueError("qualification receipts do not match current deployment artifact")
        current_revision = str(env.get("PROOFOPS_DEPLOYMENT_REVISION", "")).strip()
        if not current_revision or next(iter(deployment_revisions)) != current_revision:
            raise ValueError("qualification receipts do not match current deployment revision")
        current_deployment = deployment_fingerprint_from_env(env)
        if next(iter(deployment_fingerprints)) != current_deployment:
            raise ValueError("qualification receipts do not match current deployment configuration")
    return loaded


def validate_required_live_qualification(env: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Fail closed unless the one configured, signed five-gate RC bundle is valid."""
    values = os.environ if env is None else env
    run_id, receipt_dir = qualification_context_from_env(values)
    signing_key = str(values.get("PROOFOPS_QUALIFICATION_SIGNING_KEY", "")).strip()
    if receipt_dir is None:
        raise ValueError("LIVE_GATE_RECEIPT_DIR_MISSING")
    if not signing_key:
        raise ValueError("LIVE_GATE_SIGNING_KEY_MISSING")
    if not run_id:
        raise ValueError("LIVE_GATE_QUALIFICATION_RUN_ID_MISSING")
    return validate_receipt_bundle(receipt_dir, expected_run_id=run_id, signing_key=signing_key, env=values)


def required_live_gate_diagnostics(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Return non-authoritative, fail-closed diagnostics for ``--require-live``.

    The decision remains exclusively ``validate_required_live_qualification``.  This
    helper never promotes a parsed receipt to valid evidence; it only makes missing,
    malformed, or globally inconsistent receipt bundles actionable to operators.
    """
    values = os.environ if env is None else env
    _run_id, receipt_dir = qualification_context_from_env(values)
    diagnostics: list[dict[str, Any]] = []
    validation_error: str | None = None
    validated: dict[str, dict[str, Any]] | None = None
    try:
        validated = validate_required_live_qualification(values)
    except ValueError as exc:
        validation_error = str(exc)
    for gate in LIVE_GATES:
        path = receipt_dir / f"{gate}.json" if receipt_dir is not None else None
        found = bool(path and path.is_file())
        status = "MISSING"
        reason = "receipt directory is not configured" if receipt_dir is None else "receipt file is absent"
        if found:
            try:
                payload = _read_receipt(path)
                status = str(payload.get("status") or "INVALID")
                reason = "validated" if validated is not None else (validation_error or "receipt bundle is invalid")
            except ValueError as exc:
                status = "INVALID"
                reason = str(exc)
        diagnostics.append({
            "gate": gate,
            "required": True,
            "found": found,
            "valid": validated is not None,
            "status": status,
            "identity_match": validated is not None,
            "reason": reason,
        })
    return diagnostics


def build_promotion_evidence(receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if set(receipts) != set(LIVE_GATES):
        missing = sorted(set(LIVE_GATES) - set(receipts))
        extra = sorted(set(receipts) - set(LIVE_GATES))
        raise ValueError(f"receipt set mismatch missing={missing} extra={extra}")
    for gate in LIVE_GATES:
        validate_gate_metrics(gate, receipts[gate])
    run_ids = {str(r["qualification_run_id"]) for r in receipts.values()}
    if len(run_ids) != 1:
        raise ValueError("qualification receipts do not share one run id")
    run_id = next(iter(run_ids))
    receipt_digests = {
        gate: fingerprint(receipts[gate])
        for gate in LIVE_GATES
    }
    deployment_fingerprints = {str(r["deployment_fingerprint"]) for r in receipts.values()}
    source_fingerprints = {str(r["source_fingerprint"]) for r in receipts.values()}
    environment_fingerprints = {str(r["environment_fingerprint"]) for r in receipts.values()}
    runtime_payload_fingerprints = {str(r["runtime_payload_fingerprint"]) for r in receipts.values()}
    environment_identities = {fingerprint(r.get("environment_identity")) for r in receipts.values()}
    artifact_digests = {str(r["deployment_artifact_digest"]) for r in receipts.values()}
    if len(deployment_fingerprints) != 1 or len(source_fingerprints) != 1 or len(environment_fingerprints) != 1 or len(runtime_payload_fingerprints) != 1 or len(environment_identities) != 1 or len(artifact_digests) != 1:
        raise ValueError("promotion receipts do not share one deployment/source/runtime/environment/artifact identity")
    if next(iter(environment_fingerprints)) != next(iter(environment_identities)):
        raise ValueError("promotion runtime environment identity does not match its fingerprint")
    environment_identity = next(iter(receipts.values())).get("environment_identity")
    return {
        "schema_version": 1,
        "candidate_release": RELEASE_LABEL,
        "candidate_package_version": __version__,
        "alembic_head": ALEMBIC_HEAD,
        "qualification_run_id": run_id,
        "deployment_fingerprint": next(iter(deployment_fingerprints)),
        "source_fingerprint": next(iter(source_fingerprints)),
        "runtime_payload_fingerprint": next(iter(runtime_payload_fingerprints)),
        "environment_fingerprint": next(iter(environment_fingerprints)),
        "environment_identity": environment_identity,
        "deployment_artifact_digest": next(iter(artifact_digests)),
        "qualified_at": datetime.now(timezone.utc).isoformat(),
        "promotion_status": "READY_FOR_V1.0_TAG",
        "live_gates": {gate: "PASS" for gate in LIVE_GATES},
        "receipt_sha256": receipt_digests,
        "not_claimed": [
            "provider-level exactly-once Gmail delivery",
            "commercial renewal resolution from outreach alone",
            "production multi-tenant authorization",
            "Calendar integration",
        ],
    }


def promotion_public_key_fingerprint(public_key_pem: bytes) -> str:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is required for release-signature verification") from exc
    key = serialization.load_pem_public_key(public_key_pem)
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def promotion_public_key_from_private(private_key_pem: bytes) -> tuple[bytes, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is required for release signing") from exc
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release signing key must be an Ed25519 private key")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return public_pem, promotion_public_key_fingerprint(public_pem)


def sign_promotion_evidence(evidence: Mapping[str, Any], private_key_pem: bytes) -> tuple[str, bytes, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is required for release signing") from exc
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release signing key must be an Ed25519 private key")
    payload = _canonical_json(evidence).encode("utf-8")
    signature = key.sign(payload)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(signature).decode("ascii"), public_pem, promotion_public_key_fingerprint(public_pem)


def verify_promotion_evidence(
    evidence: Mapping[str, Any], *, signature_b64: str, public_key_pem: bytes, expected_public_key_sha256: str | None = None
) -> None:
    """Verify the signature and identity of an already-built promotion payload.

    This is deliberately a cryptographic verifier, not a substitute for
    ``validate_required_live_qualification``.  Callers that need qualification
    semantics must validate the receipt bundle first, then verify this signature.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is required for release-signature verification") from exc
    observed = promotion_public_key_fingerprint(public_key_pem)
    if expected_public_key_sha256 and observed != _normalized_artifact_digest(expected_public_key_sha256):
        raise ValueError("release public key fingerprint mismatch")
    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("release verification key must be Ed25519")
    try:
        signature = base64.b64decode(signature_b64.encode("ascii"), validate=True)
        key.verify(signature, _canonical_json(evidence).encode("utf-8"))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("invalid V1.0 promotion evidence signature") from exc
