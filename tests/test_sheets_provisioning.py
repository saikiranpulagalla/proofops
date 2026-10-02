import pytest

from app.adapters.sheets_provisioning import bindings_from_rows, create_metadata_requests
from app.ports.protocols import ProviderConflict, ProviderDataError


def test_bindings_track_actual_sheet_row_numbers_including_blanks():
    rows = [
        ["C1", "A", "a@x", "R1"],
        [],
        ["C2", "B", "b@x", "R2"],
    ]
    bindings = bindings_from_rows(rows, first_row_number=2)
    assert [(b.row_number, b.renewal_id) for b in bindings] == [(2, "R1"), (4, "R2")]


def test_missing_renewal_id_fails_provisioning():
    with pytest.raises(ProviderDataError, match="missing renewal_id"):
        bindings_from_rows([["C1", "A", "a@x", ""]])


def test_duplicate_renewal_id_fails_provisioning():
    with pytest.raises(ProviderConflict, match="duplicate renewal_id"):
        bindings_from_rows([
            ["C1", "A", "a@x", "R1"],
            ["C2", "B", "b@x", "R1"],
        ])


def test_metadata_requests_use_zero_based_single_row_dimension_ranges():
    bindings = bindings_from_rows([
        ["C1", "A", "a@x", "R1"],
        ["C2", "B", "b@x", "R2"],
    ])
    requests = create_metadata_requests(bindings, sheet_id=777)
    first = requests[0]["createDeveloperMetadata"]["developerMetadata"]
    assert first["metadataKey"] == "proofops_renewal_id"
    assert first["metadataValue"] == "R1"
    assert first["location"]["dimensionRange"] == {
        "sheetId": 777,
        "dimension": "ROWS",
        "startIndex": 1,
        "endIndex": 2,
    }


def test_existing_metadata_is_not_duplicated():
    bindings = bindings_from_rows([
        ["C1", "A", "a@x", "R1"],
        ["C2", "B", "b@x", "R2"],
    ])
    requests = create_metadata_requests(bindings, sheet_id=1, existing_values=frozenset({"R1"}))
    assert len(requests) == 1
    assert requests[0]["createDeveloperMetadata"]["developerMetadata"]["metadataValue"] == "R2"
