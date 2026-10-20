"""A build leaves the analyzed project as cloned (no `.python-version`,
no `uv.lock`), and a pyproject project is installed like `pip install -e`.

Version detection runs with UV_NO_PROJECT=1: without it,
`uv run vermin` syncs the project and writes uv.lock."""

import os
import subprocess
from pathlib import Path

import pytest
from accra_language import AccraResult, Config

from accra_python import Python3


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _python3(project: Path) -> Python3:
    env = {**os.environ, "UV_NO_PROJECT": "1"}
    python3 = Python3(Config(cwd=project, repo="https://fake_repo", env=env))
    assert python3.detect()
    return python3


def _build(project: Path) -> AccraResult:
    return _python3(project).build()


@pytest.mark.integration
def test_requirements_project_gets_no_python_version_file(tmp_path):
    _write(tmp_path, {"requirements.txt": "six\n", "main.py": "import six\n"})

    result = _build(tmp_path)

    assert result.success, [e.message for e in result.errors]
    assert not (tmp_path / ".python-version").exists()


@pytest.mark.integration
def test_pyproject_project_is_installed_without_writing_into_it(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[build-system]\n"
                'requires = ["setuptools>=61"]\n'
                'build-backend = "setuptools.build_meta"\n'
                "\n"
                "[project]\n"
                'name = "demo-cloned"\n'
                'version = "0.1.0"\n'
                'requires-python = ">=3.8"\n'
                'dependencies = ["six"]'
            ),
            "demo_cloned/__init__.py": "VALUE = 42\n",
        },
    )

    python3 = _python3(tmp_path)
    result = python3.build()

    assert result.success, [e.message for e in result.errors]
    # the build only creates the venv and installs the dependencies;
    # install_project() installs the project (pip install -e)
    assert "RUN uv pip install ." not in result.dockerfile.pre_dependencies_installation
    installed = python3.install_project()
    assert installed.success, [e.message for e in installed.errors]
    assert not (tmp_path / "uv.lock").exists()
    assert not (tmp_path / ".python-version").exists()
    assert (
        "RUN python -m pip install --no-cache-dir ."
        in installed.dockerfile.post_dependencies_installation
    )

    imported = subprocess.run(
        [
            str(tmp_path / ".venv/bin/python"),
            "-c",
            "import demo_cloned, six; print(demo_cloned.VALUE)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.stdout.strip() == "42", imported.stderr
