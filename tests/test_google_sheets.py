from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any

import pytest

from app.adapters.google_sheets import (
    CRM_COLUMNS,
    GoogleApiSheetsTransport,
    GoogleSheetsCRM,
    SheetsCRMConfig,
)
from app.domain.models import CustomerRecord
from app.ports.protocols import (
    ProviderConflict,
    ProviderDataError,
    ProviderUnauthorized,
    ProviderUnavailable,
)


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def row(
    *,
    customer_id="C001",
    company="ACME",
    contact="alice@acme.com",
    renewal_id="R2026-C001",
    status="pending",
    renewal_date="2026-10-20T17:00:00+00:00",
    dnc=False,
    version=1,
):
    return [customer_id, company, contact, renewal_id, status, renewal_date, dnc, version]


@dataclass
class MemorySheetsTransport:
    all_rows: list[list[Any]] = field(default_factory=lambda: [row()])
    metadata_rows: dict[str, list[tuple[int, str, list[Any]]]] = field(default_factory=dict)
    read_error: Exception | None = None
    metadata_error: Exception | None = None
    write_error: Exception | None = None
    update_count: int = 0
    force_total_updated_rows: int | None = 1
    post_write_mutator: Any = None

    def __post_init__(self):
        if not self.metadata_rows:
            for idx, values in enumerate(self.all_rows, start=2):
                if len(values) >= 4 and values[3]:
                    self.metadata_rows.setdefault(str(values[3]), []).append((1000 + idx, f"Customers!A{idx}:H{idx}", list(values)))

    def get_values(self, *, spreadsheet_id: str, range_name: str):
        if self.read_error:
            raise self.read_error
        return [list(r) for r in self.all_rows]

    def resolve_metadata_ids(self, *, spreadsheet_id: str, metadata_key: str, metadata_value: str):
        if self.metadata_error:
            raise self.metadata_error
        return [mid for mid, _, _ in self.metadata_rows.get(metadata_value, [])]

    def get_rows_by_metadata_id(self, *, spreadsheet_id: str, metadata_id: int):
        if self.metadata_error:
            raise self.metadata_error
        out = []
        for entries in self.metadata_rows.values():
            for mid, ref, values in entries:
                if mid == metadata_id:
                    out.append((ref, list(values)))
        return out

    def update_rows_by_metadata_id(self, *, spreadsheet_id: str, metadata_id: int, values: list[list[Any]]):
        if self.write_error:
            raise self.write_error
        self.update_count += 1
        for renewal_id, entries in list(self.metadata_rows.items()):
            for idx, (mid, ref, _) in enumerate(entries):
                if mid == metadata_id:
                    written = list(values[0])
                    if self.post_write_mutator:
                        written = list(self.post_write_mutator(written))
                    entries[idx] = (mid, ref, written)
                    self.metadata_rows[renewal_id] = entries
                    for row_idx, existing in enumerate(self.all_rows):
                        if len(existing) >= 4 and str(existing[3]) == renewal_id:
                            self.all_rows[row_idx] = list(written)
                    return {"totalUpdatedRows": self.force_total_updated_rows}
        return {"totalUpdatedRows": 0 if self.force_total_updated_rows == 1 else self.force_total_updated_rows}


def crm(transport=None):
    return GoogleSheetsCRM(
        transport or MemorySheetsTransport(),
        SheetsCRMConfig(spreadsheet_id="sheet-123", sheet_name="Customers"),
    )


def test_config_escapes_sheet_name():
    cfg = SheetsCRMConfig(spreadsheet_id="x", sheet_name="Bob's CRM")
    assert cfg.full_data_range == "'Bob''s CRM'!A2:H"


def test_find_customer_exact_casefold():
    adapter = crm(MemorySheetsTransport(all_rows=[row(company="AcMe")]))
    matches = adapter.find_customers("  acme ")
    assert [m.customer_id for m in matches] == ["C001"]


def test_blank_company_returns_no_match_without_error():
    assert crm().find_customers("  ") == []


def test_duplicate_company_remains_ambiguous():
    t = MemorySheetsTransport(all_rows=[row(customer_id="C1", renewal_id="R1"), row(customer_id="C2", renewal_id="R2")])
    assert len(crm(t).find_customers("ACME")) == 2


def test_duplicate_customer_id_is_provider_conflict():
    t = MemorySheetsTransport(all_rows=[row(renewal_id="R1"), row(renewal_id="R2")])
    with pytest.raises(ProviderConflict, match="duplicate customer_id"):
        crm(t).get_customer("C001")


@pytest.mark.parametrize(
    "badrow, message",
    [
        (row(customer_id=""), "missing required CRM fields"),
        (row(version="abc"), "invalid version"),
        (row(version=0), "version must be >=1"),
        (row(dnc="MAYBE"), "invalid do_not_contact"),
        (row(renewal_date="10/20/2026"), "ISO-8601"),
        (row(renewal_date="2026-10-20T17:00:00"), "timezone"),
    ],
)
def test_malformed_rows_fail_closed(badrow, message):
    with pytest.raises(ProviderDataError, match=message):
        crm(MemorySheetsTransport(all_rows=[badrow])).find_customers("ACME")


def test_missing_metadata_blocks_write():
    t = MemorySheetsTransport(all_rows=[row()])
    t.metadata_rows.clear()
    with pytest.raises(ProviderConflict, match="no developer-metadata row"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")
    assert t.update_count == 0


def test_duplicate_metadata_blocks_write():
    t = MemorySheetsTransport()
    t.metadata_rows["R2026-C001"].append((9999, "Customers!A9:H9", row()))
    with pytest.raises(ProviderConflict, match="ambiguous"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")
    assert t.update_count == 0


def test_metadata_pointing_at_other_renewal_blocks_write():
    t = MemorySheetsTransport()
    mid, ref, values = t.metadata_rows.pop("R2026-C001")[0]
    t.metadata_rows["R2026-C001"] = [(mid, ref, row(renewal_id="R-OTHER"))]
    with pytest.raises(ProviderConflict, match="different renewal_id"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_wrong_customer_on_metadata_row_blocks_write():
    t = MemorySheetsTransport()
    mid, ref, _ = t.metadata_rows["R2026-C001"][0]
    t.metadata_rows["R2026-C001"] = [(mid, ref, row(customer_id="C999"))]
    with pytest.raises(ProviderConflict, match="different customer"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_stale_version_blocks_before_provider_write():
    t = MemorySheetsTransport(all_rows=[row(version=4)])
    with pytest.raises(ProviderConflict, match="stale CRM version"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=3, new_status="renewed")
    assert t.update_count == 0


def test_unknown_or_formula_like_status_is_rejected_before_write():
    t = MemorySheetsTransport()
    with pytest.raises(ValueError, match="unsupported canonical renewal status"):
        crm(t).update_renewal_status(
            customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="=HYPERLINK(\"x\")"
        )
    assert t.update_count == 0


def test_verified_update_increments_version_once():
    t = MemorySheetsTransport()
    result = crm(t).update_renewal_status(
        customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed"
    )
    assert result.verified is True
    assert result.previous_version == 1
    assert result.new_version == 2
    assert result.customer.renewal_status == "renewed"
    assert result.customer.version == 2
    assert t.update_count == 1


def test_provider_reported_zero_updated_rows_is_conflict():
    t = MemorySheetsTransport(force_total_updated_rows=0)
    with pytest.raises(ProviderConflict, match="expected one updated row"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_post_write_version_mismatch_is_not_success():
    t = MemorySheetsTransport(post_write_mutator=lambda r: r[:-1] + [99])
    with pytest.raises(ProviderConflict, match="post-write version mismatch"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_post_write_status_mismatch_is_not_success():
    def mutate(r):
        r[4] = "pending"
        return r
    t = MemorySheetsTransport(post_write_mutator=mutate)
    with pytest.raises(ProviderConflict, match="post-write status mismatch"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_post_write_identity_or_critical_field_corruption_is_not_success():
    def mutate(r):
        r[2] = "attacker@example.com"
        return r
    t = MemorySheetsTransport(post_write_mutator=mutate)
    with pytest.raises(ProviderConflict, match="post-write field mismatch: contact_email"):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_read_provider_errors_are_not_converted_to_not_found():
    t = MemorySheetsTransport(read_error=ProviderUnavailable("down"))
    with pytest.raises(ProviderUnavailable):
        crm(t).get_customer("C001")


def test_metadata_provider_error_propagates():
    t = MemorySheetsTransport(metadata_error=ProviderUnauthorized("reauth"))
    with pytest.raises(ProviderUnauthorized):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_write_provider_error_propagates_without_claiming_success():
    t = MemorySheetsTransport(write_error=ProviderUnavailable("429"))
    with pytest.raises(ProviderUnavailable):
        crm(t).update_renewal_status(customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status="renewed")


def test_two_same_process_writers_same_expected_version_only_one_succeeds():
    t = MemorySheetsTransport()
    adapter = crm(t)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(status):
        barrier.wait()
        try:
            adapter.update_renewal_status(
                customer_id="C001", renewal_id="R2026-C001", expected_version=1, new_status=status
            )
            outcomes.append("ok")
        except ProviderConflict:
            outcomes.append("conflict")

    a = threading.Thread(target=writer, args=("renewed",))
    b = threading.Thread(target=writer, args=("declined",))
    a.start(); b.start(); a.join(); b.join()
    assert sorted(outcomes) == ["conflict", "ok"]
    assert t.update_count == 1


# ----- Thin google-api transport shape/error tests -----


class FakeHttpError(Exception):
    def __init__(self, status):
        self.resp = type("Resp", (), {"status": status})()
        super().__init__(f"http {status}")


class Request:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
    def execute(self):
        if self.error:
            raise self.error
        return self.response


class ValuesResource:
    def __init__(self):
        self.calls = []
        self.next_response = {}
        self.next_error = None
    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return Request(self.next_response, self.next_error)
    def batchGetByDataFilter(self, **kwargs):
        self.calls.append(("batchGetByDataFilter", kwargs))
        return Request(self.next_response, self.next_error)
    def batchUpdateByDataFilter(self, **kwargs):
        self.calls.append(("batchUpdateByDataFilter", kwargs))
        return Request(self.next_response, self.next_error)


class DeveloperMetadataResource:
    def __init__(self):
        self.calls = []
        self.next_response = {}
        self.next_error = None
    def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return Request(self.next_response, self.next_error)


class SpreadsheetResource:
    def __init__(self, values, metadata):
        self._values = values
        self._metadata = metadata
    def values(self): return self._values
    def developerMetadata(self): return self._metadata


class FakeGoogleService:
    def __init__(self):
        self.values_resource = ValuesResource()
        self.metadata_resource = DeveloperMetadataResource()
        self.sheet_resource = SpreadsheetResource(self.values_resource, self.metadata_resource)
    def spreadsheets(self): return self.sheet_resource


def test_google_transport_reads_unformatted_values():
    svc = FakeGoogleService()
    svc.values_resource.next_response = {"values": [["C1"]]}
    transport = GoogleApiSheetsTransport(svc)
    assert transport.get_values(spreadsheet_id="s", range_name="Customers!A2:H") == [["C1"]]
    _, kwargs = svc.values_resource.calls[-1]
    assert kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert kwargs["majorDimension"] == "ROWS"


def test_google_transport_resolves_unique_metadata_ids_by_key_value():
    svc = FakeGoogleService()
    svc.metadata_resource.next_response = {
        "matchedDeveloperMetadata": [{"developerMetadata": {"metadataId": 77}}]
    }
    ids = GoogleApiSheetsTransport(svc).resolve_metadata_ids(
        spreadsheet_id="s", metadata_key="proofops_renewal_id", metadata_value="R2026-C001"
    )
    assert ids == [77]
    _, kwargs = svc.metadata_resource.calls[-1]
    lookup = kwargs["body"]["dataFilters"][0]["developerMetadataLookup"]
    assert lookup["metadataKey"] == "proofops_renewal_id"
    assert lookup["metadataValue"] == "R2026-C001"


def test_google_transport_metadata_read_targets_provider_metadata_id():
    svc = FakeGoogleService()
    svc.values_resource.next_response = {
        "valueRanges": [{"valueRange": {"range": "Customers!A2:H2", "values": [row()]}}]
    }
    rows = GoogleApiSheetsTransport(svc).get_rows_by_metadata_id(spreadsheet_id="s", metadata_id=77)
    assert rows[0][0] == "Customers!A2:H2"
    _, kwargs = svc.values_resource.calls[-1]
    lookup = kwargs["body"]["dataFilters"][0]["developerMetadataLookup"]
    assert lookup == {"metadataId": 77}


def test_google_transport_write_is_raw_and_metadata_id_targeted():
    svc = FakeGoogleService()
    GoogleApiSheetsTransport(svc).update_rows_by_metadata_id(
        spreadsheet_id="s", metadata_id=77, values=[["=evil"]]
    )
    _, kwargs = svc.values_resource.calls[-1]
    body = kwargs["body"]
    assert body["valueInputOption"] == "RAW"
    assert body["includeValuesInResponse"] is True
    assert body["data"][0]["dataFilter"]["developerMetadataLookup"] == {"metadataId": 77}
    assert body["data"][0]["values"] == [["=evil"]]


@pytest.mark.parametrize(
    "status, exc_type",
    [(401, ProviderUnauthorized), (403, ProviderUnauthorized), (429, ProviderUnavailable), (500, ProviderUnavailable), (503, ProviderUnavailable), (400, ProviderDataError)],
)
def test_google_transport_maps_http_failures(status, exc_type):
    svc = FakeGoogleService()
    svc.values_resource.next_error = FakeHttpError(status)
    with pytest.raises(exc_type):
        GoogleApiSheetsTransport(svc).get_values(spreadsheet_id="s", range_name="A1")
