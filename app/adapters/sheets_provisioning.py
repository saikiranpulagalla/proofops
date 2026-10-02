from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ports.protocols import ProviderConflict, ProviderDataError


@dataclass(frozen=True)
class RowMetadataBinding:
    row_number: int  # 1-based Sheets row number
    renewal_id: str


def bindings_from_rows(rows: list[list[Any]], *, first_row_number: int = 2) -> tuple[RowMetadataBinding, ...]:
    """Extract unique renewal ids from CRM rows without trusting row numbers as identity."""
    seen: set[str] = set()
    out: list[RowMetadataBinding] = []
    for offset, row in enumerate(rows):
        if not any(str(v).strip() for v in row):
            continue
        if len(row) < 4 or not str(row[3]).strip():
            raise ProviderDataError(f"missing renewal_id at row {first_row_number + offset}")
        renewal_id = str(row[3]).strip()
        if renewal_id in seen:
            raise ProviderConflict(f"duplicate renewal_id {renewal_id!r} in sheet")
        seen.add(renewal_id)
        out.append(RowMetadataBinding(first_row_number + offset, renewal_id))
    return tuple(out)


def create_metadata_requests(
    bindings: tuple[RowMetadataBinding, ...],
    *,
    sheet_id: int,
    metadata_key: str = "proofops_renewal_id",
    existing_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for binding in bindings:
        if binding.renewal_id in existing_values:
            continue
        # DimensionRange indexes are zero-based/end-exclusive. Sheet row 2 => [1,2).
        start = binding.row_number - 1
        requests.append({
            "createDeveloperMetadata": {
                "developerMetadata": {
                    "metadataKey": metadata_key,
                    "metadataValue": binding.renewal_id,
                    "visibility": "DOCUMENT",
                    "location": {
                        "dimensionRange": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start,
                            "endIndex": start + 1,
                        }
                    },
                }
            }
        })
    return requests
