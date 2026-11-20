import importlib.util
import shutil
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "export_requirements.py"
pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="需要 uv")


def _load():
    spec = importlib.util.spec_from_file_location("export_requirements", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_requirements_match_lock() -> None:
    assert _load().main(["--check"]) == 0


def test_check_reports_stale_lists_and_export_rewrites_them(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _load()
    monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path / "requirements")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "export", lambda extra: f"# {extra}\npinned==1.0\n")

    assert module.main(["--check"]) == 1
    assert "requirements/asr.txt" in capsys.readouterr().err
    assert not (tmp_path / "requirements").exists()

    assert module.main([]) == 0
    assert (tmp_path / "requirements" / "restore.txt").read_text() == "# restore\npinned==1.0\n"
    assert module.main(["--check"]) == 0
