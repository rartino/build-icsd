import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import build_cod.cli as cli_module
from build_cod.cli import _emit_result_reports, _requests, main
from build_cod.records import StructureImportRequest
from serve_cod_optimade.cli import main as serve_main


def test_build_cod_cli_with_cod_path_fallback(tmp_path: Path, monkeypatch, capsys) -> None:
    cod = tmp_path / "COD"
    cif = cod / "cif" / "1.cif"
    cif.parent.mkdir(parents=True)
    cif.write_text(
        """data_one
_cell_length_a 1
_cell_length_b 1
_cell_length_c 1
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_IT_number 1
_space_group_name_H-M_alt 'P 1'
loop_
_space_group_symop_operation_xyz
'x,y,z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
C1 C 0 0 0
""",
        encoding="utf-8",
    )
    broken = cod / "cif" / "2.cif"
    broken.write_text(
        """data_two
_cell_length_a 1
_cell_length_b 1
_cell_length_c 1
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_IT_number 1
loop_
_atom_site_label
C1
""",
        encoding="utf-8",
    )
    output = tmp_path / "cod.sqlite"
    monkeypatch.setenv("COD_PATH", str(cod))

    emitted = 0
    original_emit = cli_module._emit_result_reports

    def interrupt_after_first(result) -> None:
        nonlocal emitted
        original_emit(result)
        emitted += 1
        if emitted == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_emit_result_reports", interrupt_after_first)
    with pytest.raises(KeyboardInterrupt):
        main(
            [
                "--format",
                "sqlite",
                "--output",
                str(output),
                "--workers",
                "2",
                "--progress-every",
                "1",
                "--commit-every",
                "1",
            ]
        )

    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cod_structure_import").fetchone() == (1,)

    monkeypatch.setattr(cli_module, "_emit_result_reports", original_emit)
    assert (
        main(
            [
                "--format",
                "sqlite",
                "--output",
                str(output),
                "--workers",
                "2",
                "--progress-every",
                "1",
                "--commit-every",
                "1",
            ]
        )
        == 0
    )

    assert output.is_file()
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM atomistic_asu_structure WHERE _httk_role = 1").fetchone() == (
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM cod_structure_import").fetchone() == (2,)
        error = connection.execute("SELECT error FROM cod_structure_import WHERE error IS NOT NULL").fetchone()[0]
        assert "this CIF holds no structure that could be interpreted" in error
        assert "_atom_site_fract_x, _atom_site_fract_y, _atom_site_fract_z" in error
    captured = capsys.readouterr()
    assert "Committed 2/2 CIF imports" in captured.out
    assert "Completed 2/2 CIF imports" in captured.out
    assert "2.cif: ERROR:" in captured.err

    responses = []

    def run_dev_server(*, app, host: str, port: int) -> None:
        with TestClient(app, base_url=f"http://{host}:{port}") as client:
            responses.append(client.get("/v1/structures"))

    monkeypatch.setattr("httk.serve.optimade.api.run_dev_server", run_dev_server)
    assert serve_main(["--database", str(output), "--port", "8123"]) == 0
    assert responses[0].status_code == 200
    assert responses[0].json()["meta"]["data_available"] == 1


def test_build_cod_cli_no_filter_reaches_worker_request(tmp_path: Path) -> None:
    cod = tmp_path / "COD" / "cif"
    cod.mkdir(parents=True)
    path = cod / "1.cif"
    path.touch()
    assert list(_requests((path,), False)) == [(None, StructureImportRequest(path, filter_enabled=False))]


def test_diagnostics_are_stderr_with_recorded_levels(capsys) -> None:
    result = SimpleNamespace(
        source="broken.cif",
        reports=(
            json.dumps({"level": "info", "message": "details"}),
            json.dumps({"level": "warning", "message": "careful"}),
            json.dumps({"level": "error", "message": "bad metadata"}),
        ),
        error="builtins.ValueError: broken input",
    )
    _emit_result_reports(result)
    expected = (
        "broken.cif: INFO: details\nbroken.cif: WARNING: careful\n"
        + "broken.cif: ERROR: bad metadata\nbroken.cif: ERROR: builtins.ValueError: broken input\n"
    )
    assert capsys.readouterr() == (
        "",
        expected,
    )

    _emit_result_reports(SimpleNamespace(source="excluded.cif", reports=(), error="excluded: journal blacklist"))
    assert capsys.readouterr().err == "excluded.cif: INFO: excluded: journal blacklist\n"
