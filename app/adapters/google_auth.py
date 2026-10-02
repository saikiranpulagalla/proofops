from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.ports.protocols import ProviderUnauthorized


SHEETS_READ_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def load_user_credentials(token_file: str | Path, *, scopes: Iterable[str]):
    """Load and refresh OAuth user credentials lazily.

    The function intentionally does not start an interactive flow. Production/demo setup
    is explicit: bootstrap a test-user token once, then the running service only loads and
    refreshes it. This avoids surprise browser flows during a judge demo.
    """

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("Install ProofOps with the 'google' extra") from exc

    path = Path(token_file)
    if not path.exists():
        raise ProviderUnauthorized(f"Google OAuth token file not found: {path}")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), list(scopes))
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise ProviderUnauthorized("Google OAuth credentials are invalid; re-bootstrap test-user token")
        return credentials
    except ProviderUnauthorized:
        raise
    except Exception as exc:
        raise ProviderUnauthorized(f"Google OAuth credentials could not be loaded/refreshed: {exc}") from exc
