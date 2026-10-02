from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.api.schemas import ApproveRequest, CreateRunRequest, ExecuteRequest, LoginRequest, RejectRequest
from app.api.security import SessionClaims, SessionError, SessionSigner
from app.api.ui import APP_JS, INDEX_HTML, STYLE_CSS
from app.domain.enums import ActionState, ApprovalStatus, RunState
from app.engine.workflow import MaterialStateChanged, RenewalWorkflowV08, WorkflowCheckpoint
from app.persistence.repository import ApiIdempotencyConflict, LedgerRepository, LoginRateLimited
from app.security.approval import ApprovalError, ApprovalService
from app.release.qualification import deployment_fingerprint_from_env, release_identity, runtime_environment_fingerprint, runtime_payload_fingerprint
from app.version import __version__, ALEMBIC_HEAD, RELEASE_STAGE

COOKIE_NAME = "proofops_session"


class RequestSizeLimitMiddleware:
    """Enforce body limits before request parsing, including chunked requests.

    The middleware buffers at most ``max_bytes`` plus the chunk that crosses the
    boundary, then replays the bounded body to FastAPI.  This avoids relying on
    Content-Length and prevents parser-level exception translation from hiding a 413.
    """
    def __init__(self, app, *, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_len = headers.get(b"content-length")
        if raw_len is not None:
            try:
                if int(raw_len.decode("ascii")) > self.max_bytes:
                    response = JSONResponse(status_code=413, content={"detail": {"code": "REQUEST_TOO_LARGE", "message": "request body too large", "details": None}})
                    return await response(scope, receive, send)
            except (ValueError, UnicodeDecodeError):
                response = JSONResponse(status_code=400, content={"detail": {"code": "INVALID_CONTENT_LENGTH", "message": "invalid Content-Length", "details": None}})
                return await response(scope, receive, send)

        body_parts: list[bytes] = []
        seen = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                return await self.app(scope, receive, send)
            chunk = message.get("body", b"")
            seen += len(chunk)
            if seen > self.max_bytes:
                response = JSONResponse(status_code=413, content={"detail": {"code": "REQUEST_TOO_LARGE", "message": "request body too large", "details": None}})
                return await response(scope, receive, send)
            body_parts.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(body_parts)
        delivered = False
        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}
        return await self.app(scope, replay_receive, send)


@dataclass(frozen=True)
class ApiSettings:
    secret_key: str
    demo_password: str
    actor: str = "judge"
    session_ttl_seconds: int = 3600
    secure_cookie: bool = False
    allowed_origin: str | None = None
    login_enabled: bool = True
    max_request_bytes: int = 65536
    login_max_failures: int = 5
    login_window_seconds: int = 300
    login_lockout_seconds: int = 300


@dataclass
class ApiRuntime:
    workflow: RenewalWorkflowV08
    repo: LedgerRepository
    approvals: ApprovalService
    provider_probe: Callable[[], dict[str, str]] | None = None


def _checkpoint(cp: WorkflowCheckpoint) -> dict[str, Any]:
    return {
        "run_id": cp.run_id,
        "run_state": cp.run_state.value,
        "action_id": cp.action_id,
        "action_state": cp.action_state.value if cp.action_state else None,
        "approval_id": cp.approval_id,
        "target": cp.target,
        "subject": cp.subject,
        "body": cp.body,
        "reasons": list(cp.reasons),
    }


def _hash_request(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _error(status_code: int, code: str, message: str, *, details: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "details": details})


def _serialize_payload(action) -> dict[str, Any]:
    try:
        return json.loads(action.payload_json)
    except Exception:
        return {"unavailable": True}


def _run_view(runtime: ApiRuntime, run_id: int, actor: str) -> dict[str, Any]:
    run = runtime.repo.get_run_for_actor(run_id, actor)
    if run is None:
        raise _error(404, "RUN_NOT_FOUND", "run not found")
    actions = runtime.repo.actions_for_run(run_id)
    approvals = runtime.repo.approvals_for_run(run_id)
    evidence = runtime.repo.evidence_for_run(run_id)
    audit = runtime.repo.audit_events_for_run(run_id)

    timeline: list[dict[str, str]] = []
    for ev in audit:
        detail = ev.details or ""
        if ev.from_state or ev.to_state:
            state_detail = f"{ev.from_state or ''} → {ev.to_state or ''}".strip()
            detail = f"{state_detail}{' · ' if detail and state_detail else ''}{detail}"
        timeline.append({"at": ev.created_at.astimezone(timezone.utc).isoformat(), "label": f"{ev.object_type}:{ev.event_type}", "detail": detail})
    for ev in evidence:
        timeline.append({"at": ev.observed_at.astimezone(timezone.utc).isoformat(), "label": f"evidence:{ev.evidence_type}", "detail": f"{ev.source_ref} · verified={ev.verified}"})
    timeline.sort(key=lambda x: x["at"])

    return {
        "run": {
            "id": run.id,
            "goal_text": run.goal_text,
            "state": run.state,
            "workflow_type": run.workflow_type,
            "customer_id": run.subject_customer_id,
            "renewal_id": run.subject_renewal_id,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        },
        "actions": [{
            "id": a.id,
            "state": a.state,
            "type": a.action_type,
            "target": a.target,
            "logical_key": a.logical_key,
            "payload": _serialize_payload(a),
        } for a in actions],
        "approvals": [{
            "id": a.id,
            "action_id": a.action_id,
            "status": a.status,
            "actor": a.actor,
            "expires_at": a.expires_at.isoformat(),
        } for a in approvals],
        "evidence": [{
            "type": e.evidence_type,
            "source_ref": e.source_ref,
            "verified": e.verified,
            "observed_at": e.observed_at.isoformat(),
        } for e in evidence],
        "timeline": timeline,
    }


def create_app(*, runtime: ApiRuntime | None = None, settings: ApiSettings | None = None) -> FastAPI:
    if settings is None:
        # Importing the app without configuration must not create a known credential.
        # A runtime supplied without explicit settings is treated as deployment intent
        # and therefore requires strong environment-provided secrets.
        env_secret = os.environ.get("PROOFOPS_SESSION_SECRET")
        env_password = os.environ.get("PROOFOPS_DEMO_PASSWORD")
        if runtime is not None:
            if not env_secret or len(env_secret.encode("utf-8")) < 32:
                raise RuntimeError("PROOFOPS_SESSION_SECRET must be at least 32 bytes when runtime is configured")
            if not env_password or len(env_password) < 12:
                raise RuntimeError("PROOFOPS_DEMO_PASSWORD must be at least 12 characters when runtime is configured")
            settings = ApiSettings(
                secret_key=env_secret, demo_password=env_password,
                actor=os.environ.get("PROOFOPS_ACTOR", "judge"),
                secure_cookie=os.environ.get("PROOFOPS_SECURE_COOKIE", "0") == "1",
                allowed_origin=os.environ.get("PROOFOPS_ALLOWED_ORIGIN") or None,
            )
        else:
            settings = ApiSettings(
                secret_key=secrets.token_urlsafe(48), demo_password=secrets.token_urlsafe(32),
                actor="unconfigured", login_enabled=False,
            )
    if settings.login_enabled and len(settings.demo_password) < 12:
        raise ValueError("demo password must be at least 12 characters")
    if not settings.actor.strip():
        raise ValueError("server actor is required")
    if runtime is not None and settings.allowed_origin and settings.allowed_origin.startswith("https://") and not settings.secure_cookie:
        raise RuntimeError("secure cookies are required for an HTTPS allowed origin")
    signer = SessionSigner(settings.secret_key, ttl_seconds=settings.session_ttl_seconds)
    app = FastAPI(title="ProofOps", version=__version__)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.state.runtime = runtime
    app.state.settings = settings
    app.state.boot_id = secrets.token_urlsafe(24)
    app.state.started_at = datetime.now(timezone.utc)
    identity = release_identity()
    app.state.source_fingerprint = identity["release_source_fingerprint"]
    app.state.runtime_payload_fingerprint = runtime_payload_fingerprint()
    app.state.environment_fingerprint = runtime_environment_fingerprint()
    app.state.deployment_fingerprint = deployment_fingerprint_from_env()
    # This is supplied by the deployment platform/CI, never generated by a worker.
    # It is intentionally distinct from boot_id and the diagnostic worker generation.
    app.state.deployment_revision = os.environ.get("PROOFOPS_DEPLOYMENT_REVISION", "").strip() or None
    app.state.deployment_generation = None
    if runtime is not None:
        app.state.deployment_generation = runtime.repo.register_deployment_boot(
            deployment_key=app.state.deployment_fingerprint, boot_id=app.state.boot_id
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_runtime() -> ApiRuntime:
        rt = app.state.runtime
        if rt is None:
            raise _error(503, "RUNTIME_UNCONFIGURED", "ProofOps runtime is not configured")
        return rt

    def session_claims(proofops_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> SessionClaims:
        if not proofops_session:
            raise _error(401, "AUTH_REQUIRED", "sign in required")
        try:
            claims = signer.verify(proofops_session)
        except SessionError as exc:
            raise _error(401, "INVALID_SESSION", str(exc)) from exc
        rt = app.state.runtime
        if rt is None or not rt.repo.session_is_active(session_id=claims.session_id, actor=claims.actor):
            raise _error(401, "INVALID_SESSION", "session revoked or unavailable")
        return claims

    def mutation_guard(
        request: Request,
        claims: SessionClaims = Depends(session_claims),
        csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> SessionClaims:
        if not csrf or not hmac.compare_digest(csrf, claims.csrf):
            raise _error(403, "CSRF_FAILED", "missing or invalid CSRF token")
        origin = request.headers.get("origin")
        allowed = settings.allowed_origin
        if origin and allowed and origin.rstrip("/") != allowed.rstrip("/"):
            raise _error(403, "ORIGIN_REJECTED", "cross-origin mutation rejected")
        return claims

    def idem_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
        if value is None or not value.strip():
            raise _error(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
        value = value.strip()
        if len(value) > 128:
            raise _error(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key is too long")
        return value

    def own_run(rt: ApiRuntime, run_id: int, actor: str):
        row = rt.repo.get_run_for_actor(run_id, actor)
        if row is None:
            # Deliberately use one response for missing vs foreign runs to avoid ID enumeration.
            raise _error(404, "RUN_NOT_FOUND", "run not found")
        return row

    def _after_or_equal(value, threshold) -> bool:
        if value is None or threshold is None:
            return False
        left = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        right = threshold if threshold.tzinfo is not None else threshold.replace(tzinfo=timezone.utc)
        return left.astimezone(timezone.utc) >= right.astimezone(timezone.utc)

    def _durable_checkpoint_body(rt: ApiRuntime, *, run_id: int, action_id: int | None = None, approval_id: int | None = None) -> dict[str, Any]:
        run = rt.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        action = rt.repo.get_action_by_id(action_id) if action_id is not None else None
        return {
            "run_id": run_id,
            "run_state": run.state,
            "action_id": action.id if action is not None else action_id,
            "action_state": action.state if action is not None else None,
            "approval_id": approval_id,
            "target": action.target if action is not None else None,
            "subject": (_serialize_payload(action).get("subject") if action is not None else None),
            "body": (_serialize_payload(action).get("body_text") if action is not None else None),
            "reasons": ["RECOVERED_IDEMPOTENT_MUTATION"],
        }

    def _recover_unknown_mutation(*, rt: ApiRuntime, actor: str, route: str, record, payload: dict[str, Any]):
        """Return a proven durable response, or reclaim the request when absence is proven.

        A pre-existing run is not proof that approve/execute/reject committed. Each route
        checks the specific durable mutation it was meant to perform.
        """
        run_id = int(payload.get("run_id") or record.resource_id or 0) if (payload.get("run_id") or record.resource_id) else None
        if route == "create_run":
            run = rt.repo.get_run_by_api_request(record.id)
            if run is not None:
                # RC4 could crash after creating the run but before persisting RC5's
                # initialization checkpoint. The idempotency hash proves this retry has
                # the exact original body, so it is safe to bind the missing input once.
                if not run.initial_request_json:
                    if run.goal_text != str(payload.get("goal_text") or ""):
                        raise ApiIdempotencyConflict("durable run goal does not match idempotent create request")
                    init = {
                        "company": str(payload.get("company") or "").strip(),
                        "deadline": payload.get("deadline"),
                    }
                    if not init["company"]:
                        raise ApiIdempotencyConflict("idempotent create request is missing company")
                    rt.repo.bind_initial_request(
                        run_id=run.id,
                        initial_request_json=json.dumps(init, sort_keys=True, separators=(",", ":")),
                    )
                cp = rt.workflow.resume(run_id=run.id)
                body = _checkpoint(cp)
                return "recovered", 202, body
            rt.repo.reclaim_unknown_api_request(record.id)
            return "reclaimed", None, None

        if run_id is None:
            raise ApiIdempotencyConflict("mutation request has no bound run")

        if route == "approve":
            action_id = int(payload.get("action_id") or 0)
            candidates = [
                a for a in rt.repo.approvals_for_run(run_id)
                if a.action_id == action_id and a.actor == actor and a.api_request_id == record.id
            ]
            if candidates:
                approval = candidates[-1]
                body = _durable_checkpoint_body(rt, run_id=run_id, action_id=action_id, approval_id=approval.id)
                return "recovered", 200, body
            rt.repo.reclaim_unknown_api_request(record.id, allow_bound_resource=True)
            return "reclaimed", None, None

        if route == "execute":
            action_id = int(payload.get("action_id") or 0)
            effect = rt.repo.effect_for_api_request(record.id, action_id=action_id) if action_id else None
            if effect is not None:
                cp = rt.workflow.resume(run_id=run_id)
                return "recovered", 200, _checkpoint(cp)
            # A different request may have crossed the effect boundary while this one
            # was in flight.  That proves this request did *not* cause the mutation;
            # never reclaim it into a misleading successful execute response.
            if action_id and rt.repo.effects_for_action(action_id):
                raise ApiIdempotencyConflict("a different execute request caused the durable effect")
            rt.repo.reclaim_unknown_api_request(record.id, allow_bound_resource=True)
            return "reclaimed", None, None

        if route == "reject":
            action_id = int(payload.get("action_id") or 0)
            approval_id = int(payload.get("approval_id") or 0)
            approval = rt.repo.get_approval(approval_id)
            action = rt.repo.get_action_by_id(action_id)
            if (
                approval is not None
                and approval.rejected_api_request_id == record.id
                and action is not None
                and action.run_id == run_id
            ):
                body = _durable_checkpoint_body(rt, run_id=run_id, action_id=action_id, approval_id=approval_id)
                return "recovered", 200, body
            if approval is not None and approval.status == ApprovalStatus.REVOKED.value:
                raise ApiIdempotencyConflict("a different reject request caused the durable revocation")
            rt.repo.reclaim_unknown_api_request(record.id, allow_bound_resource=True)
            return "reclaimed", None, None

        if route == "resume":
            # Resume is recovery-only: it reconciles/repairs state and never turns
            # RETRYABLE into a provider send. Re-executing it after lease expiry is safe.
            rt.repo.reclaim_unknown_api_request(record.id, allow_bound_resource=True)
            return "reclaimed", None, None

        raise ApiIdempotencyConflict(f"no recovery policy for route {route!r}")

    def idempotent_call(*, rt: ApiRuntime, actor: str, route: str, key: str, payload: dict[str, Any], resource_id: str | None, fn: Callable[[Any], tuple[int, dict[str, Any]]]) -> JSONResponse:
        req_hash = _hash_request(payload)
        try:
            record, created = rt.repo.claim_api_request(
                actor=actor, route=route, idempotency_key=key, request_hash=req_hash, resource_id=resource_id,
            )
        except ApiIdempotencyConflict as exc:
            raise _error(409, "IDEMPOTENCY_CONFLICT", str(exc)) from exc
        if not created:
            if record.state == "COMPLETED" and record.response_json is not None and record.status_code is not None:
                return JSONResponse(status_code=record.status_code, content=json.loads(record.response_json), headers={"Idempotent-Replay": "true"})
            if record.state == "UNKNOWN":
                try:
                    outcome, recovered_code, recovered_body = _recover_unknown_mutation(
                        rt=rt, actor=actor, route=route, record=record, payload=payload,
                    )
                except ApiIdempotencyConflict as exc:
                    raise _error(409, "REQUEST_OUTCOME_UNKNOWN", str(exc), details={"resource_id": record.resource_id}) from exc
                if outcome == "recovered":
                    assert recovered_code is not None and recovered_body is not None
                    rt.repo.complete_api_request(
                        record.id, status_code=recovered_code,
                        response_json=json.dumps(recovered_body, sort_keys=True),
                        resource_id=str(recovered_body.get("run_id") or record.resource_id or "") or None,
                    )
                    return JSONResponse(status_code=recovered_code, content=recovered_body, headers={"Idempotent-Recovery": "true"})
                record = rt.repo.get_api_request(actor=actor, route=route, idempotency_key=key)
                created = True
            else:
                raise _error(409, "REQUEST_OUTCOME_UNKNOWN", "an identical request is already in progress or its response was lost", details={"resource_id": record.resource_id})
        try:
            code, body = fn(record)
        except HTTPException:
            raise
        except (ApprovalError, MaterialStateChanged, RuntimeError, ValueError, KeyError) as exc:
            body = {"error": {"code": type(exc).__name__.upper(), "message": str(exc)}}
            code = 409 if not isinstance(exc, KeyError) else 404
            rt.repo.complete_api_request(record.id, status_code=code, response_json=json.dumps(body, sort_keys=True), resource_id=resource_id)
            return JSONResponse(status_code=code, content=body)
        rt.repo.complete_api_request(record.id, status_code=code, response_json=json.dumps(body, sort_keys=True), resource_id=str(body.get("run_id") or resource_id or "") or None)
        return JSONResponse(status_code=code, content=body)

    @app.get("/health")
    def health():
        return {
            "status": "ok", "version": __version__, "stage": RELEASE_STAGE,
            "runtime": "configured" if app.state.runtime else "unconfigured",
            "boot_id": app.state.boot_id,
            "started_at": app.state.started_at.isoformat(),
            "source_fingerprint": app.state.source_fingerprint,
            "runtime_payload_fingerprint": app.state.runtime_payload_fingerprint,
        }

    @app.get("/ready")
    def ready():
        if app.state.runtime is None:
            return JSONResponse(status_code=503, content={"status": "not-ready", "reason": "runtime-unconfigured"})
        try:
            app.state.runtime.repo.healthcheck()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not-ready", "reason": "database-unavailable"})
        return {"status": "ready", "version": __version__}

    @app.get("/ready/providers")
    def ready_providers(
        _claims: SessionClaims = Depends(session_claims),
        rt: ApiRuntime = Depends(require_runtime),
    ):
        # Provider probes can consume Google/Gemini quota and expose deployment state;
        # they are operator-authenticated rather than public load-balancer health checks.
        if rt.provider_probe is None:
            return JSONResponse(status_code=503, content={"status": "not-ready", "reason": "provider-probe-unconfigured"})
        try:
            details = rt.provider_probe()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not-ready", "reason": "provider-unavailable"})
        return {"status": "ready", "providers": details, "version": __version__}

    @app.get("/api/qualification/attestation")
    def qualification_attestation(
        _claims: SessionClaims = Depends(session_claims),
        rt: ApiRuntime = Depends(require_runtime),
    ):
        if os.environ.get("PROOFOPS_QUALIFICATION_MODE") != "YES":
            raise _error(404, "NOT_FOUND", "qualification attestation is unavailable")
        generation = rt.repo.current_deployment_generation(app.state.deployment_fingerprint)
        return {
            "version": __version__,
            "alembic_head": ALEMBIC_HEAD,
            "boot_id": app.state.boot_id,
            "deployment_generation": generation,
            "deployment_revision": app.state.deployment_revision,
            "started_at": app.state.started_at.isoformat(),
            "source_fingerprint": app.state.source_fingerprint,
            "runtime_payload_fingerprint": app.state.runtime_payload_fingerprint,
            "environment_fingerprint": app.state.environment_fingerprint,
            "deployment_fingerprint": app.state.deployment_fingerprint,
            "qualification_mode": True,
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(INDEX_HTML)

    @app.get("/ui/style.css")
    def style():
        return PlainTextResponse(STYLE_CSS, media_type="text/css")

    @app.get("/ui/app.js")
    def javascript():
        return PlainTextResponse(APP_JS, media_type="application/javascript")

    @app.post("/api/session")
    def login(request: Request, body: LoginRequest, response: Response):
        if not settings.login_enabled:
            raise _error(503, "LOGIN_DISABLED", "runtime authentication is not configured")
        rt = app.state.runtime
        if rt is None:
            raise _error(503, "RUNTIME_UNCONFIGURED", "ProofOps runtime is not configured")
        remote = request.client.host if request.client is not None else "unknown"
        throttle_key = f"{settings.actor}:{remote}"
        try:
            rt.repo.begin_login_attempt(
                throttle_key=throttle_key,
                max_failures=settings.login_max_failures,
                window_seconds=settings.login_window_seconds,
                lockout_seconds=settings.login_lockout_seconds,
            )
        except LoginRateLimited as exc:
            raise _error(429, "LOGIN_RATE_LIMITED", str(exc)) from exc
        # ``compare_digest(str, str)`` rejects non-ASCII input with TypeError.  Compare
        # UTF-8 bytes so every syntactically valid password is a normal failed-login
        # candidate rather than an application 500.
        if not hmac.compare_digest(body.password.encode("utf-8"), settings.demo_password.encode("utf-8")):
            raise _error(401, "LOGIN_FAILED", "invalid credentials")
        rt.repo.clear_login_attempts(throttle_key=throttle_key)
        token, claims = signer.issue(settings.actor)
        from datetime import datetime, timezone
        rt.repo.create_session(
            session_id=claims.session_id, actor=claims.actor,
            expires_at=datetime.fromtimestamp(claims.expires_at, tz=timezone.utc),
        )
        response.set_cookie(
            COOKIE_NAME, token, httponly=True, secure=settings.secure_cookie,
            samesite="strict", max_age=settings.session_ttl_seconds, path="/",
        )
        return {"actor": claims.actor, "csrf_token": claims.csrf, "expires_at": claims.expires_at}

    @app.get("/api/session")
    def current_session(claims: SessionClaims = Depends(session_claims)):
        return {"actor": claims.actor, "csrf_token": claims.csrf, "expires_at": claims.expires_at}

    @app.delete("/api/session")
    def logout(response: Response, claims: SessionClaims = Depends(mutation_guard), rt: ApiRuntime = Depends(require_runtime)):
        rt.repo.revoke_session(session_id=claims.session_id, actor=claims.actor)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/runs")
    def list_runs(claims: SessionClaims = Depends(session_claims), rt: ApiRuntime = Depends(require_runtime)):
        return {"runs": [{"id": r.id, "goal_text": r.goal_text, "state": r.state, "updated_at": r.updated_at.isoformat()} for r in rt.repo.list_runs_for_actor(claims.actor)]}

    @app.post("/api/runs")
    def create_run(body: CreateRunRequest, claims: SessionClaims = Depends(mutation_guard), key: str = Depends(idem_key), rt: ApiRuntime = Depends(require_runtime)):
        payload = body.model_dump(mode="json")
        def op(record):
            cp = rt.workflow.prepare(goal_text=body.goal_text, company=body.company, deadline=body.deadline, owner_actor=claims.actor, api_request_id=record.id)
            return 201, _checkpoint(cp)
        return idempotent_call(rt=rt, actor=claims.actor, route="create_run", key=key, payload=payload, resource_id=None, fn=op)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int, claims: SessionClaims = Depends(session_claims), rt: ApiRuntime = Depends(require_runtime)):
        own_run(rt, run_id, claims.actor)
        return _run_view(rt, run_id, claims.actor)

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: int, body: ApproveRequest, claims: SessionClaims = Depends(mutation_guard), key: str = Depends(idem_key), rt: ApiRuntime = Depends(require_runtime)):
        own_run(rt, run_id, claims.actor)
        payload = {"run_id": run_id, **body.model_dump()}
        def op(record):
            cp = rt.workflow.approve(
                run_id=run_id, action_id=body.action_id, actor=claims.actor,
                ttl_seconds=body.ttl_seconds, api_request_id=record.id,
            )
            return 200, _checkpoint(cp)
        return idempotent_call(rt=rt, actor=claims.actor, route="approve", key=key, payload=payload, resource_id=str(run_id), fn=op)

    @app.post("/api/runs/{run_id}/execute")
    def execute(run_id: int, body: ExecuteRequest, claims: SessionClaims = Depends(mutation_guard), key: str = Depends(idem_key), rt: ApiRuntime = Depends(require_runtime)):
        own_run(rt, run_id, claims.actor)
        payload = {"run_id": run_id, **body.model_dump()}
        def op(record):
            action = rt.repo.get_action_by_id(body.action_id)
            if action is None or action.run_id != run_id:
                raise KeyError(body.action_id)
            if ActionState(action.state) in {ActionState.UNKNOWN, ActionState.RECONCILING, ActionState.EXECUTING}:
                cp = rt.workflow.resume(run_id=run_id)
            else:
                cp = rt.workflow.execute(
                    run_id=run_id, action_id=body.action_id, approval_id=body.approval_id,
                    actor=claims.actor, api_request_id=record.id,
                )
            return 200, _checkpoint(cp)
        return idempotent_call(rt=rt, actor=claims.actor, route="execute", key=key, payload=payload, resource_id=str(run_id), fn=op)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: int, claims: SessionClaims = Depends(mutation_guard), key: str = Depends(idem_key), rt: ApiRuntime = Depends(require_runtime)):
        own_run(rt, run_id, claims.actor)
        payload = {"run_id": run_id}
        def op(record):
            cp = rt.workflow.resume(run_id=run_id)
            return 200, _checkpoint(cp)
        return idempotent_call(rt=rt, actor=claims.actor, route="resume", key=key, payload=payload, resource_id=str(run_id), fn=op)

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: int, body: RejectRequest, claims: SessionClaims = Depends(mutation_guard), key: str = Depends(idem_key), rt: ApiRuntime = Depends(require_runtime)):
        run = own_run(rt, run_id, claims.actor)
        payload = {"run_id": run_id, **body.model_dump()}
        def op(record):
            approval = rt.repo.get_approval(body.approval_id)
            action = rt.repo.get_action_by_id(body.action_id)
            if approval is None or action is None or approval.run_id != run_id or action.run_id != run_id or approval.action_id != action.id:
                raise KeyError(body.approval_id)
            if approval.actor != claims.actor:
                raise ApprovalError("approval actor mismatch")

            run_now = rt.repo.get_run(run_id)
            action_state = ActionState(action.state)
            run_state = RunState(run_now.state)

            # Once authorization has been CLAIMED the external request may already be
            # in flight.  Never tell the operator that cancellation succeeded.
            if approval.status in {ApprovalStatus.CLAIMED.value, ApprovalStatus.CONSUMED.value} or action_state in {
                ActionState.PREPARED, ActionState.EXECUTING, ActionState.UNKNOWN,
                ActionState.RECONCILING, ActionState.RETRYABLE, ActionState.CONFIRMED,
            }:
                body_out = {
                    "error": {
                        "code": "TOO_LATE_TO_CANCEL" if run_state not in {RunState.COMPLETED_VERIFIED, RunState.COMPLETED_NO_ACTION_REQUIRED, RunState.CANCELED, RunState.FAILED} else "ALREADY_TERMINAL",
                        "message": "execution has already crossed the cancellation boundary",
                    },
                    "run_id": run_id, "run_state": run_state.value,
                    "action_id": action.id, "action_state": action_state.value,
                }
                return 409, body_out

            if action_state == ActionState.CANCELED or run_state == RunState.CANCELED:
                return 200, {"run_id": run_id, "run_state": run_state.value, "action_id": action.id, "action_state": action_state.value}

            if approval.status != ApprovalStatus.ACTIVE.value:
                return 409, {
                    "error": {"code": "CANNOT_CANCEL", "message": f"approval is {approval.status.lower()}"},
                    "run_id": run_id, "run_state": run_state.value,
                    "action_id": action.id, "action_state": action_state.value,
                }

            rt.approvals.revoke(approval.id, api_request_id=record.id)
            action = rt.repo.get_action_by_id(action.id)
            if ActionState(action.state) == ActionState.APPROVAL_REQUIRED:
                action = rt.repo.transition_action_state(action.id, ActionState.CANCELED, expected=ActionState.APPROVAL_REQUIRED)
            current = rt.repo.get_run(run_id)
            if RunState(current.state) == RunState.WAITING_APPROVAL:
                current = rt.repo.transition_run_state(run_id, RunState.CANCELED, expected=RunState.WAITING_APPROVAL)
            return 200, {"run_id": run_id, "run_state": current.state, "action_id": action.id, "action_state": action.state}
        return idempotent_call(rt=rt, actor=claims.actor, route="reject", key=key, payload=payload, resource_id=str(run_id), fn=op)

    return app
