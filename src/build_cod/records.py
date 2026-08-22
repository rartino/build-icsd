"""Storage record for one resilient COD structure import."""

import html
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from httk.atomistic import ASUStructure, ASUStructureRecord, ASUStructureView, primitive_cell
from httk.core import load
from httk.core.report import JsonFormatter, collect_reports
from httk.core.storage import StorageInfo, content_id

_REPORT_FORMATTER = JsonFormatter()
_MAX_PRIMITIVE_SITES = 1000
_FILTER_JOURNALS = {
    "organic letters",
    "organometallics",
    "organic & biomolecular chemistry",
    "journal of organic chemistry",
}


@dataclass(frozen=True)
class StructureImportRequest:
    """The source and filter setting sent to a pass-1 worker."""

    path: Path
    filter_enabled: bool = True


def _normalize_journal(value: str) -> str:
    normalized = re.sub(r"\s+", " ", html.unescape(value)).strip().casefold()
    normalized = normalized.removeprefix("the ")
    return normalized


def _cif_scalar(value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith(";"):
        return None
    if value[0] in "'\"":
        quote = value[0]
        for index in range(1, len(value)):
            if value[index] != quote:
                continue
            tail = value[index + 1 :]
            if not tail or tail[0].isspace():
                if not tail.strip() or tail.lstrip().startswith("#"):
                    return value[1:index]
                return None
        return None
    token = value.split(None, 1)[0]
    tail = value[len(token) :].strip()
    if tail and not tail.startswith("#"):
        return None
    return token


def _cif_semicolon_value(first_line: str, lines) -> str | None:
    first = first_line[1:].rstrip("\r\n")
    parts = [] if not first else [first]
    for continuation in lines:
        if continuation.startswith(";") and not continuation[1:].strip():
            return "\n".join(parts)
        parts.append(continuation.rstrip("\r\n"))
    return None


def _journal_title(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="strict") as stream:
            lines = iter(stream)
            loop_headers = False
            for line in lines:
                if line.startswith(";"):
                    if _cif_semicolon_value(line, lines) is None:
                        return None
                    continue
                tokens = line.lstrip().split(None, 1)
                if not tokens or tokens[0].startswith("#"):
                    continue
                token = tokens[0].casefold()
                if loop_headers:
                    if token.startswith("_"):
                        continue
                    loop_headers = False
                if token == "loop_":
                    loop_headers = True
                    continue
                if token != "_journal_name_full":
                    continue
                if len(tokens) == 2 and not tokens[1].lstrip().startswith("#"):
                    return _cif_scalar(tokens[1])
                for value_line in lines:
                    if not value_line.strip() or value_line.lstrip().startswith("#"):
                        continue
                    if value_line.startswith(";"):
                        return _cif_semicolon_value(value_line, lines)
                    if value_line.lstrip().startswith(("_", "data_", "loop_")):
                        return None
                    return _cif_scalar(value_line)
                return None
    except Exception:  # noqa: BLE001 - uncertain metadata must never exclude a structure
        return None
    return None


def _journal_exclusion(path: Path) -> str | None:
    title = _journal_title(path)
    if title is not None and _normalize_journal(title) in _FILTER_JOURNALS:
        return f"journal blacklist: {title}"
    return None


def _site_exclusion(structure: ASUStructure, path: Path) -> str | None:
    try:
        count = len(primitive_cell(structure).structure.sites)
    except Exception as error:  # noqa: BLE001 - inability to size must retain the structure
        logging.getLogger(__name__).warning(
            "could not determine primitive site count for %s; retaining structure: %s",
            path,
            error,
            extra={"context": "cod-filter"},
        )
        return None
    if count > _MAX_PRIMITIVE_SITES:
        return f"primitive site count {count} exceeds limit {_MAX_PRIMITIVE_SITES}"
    return None


def _report_json(record: logging.LogRecord) -> str:
    payload = json.loads(_REPORT_FORMATTER.format(record))
    payload.pop("ts", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _error_text(error: Exception) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}: {error}"


@dataclass(frozen=True)
class StructureImportRecord:
    """Record one source path and its imported or failed ASU."""

    __httk_storage__: ClassVar[StorageInfo] = StorageInfo(
        storage_name="cod_structure_import",
        identity_name="cod_structure_import",
        indexes=(("source",), ("autocorrected",)),
    )
    __httk_canonical_source__: ClassVar[type[StructureImportRequest]] = StructureImportRequest

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
    def __httk_project__(cls, request: StructureImportRequest) -> Mapping[str, object]:
        """Read one path inside its bulk worker and retain warnings or failure."""
        path = request.path
        structure = None
        error_text = None
        exclusion_reason = _journal_exclusion(path) if request.filter_enabled else None
        autocorrect_attempted = False
        autocorrected = False
        with collect_reports(level="warning") as reports:
            try:
                if exclusion_reason is None:
                    structure = ASUStructureView(path).unview()
                    if request.filter_enabled:
                        exclusion_reason = _site_exclusion(structure, path)
                    if exclusion_reason is None:
                        content_id(structure, as_record=ASUStructureRecord)
                    else:
                        structure = None
                        error_text = f"excluded: {exclusion_reason}"
                else:
                    error_text = f"excluded: {exclusion_reason}"
            except Exception as error:  # noqa: BLE001 - one bad external file must become a record, not abort the build
                if isinstance(error, ValueError) and "repair=True" in str(error):
                    autocorrect_attempted = True
                    try:
                        structure = ASUStructureView(load(path, repair=True)).unview()
                        if request.filter_enabled:
                            exclusion_reason = _site_exclusion(structure, path)
                        if exclusion_reason is None:
                            content_id(structure, as_record=ASUStructureRecord)
                        else:
                            structure = None
                            error_text = f"excluded: {exclusion_reason}"
                    except Exception as autocorrect_error:  # noqa: BLE001 - retain the final per-file failure
                        structure = None
                        exclusion_reason = None
                        error_text = _error_text(autocorrect_error)
                    else:
                        autocorrected = structure is not None
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


@dataclass(frozen=True)
class CanonicalizationRecord:
    """Record the pass-2 canonicalization outcome for one imported structure.

    Exactly one of ``canonical_content_id`` (success) or ``error`` (failure) is set.
    ``source`` is the importing ``cod_structure_import`` row's path and is the resume
    key: pass 2 processes import rows whose ``source`` has no canonicalization row yet.
    The content-id columns are loose references (the same layout-independent content
    identities used by the searcher and OPTIMADE serving) to the original and canonical
    structures, the derived prototype and protostructure, and the provenance run.

    A ``--retry-errors`` retry supersedes an error row with ``store.replace``, which keeps the
    old row queryable, so after retries the table holds superseded lineage rows. Raw
    ``COUNT(*)`` therefore over-counts; the current-state "canonicalized imports" count is
    ``SELECT COUNT(DISTINCT source) WHERE error IS NULL`` (one success per source, retry-proof).

    :param source: The importing row's source path (the resume/link key).
    :param original_content_id: The content id of the imported (pre-canonicalization) structure.
    :param canonical_content_id: The content id of the canonical structure, or ``None`` on failure.
    :param prototype_content_id: The content id of the derived prototype, or ``None`` on failure.
    :param protostructure_content_id: The content id of the derived protostructure, or ``None`` on failure.
    :param run_content_id: The content id of the provenance run, or ``None`` on failure.
    :param error: The per-structure failure text, or ``None`` on success.
    :param lift: Whether higher-pseudosymmetry lifting was requested for this structure.
    """

    __httk_storage__: ClassVar[StorageInfo] = StorageInfo(
        storage_name="cod_canonicalization",
        identity_name="cod_canonicalization",
        indexes=(("source",), ("protostructure_content_id",)),
    )

    source: str
    original_content_id: str
    canonical_content_id: str | None
    prototype_content_id: str | None
    protostructure_content_id: str | None
    run_content_id: str | None
    error: str | None
    lift: bool

    @property
    def id(self) -> str:
        """Return the layout-independent content identity of this record."""
        return content_id(self)

    def __post_init__(self) -> None:
        for name in ("source", "original_content_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"CanonicalizationRecord {name} must be a non-empty string")
        if not isinstance(self.lift, bool):
            raise TypeError("CanonicalizationRecord lift must be a bool")
        if (self.canonical_content_id is None) == (self.error is None):
            raise ValueError("CanonicalizationRecord requires exactly one of canonical_content_id or error")
        derived = (self.prototype_content_id, self.protostructure_content_id, self.run_content_id)
        if self.error is not None and any(value is not None for value in derived):
            raise ValueError("a failed canonicalization must not carry derived references")
        if self.canonical_content_id is not None and any(value is None for value in derived):
            raise ValueError("a successful canonicalization requires prototype, protostructure, and run references")
