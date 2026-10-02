import os
import secrets

from app.api.app import ApiSettings, create_app
from app.api.demo_runtime import build_demo_runtime
from app.api.live_runtime import LiveRuntimeConfig, build_live_runtime
from app.version import __version__, RELEASE_STAGE


def _strong_web_settings(*, actor_default: str = "judge", live: bool = False) -> ApiSettings:
    secret = os.environ.get("PROOFOPS_SESSION_SECRET")
    password = os.environ.get("PROOFOPS_UI_PASSWORD") or os.environ.get("PROOFOPS_DEMO_PASSWORD")
    placeholders = ("replace-with", "changeme", "change-me", "example", "default", "password")
    if not secret or len(secret.encode("utf-8")) < 32 or any(x in secret.casefold() for x in placeholders):
        raise RuntimeError("PROOFOPS_SESSION_SECRET must be a non-placeholder high-entropy secret of at least 32 bytes")
    if not password or len(password) < 12 or any(x in password.casefold() for x in placeholders):
        raise RuntimeError("PROOFOPS_UI_PASSWORD must be a non-placeholder strong password")
    allowed_origin = os.environ.get("PROOFOPS_ALLOWED_ORIGIN") or None
    secure_cookie = os.environ.get("PROOFOPS_SECURE_COOKIE", "0") == "1"
    if allowed_origin and allowed_origin.startswith("https://") and not secure_cookie:
        raise RuntimeError("PROOFOPS_SECURE_COOKIE=1 is required for an HTTPS allowed origin")
    if live:
        if not allowed_origin or not allowed_origin.startswith("https://"):
            raise RuntimeError("live runtime requires PROOFOPS_ALLOWED_ORIGIN=https://...")
        if not secure_cookie:
            raise RuntimeError("live runtime requires PROOFOPS_SECURE_COOKIE=1")
    try:
        session_ttl = int(os.environ.get("PROOFOPS_SESSION_TTL_SECONDS", "3600"))
        max_request_bytes = int(os.environ.get("PROOFOPS_MAX_REQUEST_BYTES", "65536"))
    except ValueError as exc:
        raise RuntimeError("PROOFOPS_SESSION_TTL_SECONDS and PROOFOPS_MAX_REQUEST_BYTES must be integers") from exc
    if session_ttl < 300 or session_ttl > 86400:
        raise RuntimeError("PROOFOPS_SESSION_TTL_SECONDS must be between 300 and 86400")
    if max_request_bytes < 4096 or max_request_bytes > 1048576:
        raise RuntimeError("PROOFOPS_MAX_REQUEST_BYTES must be between 4096 and 1048576")
    try:
        login_max_failures = int(os.environ.get("PROOFOPS_LOGIN_MAX_FAILURES", "5"))
        login_window_seconds = int(os.environ.get("PROOFOPS_LOGIN_WINDOW_SECONDS", "300"))
        login_lockout_seconds = int(os.environ.get("PROOFOPS_LOGIN_LOCKOUT_SECONDS", "300"))
    except ValueError as exc:
        raise RuntimeError("login throttle settings must be integers") from exc
    if not (1 <= login_max_failures <= 20):
        raise RuntimeError("PROOFOPS_LOGIN_MAX_FAILURES must be between 1 and 20")
    if not (30 <= login_window_seconds <= 3600 and 30 <= login_lockout_seconds <= 86400):
        raise RuntimeError("login throttle window/lockout settings are out of range")
    return ApiSettings(
        secret_key=secret,
        demo_password=password,
        actor=os.environ.get("PROOFOPS_ACTOR", actor_default),
        secure_cookie=secure_cookie,
        allowed_origin=allowed_origin,
        session_ttl_seconds=session_ttl,
        max_request_bytes=max_request_bytes,
        login_max_failures=login_max_failures,
        login_window_seconds=login_window_seconds,
        login_lockout_seconds=login_lockout_seconds,
    )


def _build():
    runtime = None
    mode = (os.environ.get("PROOFOPS_RUNTIME") or "").strip().casefold()
    if mode == "demo":
        settings = _strong_web_settings()
        runtime = build_demo_runtime(os.environ.get("PROOFOPS_DATABASE_URL", "sqlite:///proofops-v10-demo.sqlite"))
    elif mode == "live":
        # No fallback: if any real-provider or Postgres prerequisite is missing, import
        # fails and the deployment remains unavailable rather than silently switching
        # to fake providers.
        settings = _strong_web_settings(live=True)
        runtime = build_live_runtime(LiveRuntimeConfig.from_env())
    elif mode:
        raise RuntimeError("PROOFOPS_RUNTIME must be 'demo', 'live', or unset")
    else:
        settings = ApiSettings(
            secret_key=secrets.token_urlsafe(48),
            demo_password=secrets.token_urlsafe(32),
            actor="unconfigured",
            login_enabled=False,
        )
    return create_app(runtime=runtime, settings=settings)


app = _build()


def health():
    """Backward-compatible direct health probe used by release metadata tests."""
    return {
        "status": "ok",
        "version": __version__,
        "stage": RELEASE_STAGE,
        "runtime": "configured" if app.state.runtime else "unconfigured",
    }
