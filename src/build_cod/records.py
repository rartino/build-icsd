"""Storage record for one resilient COD structure import."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from httk.atomistic import ASUStructureRecord, ASUStructureView
from httk.core import load
from httk.core.report import JsonFormatter, collect_reports
from httk.core.storage import StorageInfo, content_id

_REPORT_FORMATTER = JsonFormatter()


def _report_json(record: logging.LogRecord) -> str:
    payload = json.loads(_REPORT_FORMATTER.format(record))
    payload.pop("ts", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _error_text(error: Exception) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}: {error}"


@dataclass(frozen=True)
class StructureImportRecord:
    """Record one source path and either its imported ASU or its failure."""

    __httk_storage__: ClassVar[StorageInfo] = StorageInfo(
        storage_name="cod_structure_import",
        identity_name="cod_structure_import",
        indexes=(("source",), ("autocorrected",)),
    )
    __httk_canonical_source__: ClassVar[type[Path]] = Path

    source: str
    structure: ASUStructureRecord | None
    reports: tuple[str, ...]
    error: str | None
    autocorrect_attempted: bool
    autocorrected: bool

    def __post_init__(self) -> None:
        reports = tuple(self.reports)
        if not all(isinstance(report, str) for report in reports):
            raise TypeError("StructureImportRecord reports must contain JSON strings")
        if (self.structure is None) == (self.error is None):
            raise ValueError("StructureImportRecord requires exactly one of structure or error")
        if self.autocorrected and (not self.autocorrect_attempted or self.structure is None):
            raise ValueError("a corrected import requires an attempted autocorrect and a structure")
        object.__setattr__(self, "reports", reports)

    @classmethod
    def __httk_project__(cls, path: Path) -> Mapping[str, object]:
        """Read one path inside its bulk worker and retain warnings or failure."""
        structure = None
        error_text = None
        autocorrect_attempted = False
        autocorrected = False
        with collect_reports(level="warning") as reports:
            try:
                structure = ASUStructureView(path).unview()
                content_id(structure, as_record=ASUStructureRecord)
            except Exception as error:  # noqa: BLE001 - one bad external file must become a record, not abort the build
                if isinstance(error, ValueError) and "autocorrect=True" in str(error):
                    autocorrect_attempted = True
                    try:
                        structure = ASUStructureView(load(path, autocorrect=True)).unview()
                        content_id(structure, as_record=ASUStructureRecord)
                    except Exception as autocorrect_error:  # noqa: BLE001 - retain the final per-file failure
                        structure = None
                        error_text = _error_text(autocorrect_error)
                    else:
                        autocorrected = True
                else:
                    error_text = _error_text(error)
        return {
            "source": str(path),
            "structure": structure,
            "reports": tuple(_report_json(record) for record in reports.records),
            "error": error_text,
            "autocorrect_attempted": autocorrect_attempted,
            "autocorrected": autocorrected,
        }
