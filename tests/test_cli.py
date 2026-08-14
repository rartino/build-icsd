import sqlite3
from pathlib import Path

from build_cod.cli import main


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
    output = tmp_path / "cod.sqlite"
    monkeypatch.setenv("COD_PATH", str(cod))

    assert main(["--output", str(output), "--workers", "2", "--progress-every", "1"]) == 0

    assert output.is_file()
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT COUNT(*) FROM atomistic_asu_structure_v3 WHERE _httk_role = 1"
        ).fetchone() == (1,)
    captured = capsys.readouterr().out
    assert "Submitted/queued 1/1 structures" in captured
    assert "Completed 1/1 structures" in captured
