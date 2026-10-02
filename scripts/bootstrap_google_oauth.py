#!/usr/bin/env python3
"""One-time local OAuth bootstrap for the hackathon test account.

Usage:
  python scripts/bootstrap_google_oauth.py client_secret.json token.json

Bootstraps one dedicated hackathon test-user token for both Sheets CRM and Gmail.
Gmail uses gmail.modify because ProofOps must both send and read back/search messages for
verification and reconciliation. Keep the OAuth app in Testing with explicit test users.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.google_auth import GMAIL_MODIFY_SCOPE, SHEETS_READ_WRITE_SCOPE


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: bootstrap_google_oauth.py CLIENT_SECRET_JSON TOKEN_JSON")
        return 2
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install ProofOps with: pip install -e '.[google]'", file=sys.stderr)
        return 2

    client_secret, token_path = map(Path, sys.argv[1:])
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret), [SHEETS_READ_WRITE_SCOPE, GMAIL_MODIFY_SCOPE]
    )
    credentials = flow.run_local_server(port=0)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    print(f"wrote OAuth token: {token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
