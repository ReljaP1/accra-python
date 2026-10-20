"""For Python: the project install, its names and covered
manifests, the run environment, the installed-patch choice, and read-only
version detection."""

import subprocess
from pathlib import Path

import pytest
from accra_language import Config

from accra_python import Python3, Uv, utils


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _venv(project: Path) -> None:
    subprocess.run(["uv", "venv", "--seed", "-q", str(project / ".venv")], check=True)


SETUP_PY = "from setuptools import setup\nsetup(name='demo-setup', version='0.1', packages=['demo_setup'])\n"


@pytest.mark.integration
def test_setup_py_project_is_installed_editable(tmp_path):
    _write(tmp_path, {"setup.py": SETUP_PY, "demo_setup/__init__.py": "VALUE = 7\n"})
    _venv(tmp_path)
    uv = Uv(Config(cwd=tmp_path))
    assert uv.has_project_install() is True

    result = uv.install_project()

    assert result.success, [e.message for e in result.errors]
    assert result.dockerfile.post_dependencies_installation == [
        "RUN python -m pip install --no-cache-dir ."
    ]
    imported = subprocess.run(
        [
            str(tmp_path / ".venv/bin/python"),
            "-c",
            "import demo_setup; print(demo_setup.VALUE)",
        ],
        capture_output=True,
        text=True,
        cwd="/",
        check=False,
    )
    assert imported.stdout.strip() == "7", imported.stderr
    assert {"demo-setup"} & uv.project_names()
    assert "demo-setup" in uv._editable_names()


def test_project_without_install_step_runs_from_source(tmp_path):
    _write(
        tmp_path, {"main.py": "print(1)\n", "tools/run.py": "", "pkg/__init__.py": ""}
    )
    uv = Uv(Config(cwd=tmp_path, image_workdir="/srv/app"))
    assert uv.has_project_install() is False

    result = uv.install_project()

    assert result.success
    assert result.dockerfile.post_dependencies_installation == [
        "ENV PYTHONPATH=/srv/app:/srv/app/tools"
    ]


@pytest.mark.integration
def test_failing_project_install_falls_back_to_source(tmp_path):
    _write(tmp_path, {"setup.py": "raise SystemExit('broken setup')\n", "lib.py": ""})
    _venv(tmp_path)
    uv = Uv(Config(cwd=tmp_path))

    result = uv.install_project()

    assert not result.success
    assert "pip could not install the project" in result.errors[-1].message
    assert (
        result.dockerfile.post_dependencies_installation[-1]
        == "ENV PYTHONPATH=/project"
    )


def test_run_environment_puts_the_import_roots_first(tmp_path):
    _write(tmp_path, {"scripts/a.py": "", "pkg/__init__.py": ""})
    env = Uv(Config(cwd=tmp_path)).run_environment({"PYTHONPATH": "/x", "PATH": "/bin"})
    root = tmp_path.resolve()
    assert env["PYTHONPATH"] == f"{root}:{root / 'scripts'}:/x"


def test_maturin_build_covers_its_cargo_manifest(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": '[build-system]\nrequires = ["maturin>=1"]\nbuild-backend = "maturin"\n',
            "Cargo.toml": "[package]\nname = 'ext'\n",
        },
    )
    assert Python3(Config(cwd=tmp_path)).covered_manifests() == {
        tmp_path / "Cargo.toml"
    }

    plain = tmp_path / "plain"
    _write(plain, {"pyproject.toml": "[project]\nname = 'x'\n", "Cargo.toml": ""})
    assert Python3(Config(cwd=plain)).covered_manifests() == set()


def test_installed_patch_resolution(tmp_path, monkeypatch):
    listed = "2.7.18\n3.11.2\n3.11.12\n3.12.10\n3.11.4/envs/x\n"
    monkeypatch.setattr(
        utils,
        "run",
        lambda command, config, timeout=None, **kw: subprocess.CompletedProcess(
            command, 0, listed, ""
        ),
    )
    config = Config(cwd=tmp_path)
    assert utils.installed_patch("3.11", config) == "3.11.12"
    assert utils.installed_patch("3.11.2", config) == "3.11.2"
    assert utils.installed_patch("3.9", config) == "3.9"


def test_version_detection_leaves_a_pyproject_project_untouched(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.8"\ndependencies = ["six"]\n',
            "demo.py": "import six\n",
        },
    )
    before = sorted(p.name for p in tmp_path.iterdir())

    assert Python3(Config(cwd=tmp_path)).detect() is True

    assert sorted(p.name for p in tmp_path.iterdir()) == before
