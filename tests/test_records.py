import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from build_icsd import records


def test_structure_import_projection_collects_warning_and_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "broken.cif"
    path.touch()

    class BrokenView:
        def __init__(self, source: Path) -> None:
            assert source == path

        def unview(self) -> None:
            logging.getLogger("httk.test").info("kept info", extra={"context": "cif"})
            logging.getLogger("httk.test").warning("kept warning", extra={"context": "cif"})
            logging.getLogger("httk.test").error("kept error", extra={"context": "cif"})
            raise ValueError("broken input")

    monkeypatch.setattr(records, "ASUStructureView", BrokenView)
    projected = records.StructureImportRecord.__httk_project__(records.StructureImportRequest(path))

    assert projected["structure"] is None
    assert projected["error"] == "broken input"
    assert projected["autocorrect_attempted"] is False
    reports = [json.loads(report) for report in projected["reports"]]
    assert [(report["level"], report["message"]) for report in reports] == [
        ("info", "kept info"),
        ("warning", "kept warning"),
        ("error", "kept error"),
    ]


def test_structure_import_retries_an_advertised_autocorrect(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "repairable.cif"
    path.touch()
    corrected = object()
    load_calls: list[tuple[object, dict[str, object]]] = []

    def fake_load(source: object, **options: object) -> tuple[object, dict[str, object]]:
        load_calls.append((source, options))
        return source, options

    class RepairableView:
        def __init__(self, source: object) -> None:
            self.source = source

        def unview(self) -> object:
            if self.source == path:
                raise ValueError("Remedy: load(..., repair=True)")
            return corrected

    monkeypatch.setattr(records, "ASUStructureView", RepairableView)
    monkeypatch.setattr(records, "load", fake_load)
    monkeypatch.setattr(records, "content_id", lambda *args, **kwargs: "content-id")

    projected = records.StructureImportRecord.__httk_project__(records.StructureImportRequest(path))

    assert projected["structure"] is corrected
    assert projected["error"] is None
    assert projected["autocorrect_attempted"] is True
    assert projected["autocorrected"] is True
    assert load_calls == [(path, {"repair": True})]


def test_structure_import_retains_exact_journal_title(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "journal.cif"
    path.write_text("data_one\n_journal_name_full 'The Organic Letters'\n", encoding="utf-8")
    structure = object()

    class View:
        def __init__(self, source: Path) -> None:
            assert source == path

        def unview(self) -> object:
            return structure

    monkeypatch.setattr(records, "ASUStructureView", View)
    monkeypatch.setattr(records, "primitive_cell", lambda value: SimpleNamespace(structure=SimpleNamespace(sites=[])))
    monkeypatch.setattr(records, "content_id", lambda *args, **kwargs: "content-id")
    projected = records.StructureImportRecord.__httk_project__(records.StructureImportRequest(path))

    assert projected["journal_name"] == "The Organic Letters"
    assert projected["structure"] is structure
    assert projected["error"] is None
    records.StructureImportRecord(**projected)


@pytest.mark.parametrize("site_count, excluded", [(1000, False), (1001, True)])
def test_structure_import_site_limit_is_strict(tmp_path: Path, monkeypatch, site_count: int, excluded: bool) -> None:
    path = tmp_path / "sites.cif"
    path.touch()
    structure = object()

    class View:
        def __init__(self, source: Path) -> None:
            assert source == path

        def unview(self) -> object:
            return structure

    monkeypatch.setattr(records, "ASUStructureView", View)
    monkeypatch.setattr(
        records, "primitive_cell", lambda value: SimpleNamespace(structure=SimpleNamespace(sites=[None] * site_count))
    )
    monkeypatch.setattr(records, "content_id", lambda *args, **kwargs: "content-id")

    projected = records.StructureImportRecord.__httk_project__(records.StructureImportRequest(path))

    assert (projected["error"] is not None) is excluded
    assert (projected["structure"] is not None) is not excluded
    records.StructureImportRecord(**projected)


def test_primitive_site_count_collapses_assembly_alternatives() -> None:
    class Coordinate:
        def __init__(self, values: tuple[int, int, int]) -> None:
            self.values = values

        def to_fractions(self) -> tuple[int, int, int]:
            return self.values

    structure = SimpleNamespace(
        assemblies=(object(),),
        spacegroup=SimpleNamespace(centering_translations=(object(), object())),
        expand_sites=lambda: SimpleNamespace(
            reduced_coords=[
                Coordinate((0, 0, 0)),
                Coordinate((0, 0, 0)),
                Coordinate((1, 1, 1)),
                Coordinate((1, 1, 1)),
                Coordinate((2, 2, 2)),
                Coordinate((3, 3, 3)),
            ]
        ),
    )

    assert records._primitive_site_count(structure) == 2


@pytest.mark.parametrize(
    "value, expected",
    [
        ("_journal_name_full 'The Organic Letters'", "The Organic Letters"),
        ("_journal_name_full\n# comment\n\nOrganometallics", "Organometallics"),
        ("_journal_name_full # comment\n'Organic Letters'", "Organic Letters"),
        ("_journal_name_full\n;\nOrganic &amp; Biomolecular Chemistry\n;", "Organic &amp; Biomolecular Chemistry"),
    ],
)
def test_journal_scanner_accepts_safe_scalar_forms(tmp_path: Path, value: str, expected: str) -> None:
    path = tmp_path / "journal.cif"
    path.write_text(f"data_one\n{value}\n", encoding="utf-8")

    assert records._journal_title(path) == expected


@pytest.mark.parametrize(
    "value",
    [
        "_other_text\n;\n_journal_name_full 'Organic Letters'\n;\n",
        "_other_text\n;\n;not a delimiter\n_journal_name_full 'Organic Letters'\n;\n",
        "_journal_name_full\n  ;\nOrganic Letters\n  ;\n",
        "_journal_name_full 'Organic Letters' trailing\n",
        "_journal_name_full 'Organic Letters\n",
        "_journal_name_full 'Organic Letters'#supplement'\n",
    ],
)
def test_journal_scanner_rejects_uncertain_near_matches(tmp_path: Path, value: str) -> None:
    path = tmp_path / "journal.cif"
    path.write_text(f"data_one\n{value}", encoding="utf-8")

    assert records._journal_exclusion(path) is None


def test_journal_scanner_skips_loop_column_but_reads_later_scalar(tmp_path: Path) -> None:
    path = tmp_path / "journal.cif"
    path.write_text(
        "data_one\n"
        "loop_\n"
        "_journal_name_full\n"
        "_other_column\n"
        "'Organic Letters' value\n"
        "_journal_name_full 'Organometallics'\n",
        encoding="utf-8",
    )

    assert records._journal_exclusion(path) == "journal blacklist: Organometallics"


@pytest.mark.parametrize("fails", [False, True])
def test_import_keeps_distinct_archive_and_collection_ids(tmp_path: Path, monkeypatch, fails: bool) -> None:
    path = tmp_path / "2.cif"
    path.write_text("data_example\n_database_code_ICSD 5\n", encoding="utf-8")
    structure = object()

    class View:
        def __init__(self, source: Path) -> None:
            assert source == path

        def unview(self) -> object:
            if fails:
                raise ValueError("invalid structure")
            return structure

    monkeypatch.setattr(records, "ASUStructureView", View)
    monkeypatch.setattr(records, "content_id", lambda *args, **kwargs: "content-id")
    projected = records.StructureImportRecord.__httk_project__(
        records.StructureImportRequest(path, filter_enabled=False)
    )

    assert projected["archive_id"] == "2"
    assert projected["icsd_code"] == "5"
    assert projected["error"] == ("invalid structure" if fails else None)
    assert projected["structure"] is (None if fails else structure)
    records.StructureImportRecord(**projected)


@pytest.mark.parametrize("code", ["?", ".", "'not a code'"])
def test_import_unknown_identifiers_remain_absent(tmp_path: Path, code: str) -> None:
    path = tmp_path / "unknown.cif"
    path.write_text(f"data_example\n_database_code_ICSD {code}\n", encoding="utf-8")

    result = records._read_structure(records.StructureImportRequest(path))

    assert result.archive_id is None
    assert result.icsd_code is None
    assert result.error is not None


def test_journal_reads_primary_icsd_citation_with_multiline_text(tmp_path: Path) -> None:
    path = tmp_path / "citation.cif"
    path.write_text(
        "data_example\n"
        "loop_\n_citation_id\n_citation_journal_full\n_citation_year\n"
        "secondary 'A Different Journal' 2001\n"
        "primary\n;\nOrganic &amp;\nBiomolecular Chemistry\n; 2002\n",
        encoding="utf-8",
    )

    assert records._journal_title(path) == "Organic &amp;\nBiomolecular Chemistry"
    assert records._journal_exclusion(path) == "journal blacklist: Organic &amp;\nBiomolecular Chemistry"


@pytest.mark.parametrize(
    "citations",
    [
        "secondary 'Organic Letters' 2000\n",
        "primary 'Organic Letters' 2000\nprimary 'Organometallics' 2001\n",
        "primary 'Organic Letters'\n",
        "primary ? 2000\n",
    ],
)
def test_uncertain_icsd_citation_never_filters(tmp_path: Path, citations: str) -> None:
    path = tmp_path / "citation.cif"
    path.write_text(
        "data_example\nloop_\n_citation_id\n_citation_journal_full\n_citation_year\n" + citations,
        encoding="utf-8",
    )

    assert records._journal_exclusion(path) is None


def test_metadata_diagnostics_are_collected_with_import_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "2.cif"
    path.write_text("data_example\n_database_code_ICSD 5\n", encoding="utf-8")

    def failed_metadata(source: Path):
        assert source == path
        logging.getLogger("httk.test").warning("uncertain citation metadata")
        raise ValueError("metadata parse failed")

    monkeypatch.setattr(records, "read_cif", failed_metadata)
    result = records._read_structure(records.StructureImportRequest(path))

    assert result.archive_id == "2"
    assert result.icsd_code == "5"
    assert result.journal_name is None
    assert result.error is not None
    assert any(json.loads(report)["message"] == "uncertain citation metadata" for report in result.reports)
