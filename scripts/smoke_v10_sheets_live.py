#!/usr/bin/env python3
"""Live V1.0 Google Sheets smoke using a dedicated synthetic CRM row.

The script refuses to mutate unless:
- PROOFOPS_ALLOW_LIVE_SHEETS_SMOKE=YES
- customer_id and renewal_id both start with ``SMOKE-``

It performs update -> exact readback -> stale-version rejection -> status restore.
The synthetic row's version increases by two; all other fields and the original status
are restored and verified.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.google_auth import SHEETS_READ_WRITE_SCOPE, load_user_credentials
from app.adapters.google_sheets import GoogleApiSheetsTransport, GoogleSheetsCRM, SheetsCRMConfig, build_google_sheets_service
from app.ports.protocols import ProviderConflict
from app.release.qualification import write_receipt


def main() -> int:
    if os.environ.get("PROOFOPS_ALLOW_LIVE_SHEETS_SMOKE") != "YES":
        print("Refusing live Sheets mutation. Set PROOFOPS_ALLOW_LIVE_SHEETS_SMOKE=YES explicitly.", file=sys.stderr)
        return 2
    if len(sys.argv) not in {5, 6}:
        print("usage: smoke_v10_sheets_live.py TOKEN_JSON SPREADSHEET_ID SMOKE_CUSTOMER_ID SMOKE_RENEWAL_ID [SHEET_NAME]", file=sys.stderr)
        return 2
    token, spreadsheet_id, customer_id, renewal_id = sys.argv[1:5]
    sheet_name = sys.argv[5] if len(sys.argv) == 6 else "Customers"
    if not customer_id.startswith("SMOKE-") or not renewal_id.startswith("SMOKE-"):
        print("Refusing mutation: customer_id and renewal_id must both start with SMOKE-.", file=sys.stderr)
        return 2

    credentials = load_user_credentials(token, scopes=[SHEETS_READ_WRITE_SCOPE])
    service = build_google_sheets_service(credentials=credentials)
    crm = GoogleSheetsCRM(
        GoogleApiSheetsTransport(service),
        SheetsCRMConfig(spreadsheet_id=spreadsheet_id, sheet_name=sheet_name),
    )
    before = crm.get_customer(customer_id)
    if before is None or before.renewal_id != renewal_id:
        raise SystemExit("synthetic customer/renewal row not found or identity mismatch")

    temporary_status = "deferred" if before.renewal_status.casefold() != "deferred" else "pending"
    changed = crm.update_renewal_status(
        customer_id=customer_id,
        renewal_id=renewal_id,
        expected_version=before.version,
        new_status=temporary_status,
    )
    if not changed.verified or changed.customer.renewal_status != temporary_status:
        raise SystemExit("Sheets update/readback was not verified")

    try:
        crm.update_renewal_status(
            customer_id=customer_id,
            renewal_id=renewal_id,
            expected_version=before.version,
            new_status=before.renewal_status,
        )
    except ProviderConflict:
        pass
    else:
        raise SystemExit("stale-version write was not rejected")

    restored = crm.update_renewal_status(
        customer_id=customer_id,
        renewal_id=renewal_id,
        expected_version=changed.new_version,
        new_status=before.renewal_status,
    )
    if not restored.verified or restored.customer.renewal_status != before.renewal_status:
        raise SystemExit("synthetic row status restore did not verify")
    if restored.customer.version != before.version + 2:
        raise SystemExit("unexpected version progression during live Sheets smoke")

    write_receipt(
        gate="sheets",
        target={"spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name, "customer_id": customer_id, "renewal_id": renewal_id},
        metrics={"stale_version_rejected": True, "verified_readback": True, "version_delta": 2},
    )
    print("PASS: live Sheets synthetic update/readback/stale-version/restore")
    print(f"provider_ref: {restored.provider_ref}")
    print(f"final_status: {restored.customer.renewal_status}")
    print(f"version: {before.version} -> {restored.customer.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
