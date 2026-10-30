"""What already imports is not installed, and the import name maps to the
install name."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from accra_language import Config, Dependency

from accra_python import Uv
from accra_python.adhoc import Adhoc
from accra_python.import_names import IMPORT_NAMES


def _venv(project: Path) -> None:
    subprocess.run(["uv", "venv", "--seed", "-q", str(project / ".venv")], check=True)


def _recording_uv(project: Path, monkeypatch) -> tuple[Uv, list[list[str]]]:
    _venv(project)
    uv = Uv(Config(cwd=project))
    commands: list[list[str]] = []

    def fake_run(command):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uv, "_run", fake_run)
    return uv, commands


@pytest.mark.integration
def test_namespace_dir_is_not_replaced_by_the_pypi_package(tmp_path, monkeypatch):
    (tmp_path / "plot").mkdir()
    (tmp_path / "plot" / "plot_rank_a.py").write_text("def main():\n    pass\n")
    (tmp_path / "run_figures.py").write_text("from plot.plot_rank_a import main\n")

    # the inference names `plot` (no requirements, no manifest)
    adhoc = Adhoc(Config(cwd=tmp_path))
    inferred = adhoc.extract_dependencies()
    assert inferred is not None

    uv, commands = _recording_uv(tmp_path, monkeypatch)
    result = uv._install_dependency(Dependency(name="plot", version=""))
    assert result.success and not result.errors
    assert commands == []
    assert not result.dockerfile.dependencies_installation


@pytest.mark.integration
def test_constrained_spec_that_imports_is_skipped_like_main(tmp_path, monkeypatch):
    # only the base name is checked: the version is not looked at
    (tmp_path / "tokenizer").mkdir()
    uv, commands = _recording_uv(tmp_path, monkeypatch)
    result = uv._install_dependency(Dependency(name="tokenizer", version=">=1.0"))
    assert result.success and commands == []


@pytest.mark.integration
def test_dependency_that_does_not_import_is_installed(tmp_path, monkeypatch):
    uv, commands = _recording_uv(tmp_path, monkeypatch)
    uv._install_dependency(Dependency(name="not_a_real_module_xyz", version=""))
    assert len(commands) == 1
    assert commands[0][:3] == ["uv", "pip", "install"]
    assert commands[0][-1] == "not_a_real_module_xyz"


@pytest.mark.integration
def test_location_spec_is_not_checked(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    uv, commands = _recording_uv(tmp_path, monkeypatch)
    uv._install_dependency(
        Dependency(name="pkg @ https://example.org/p.zip", version="")
    )
    assert len(commands) == 1


@pytest.mark.integration
def test_installed_package_imports_under_another_name(tmp_path, monkeypatch):
    # the distribution is `pip` here: already in the seeded venv, so not reinstalled
    uv, commands = _recording_uv(tmp_path, monkeypatch)
    uv._install_dependency(Dependency(name="pip", version=""))
    assert commands == []


@pytest.mark.parametrize(
    ("module", "package"),
    [
        ("absl", "absl-py"),
        ("docx", "python-docx"),
        ("sklearn", "scikit-learn"),
        ("cv2", "opencv-python"),
        ("yaml", "PyYAML"),
        ("PIL", "Pillow"),
        ("bs4", "beautifulsoup4"),
        ("absl.flags", "absl-py"),
        ("numpy.linalg", "numpy"),
        ("some_unknown_thing", "some_unknown_thing"),
    ],
)
def test_import_name_resolves_to_install_name(module, package):
    assert Adhoc._resolve_package(module) == package


def test_sample_of_mains_facts_resolve():
    for module, package in (
        ("community", "python-louvain"),
        ("gprofiler", "gprofiler-official"),
        ("antlr4", "antlr4-python3-runtime"),
        ("umap", "umap-learn"),
        ("google.generativeai", "google-generativeai"),
    ):
        assert IMPORT_NAMES[module] == package
        assert Adhoc._resolve_package(module) == package
    assert len(IMPORT_NAMES) > 150
    assert list(IMPORT_NAMES) == sorted(IMPORT_NAMES, key=lambda k: (k.lower(), k))


def test_mapped_name_is_the_inferred_dependency(tmp_path):
    (tmp_path / "a.py").write_text("import absl\nimport docx\nimport numpy\n")
    inferred = Adhoc(Config(cwd=tmp_path)).extract_dependencies()
    assert inferred is not None
    names = {d.name for d in inferred}
    assert {"absl-py", "python-docx"} <= names
    assert "absl" not in names and "docx" not in names
