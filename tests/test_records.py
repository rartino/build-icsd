import json
import logging
from pathlib import Path

from build_cod import records


def test_structure_import_projection_collects_warning_and_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "broken.cif"
    path.touch()

    class BrokenView:
        def __init__(self, source: Path) -> None:
            assert source == path

        def unview(self) -> None:
            logging.getLogger("httk.test").warning("kept warning", extra={"context": "cif"})
            raise ValueError("broken input")

    monkeypatch.setattr(records, "ASUStructureView", BrokenView)
    projected = records.StructureImportRecord.__httk_project__(path)

    assert projected["structure"] is None
    assert projected["error"] == "builtins.ValueError: broken input"
    assert projected["autocorrect_attempted"] is False
    report = json.loads(projected["reports"][0])
    assert report == {"context": "cif", "level": "warning", "logger": "httk.test", "message": "kept warning"}


def test_structure_import_retries_an_advertised_autocorrect(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "repairable.cif"
    path.touch()
    corrected = object()

    class RepairableView:
        def __init__(self, source: object) -> None:
            self.source = source

        def unview(self) -> object:
            if self.source == path:
                raise ValueError("Remedy: load(..., autocorrect=True)")
            return corrected

    monkeypatch.setattr(records, "ASUStructureView", RepairableView)
    monkeypatch.setattr(records, "load", lambda source, **options: (source, options))
    monkeypatch.setattr(records, "content_id", lambda *args, **kwargs: "content-id")

    projected = records.StructureImportRecord.__httk_project__(path)

    assert projected["structure"] is corrected
    assert projected["error"] is None
    assert projected["autocorrect_attempted"] is True
    assert projected["autocorrected"] is True
