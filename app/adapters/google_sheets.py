from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Protocol

from app.domain.models import CRMUpdateResult, CustomerRecord
from app.ports.protocols import (
    ProviderConflict,
    ProviderDataError,
    ProviderUnauthorized,
    ProviderUnavailable,
)


CRM_COLUMNS = (
    "customer_id",
    "company",
    "contact_email",
    "renewal_id",
    "renewal_status",
    "renewal_date",
    "do_not_contact",
    "version",
)


class SheetsTransport(Protocol):
    def get_values(self, *, spreadsheet_id: str, range_name: str) -> list[list[Any]]: ...

    def resolve_metadata_ids(
        self, *, spreadsheet_id: str, metadata_key: str, metadata_value: str
    ) -> list[int]: ...

    def get_rows_by_metadata_id(
        self, *, spreadsheet_id: str, metadata_id: int
    ) -> list[tuple[str, list[Any]]]: ...

    def update_rows_by_metadata_id(
        self, *, spreadsheet_id: str, metadata_id: int, values: list[list[Any]]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SheetsCRMConfig:
    spreadsheet_id: str
    sheet_name: str = "Customers"
    metadata_key: str = "proofops_renewal_id"
    data_range: str = "A2:H"

    @property
    def full_data_range(self) -> str:
        escaped = self.sheet_name.replace("'", "''")
        return f"'{escaped}'!{self.data_range}"


class GoogleApiSheetsTransport:
    """Thin adapter around a google-api-python-client Sheets service.

    The service is injected so the safety/business logic can be tested offline. A real
    service can be built with ``build_google_sheets_service`` when optional Google
    dependencies and credentials are available.
    """

    def __init__(self, service: Any):
        self.service = service

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        status = getattr(getattr(exc, "resp", None), "status", None)
        status = status or getattr(exc, "status_code", None)
        if status in {401, 403}:
            return ProviderUnauthorized(f"Google Sheets authorization failed ({status})")
        if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
            return ProviderUnavailable(f"Google Sheets unavailable ({status})")
        return ProviderDataError(f"Google Sheets request failed: {exc}")

    def get_values(self, *, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        try:
            response = (
                self.service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    majorDimension="ROWS",
                    valueRenderOption="UNFORMATTED_VALUE",
                    dateTimeRenderOption="FORMATTED_STRING",
                )
                .execute()
            )
            return response.get("values", [])
        except Exception as exc:  # google client type is optional at import time
            mapped = self._map_error(exc)
            raise mapped from exc

    def resolve_metadata_ids(
        self, *, spreadsheet_id: str, metadata_key: str, metadata_value: str
    ) -> list[int]:
        body = {
            "dataFilters": [
                {
                    "developerMetadataLookup": {
                        "metadataKey": metadata_key,
                        "metadataValue": metadata_value,
                        "visibility": "DOCUMENT",
                    }
                }
            ]
        }
        try:
            response = (
                self.service.spreadsheets()
                .developerMetadata()
                .search(spreadsheetId=spreadsheet_id, body=body)
                .execute()
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            raise mapped from exc
        ids: list[int] = []
        for match in response.get("matchedDeveloperMetadata", []):
            metadata_id = match.get("developerMetadata", {}).get("metadataId")
            if metadata_id is not None:
                ids.append(int(metadata_id))
        return ids

    def get_rows_by_metadata_id(
        self, *, spreadsheet_id: str, metadata_id: int
    ) -> list[tuple[str, list[Any]]]:
        body = {
            "dataFilters": [{"developerMetadataLookup": {"metadataId": metadata_id}}],
            "majorDimension": "ROWS",
            "valueRenderOption": "UNFORMATTED_VALUE",
            "dateTimeRenderOption": "FORMATTED_STRING",
        }
        try:
            response = (
                self.service.spreadsheets()
                .values()
                .batchGetByDataFilter(spreadsheetId=spreadsheet_id, body=body)
                .execute()
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            raise mapped from exc
        rows: list[tuple[str, list[Any]]] = []
        for entry in response.get("valueRanges", []):
            value_range = entry.get("valueRange", {})
            values = value_range.get("values", [])
            range_name = value_range.get("range", "")
            for row in values:
                rows.append((range_name, list(row)))
        return rows

    def update_rows_by_metadata_id(
        self, *, spreadsheet_id: str, metadata_id: int, values: list[list[Any]]
    ) -> dict[str, Any]:
        body = {
            "valueInputOption": "RAW",
            "includeValuesInResponse": True,
            "data": [
                {
                    "dataFilter": {
                        "developerMetadataLookup": {
                            "metadataId": metadata_id,
                        }
                    },
                    "majorDimension": "ROWS",
                    "values": values,
                }
            ],
        }
        try:
            return (
                self.service.spreadsheets()
                .values()
                .batchUpdateByDataFilter(spreadsheetId=spreadsheet_id, body=body)
                .execute()
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            raise mapped from exc


def build_google_sheets_service(*, credentials: Any):
    """Build a real Sheets v4 service lazily.

    Keeping the import here prevents offline/test installations from requiring Google
    packages just to import ProofOps.
    """

    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(
            "Install ProofOps with the 'google' extra to use the real Sheets adapter"
        ) from exc
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


class GoogleSheetsCRM:
    """Typed CRM adapter backed by a Google Sheet.

    Reads by company/customer are intentionally simple scans for a small hackathon CRM.
    Consequential writes never target row numbers: they target a stable renewal row via
    developer metadata, require an expected version, use RAW values, and read back the
    complete row before reporting success.

    This is optimistic detection, not a claim of provider-side compare-and-swap against
    arbitrary external editors.
    """

    def __init__(self, transport: SheetsTransport, config: SheetsCRMConfig):
        if not config.spreadsheet_id.strip():
            raise ValueError("spreadsheet_id is required")
        self.transport = transport
        self.config = config
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, renewal_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(renewal_id, threading.RLock())

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0", ""}:
            return False
        raise ProviderDataError(f"invalid do_not_contact value: {value!r}")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value).strip()
            if not text:
                raise ProviderDataError("renewal_date is required")
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProviderDataError(
                    "renewal_date must be an ISO-8601 timestamp stored as RAW text"
                ) from exc
        if dt.tzinfo is None:
            raise ProviderDataError("renewal_date must include a timezone")
        return dt.astimezone(timezone.utc)

    @classmethod
    def _parse_row(cls, row: list[Any], *, provider_ref: str) -> CustomerRecord:
        padded = list(row[: len(CRM_COLUMNS)]) + [""] * max(0, len(CRM_COLUMNS) - len(row))
        customer_id, company, contact_email, renewal_id, status, renewal_date, dnc, version = padded
        required = {
            "customer_id": customer_id,
            "company": company,
            "renewal_id": renewal_id,
            "renewal_status": status,
        }
        missing = [k for k, v in required.items() if not str(v).strip()]
        if missing:
            raise ProviderDataError(f"missing required CRM fields {missing} at {provider_ref}")
        try:
            version_int = int(version)
        except (TypeError, ValueError) as exc:
            raise ProviderDataError(f"invalid version {version!r} at {provider_ref}") from exc
        if version_int < 1:
            raise ProviderDataError(f"version must be >=1 at {provider_ref}")
        return CustomerRecord(
            customer_id=str(customer_id).strip(),
            company=str(company).strip(),
            contact_email=str(contact_email).strip() or None,
            renewal_id=str(renewal_id).strip(),
            renewal_status=str(status).strip(),
            renewal_date=cls._parse_datetime(renewal_date),
            do_not_contact=cls._parse_bool(dnc),
            version=version_int,
        )

    @staticmethod
    def _serialize_row(customer: CustomerRecord) -> list[Any]:
        return [
            customer.customer_id,
            customer.company,
            customer.contact_email or "",
            customer.renewal_id,
            customer.renewal_status,
            customer.renewal_date.astimezone(timezone.utc).isoformat() if customer.renewal_date else "",
            customer.do_not_contact,
            customer.version,
        ]

    def _all_rows(self) -> list[tuple[str, CustomerRecord]]:
        rows = self.transport.get_values(
            spreadsheet_id=self.config.spreadsheet_id,
            range_name=self.config.full_data_range,
        )
        parsed: list[tuple[str, CustomerRecord]] = []
        for offset, row in enumerate(rows, start=2):
            if not any(str(v).strip() for v in row):
                continue
            ref = f"{self.config.sheet_name}!row:{offset}"
            parsed.append((ref, self._parse_row(row, provider_ref=ref)))
        return parsed

    def find_customers(self, company: str) -> list[CustomerRecord]:
        query = company.strip().casefold()
        if not query:
            return []
        return [c for _, c in self._all_rows() if c.company.casefold() == query]

    def get_customer(self, customer_id: str) -> CustomerRecord | None:
        matches = [c for _, c in self._all_rows() if c.customer_id == customer_id]
        if len(matches) > 1:
            raise ProviderConflict(f"duplicate customer_id {customer_id!r} in CRM")
        return matches[0] if matches else None

    def _get_exact_renewal_row(self, renewal_id: str, *, metadata_id: int | None = None) -> tuple[int, str, CustomerRecord]:
        if metadata_id is None:
            metadata_ids = self.transport.resolve_metadata_ids(
                spreadsheet_id=self.config.spreadsheet_id,
                metadata_key=self.config.metadata_key,
                metadata_value=renewal_id,
            )
            if not metadata_ids:
                raise ProviderConflict(f"no developer-metadata row for renewal {renewal_id!r}")
            if len(metadata_ids) != 1:
                raise ProviderConflict(f"ambiguous developer-metadata rows for renewal {renewal_id!r}")
            metadata_id = metadata_ids[0]
        rows = self.transport.get_rows_by_metadata_id(
            spreadsheet_id=self.config.spreadsheet_id, metadata_id=metadata_id
        )
        if len(rows) != 1:
            raise ProviderConflict(f"metadata id {metadata_id} does not resolve to exactly one row")
        provider_ref, values = rows[0]
        record = self._parse_row(values, provider_ref=provider_ref or renewal_id)
        if record.renewal_id != renewal_id:
            raise ProviderConflict("developer metadata points to a different renewal_id")
        return metadata_id, provider_ref or f"metadata:{metadata_id}", record

    def update_renewal_status(
        self,
        *,
        customer_id: str,
        renewal_id: str,
        expected_version: int,
        new_status: str,
    ) -> CRMUpdateResult:
        if expected_version < 1:
            raise ValueError("expected_version must be >=1")
        status = new_status.strip().casefold()
        allowed_statuses = {"pending", "renewed", "declined", "canceled", "expired", "deferred"}
        if status not in allowed_statuses:
            raise ValueError(f"unsupported canonical renewal status: {new_status!r}")

        # Local lock prevents duplicate same-process writers. Cross-worker safety is
        # expected to come from ProofOps' durable logical-action/effect ledger.
        with self._lock_for(renewal_id):
            metadata_id, provider_ref, current = self._get_exact_renewal_row(renewal_id)
            if current.customer_id != customer_id:
                raise ProviderConflict("renewal row belongs to a different customer")
            if current.version != expected_version:
                raise ProviderConflict(
                    f"stale CRM version: expected {expected_version}, found {current.version}"
                )

            intended = current.model_copy(
                update={"renewal_status": status, "version": expected_version + 1}
            )
            response = self.transport.update_rows_by_metadata_id(
                spreadsheet_id=self.config.spreadsheet_id,
                metadata_id=metadata_id,
                values=[self._serialize_row(intended)],
            )
            updated_rows = response.get("totalUpdatedRows")
            if updated_rows is not None and int(updated_rows) != 1:
                raise ProviderConflict(f"expected one updated row, provider reported {updated_rows}")

            verified_metadata_id, verified_ref, observed = self._get_exact_renewal_row(renewal_id, metadata_id=metadata_id)
            if verified_metadata_id != metadata_id:
                raise ProviderConflict("post-write metadata identity changed")
            if observed.customer_id != intended.customer_id or observed.renewal_id != intended.renewal_id:
                raise ProviderConflict("post-write identity mismatch")
            if observed.version != intended.version:
                raise ProviderConflict(
                    f"post-write version mismatch: expected {intended.version}, found {observed.version}"
                )
            if observed.renewal_status != intended.renewal_status:
                raise ProviderConflict(
                    f"post-write status mismatch: expected {intended.renewal_status!r}, found {observed.renewal_status!r}"
                )
            # Verify all untouched business-critical fields to detect row corruption or
            # writing to a semantically different row.
            for field in ("company", "contact_email", "renewal_date", "do_not_contact"):
                if getattr(observed, field) != getattr(intended, field):
                    raise ProviderConflict(f"post-write field mismatch: {field}")

            return CRMUpdateResult(
                customer=observed,
                previous_version=expected_version,
                new_version=observed.version,
                provider_ref=verified_ref or provider_ref,
                verified=True,
            )
