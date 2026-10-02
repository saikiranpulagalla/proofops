#!/usr/bin/env python3
"""Attach stable ProofOps renewal metadata to the demo CRM rows.

Usage:
  python scripts/provision_sheets_crm.py TOKEN_JSON SPREADSHEET_ID [SHEET_NAME]
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.google_auth import SHEETS_READ_WRITE_SCOPE, load_user_credentials
from app.adapters.google_sheets import build_google_sheets_service
from app.adapters.sheets_provisioning import bindings_from_rows, create_metadata_requests


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        print("usage: provision_sheets_crm.py TOKEN_JSON SPREADSHEET_ID [SHEET_NAME]")
        return 2
    token, spreadsheet_id = sys.argv[1:3]
    sheet_name = sys.argv[3] if len(sys.argv) == 4 else "Customers"
    credentials = load_user_credentials(token, scopes=[SHEETS_READ_WRITE_SCOPE])
    service = build_google_sheets_service(credentials=credentials)

    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    matches = [s["properties"] for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == sheet_name]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one sheet named {sheet_name!r}, found {len(matches)}")
    sheet_id = matches[0]["sheetId"]

    escaped = sheet_name.replace("'", "''")
    values = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{escaped}'!A2:H",
        majorDimension="ROWS",
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
    ).execute().get("values", [])
    bindings = bindings_from_rows(values)

    search = service.spreadsheets().developerMetadata().search(
        spreadsheetId=spreadsheet_id,
        body={"dataFilters": [{"developerMetadataLookup": {"metadataKey": "proofops_renewal_id", "visibility": "DOCUMENT"}}]},
    ).execute()
    existing_values = frozenset(
        str(item.get("developerMetadata", {}).get("metadataValue", ""))
        for item in search.get("matchedDeveloperMetadata", [])
        if item.get("developerMetadata", {}).get("metadataValue")
    )
    requests = create_metadata_requests(bindings, sheet_id=sheet_id, existing_values=existing_values)
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    print(f"metadata ready: {len(bindings)} rows, {len(requests)} newly tagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
