#!/usr/bin/env python3
"""Offline V0.6 behavior demo using the same GoogleSheetsCRM logic with memory transport."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.google_sheets import GoogleSheetsCRM, SheetsCRMConfig
from app.ports.protocols import ProviderConflict


class MemoryTransport:
    def __init__(self):
        self.row = [
            "C001", "ACME", "alice@acme.com", "R2026-C001", "pending",
            "2026-10-20T17:00:00+00:00", False, 1,
        ]
    def get_values(self, **kwargs): return [list(self.row)]
    def resolve_metadata_ids(self, **kwargs): return [1002]
    def get_rows_by_metadata_id(self, **kwargs): return [("Customers!A2:H2", list(self.row))]
    def update_rows_by_metadata_id(self, *, values, **kwargs):
        self.row = list(values[0])
        return {"totalUpdatedRows": 1}


def main():
    t = MemoryTransport()
    crm = GoogleSheetsCRM(t, SheetsCRMConfig(spreadsheet_id="demo"))
    before = crm.get_customer("C001")
    print("BEFORE", before.renewal_status, "version", before.version)
    result = crm.update_renewal_status(
        customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed"
    )
    print("VERIFIED", result.customer.renewal_status, "version", result.new_version, result.provider_ref)
    try:
        crm.update_renewal_status(
            customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="declined"
        )
    except ProviderConflict as exc:
        print("STALE WRITE BLOCKED", exc)


if __name__ == "__main__":
    main()
