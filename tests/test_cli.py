import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from build_cod.cli import main
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

    assert main(["--output", str(output), "--workers", "2", "--progress-every", "1"]) == 0

    assert output.is_file()
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT COUNT(*) FROM atomistic_asu_structure_v3 WHERE _httk_role = 1"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM cod_structure_import_v1").fetchone() == (2,)
        error = connection.execute("SELECT error FROM cod_structure_import_v1 WHERE error IS NOT NULL").fetchone()[0]
        assert "this CIF holds no structure that could be interpreted" in error
        assert "_atom_site_fract_x, _atom_site_fract_y, _atom_site_fract_z" in error
    captured = capsys.readouterr().out
    assert "Submitted/queued 2/2 CIF imports" in captured
    assert "Completed 2/2 CIF imports" in captured

    responses = []

    def run_dev_server(*, app, host: str, port: int) -> None:
        with TestClient(app, base_url=f"http://{host}:{port}") as client:
            responses.append(client.get("/v1/structures"))

    monkeypatch.setattr("httk.serve.optimade.api.run_dev_server", run_dev_server)
    assert serve_main(["--database", str(output), "--port", "8123"]) == 0
    assert responses[0].status_code == 200
    assert responses[0].json()["meta"]["data_available"] == 1
