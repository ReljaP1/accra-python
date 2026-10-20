"""Dependency parsing, the project's own names, version detection and installs."""

import subprocess
from pathlib import Path

import pytest
from accra_language import AccraError, Config, Dependency

from accra_python import Adhoc, Pyproject_toml, Requirements_txt, Uv, Venv, utils
from accra_python.python_environment import PythonEnvironment
from accra_python.utils import project_own_names, requirement_dependency


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_requirement_dependency_keeps_specifier_extras_and_marker():
    assert requirement_dependency("numpy>=1.2,<2") == Dependency(
        name="numpy", version="<2,>=1.2"
    )
    assert requirement_dependency("requests[socks]==2.31") == Dependency(
        name="requests[socks]", version="==2.31"
    )

    marked = requirement_dependency('futures; python_version < "3.0"')
    assert marked is not None
    assert marked.name == "futures"
    assert marked.version == '; python_version < "3.0"'

    url = "pkg @ https://example.com/pkg-1.0.tar.gz"
    assert requirement_dependency(url) == Dependency(name=url, version="")

    assert requirement_dependency("not a requirement !") is None


def test_requirements_txt_markers_comments_options_and_nested(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": (
                "--index-url https://example.com/simple\n"
                "-e .\n"
                "-c constraints.txt\n"
                "-rreq/base.txt\n"
                'numpy>=1.2; python_version >= "3.8"  # comment\n'
                "git+https://github.com/o/r.git@v1#egg=r\n"
            ),
            "req/base.txt": "pandas\n",
        },
    )

    deps = Requirements_txt(Config(cwd=tmp_path)).extract_dependencies()

    assert deps == {
        Dependency(name="numpy", version='>=1.2; python_version >= "3.8"'),
        Dependency(name="pandas", version=""),
        Dependency(name="git+https://github.com/o/r.git@v1#egg=r", version=""),
    }


def test_pyproject_keeps_specifiers(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "demo"\ndependencies = ["numpy>=1.2", "rich"]'
            )
        },
    )

    manifest = Pyproject_toml(Config(cwd=tmp_path))
    assert manifest.detect()

    assert manifest.extract_dependencies() == {
        Dependency(name="numpy", version=">=1.2"),
        Dependency(name="rich", version=""),
    }


def test_adhoc_keeps_packages_of_a_file_importing_stdlib(tmp_path):
    _write(tmp_path, {"main.py": "import os\nimport yaml\n"})

    deps = Adhoc(Config(cwd=tmp_path)).extract_dependencies()

    assert deps == {Dependency(name="PyYAML", version="")}


def test_project_own_names(tmp_path):
    _write(
        tmp_path,
        {
            "main.py": "",
            "setup.py": "from setuptools import setup\nsetup(name='demo-dist')\n",
            "toolbox/__init__.py": "",
            "src/inner_pkg/__init__.py": "",
            "data/notes.txt": "",
        },
    )

    assert project_own_names(tmp_path) == {
        "main",
        "toolbox",
        "inner-pkg",
        "demo-dist",
    }


def test_uv_skips_the_projects_own_names(tmp_path):
    # local modules and the project's own distribution never become
    # dependencies (dependency confusion, uv self-dependency errors)
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\n"
                'name = "Demo_Project"\n'
                'dependencies = ["demo-project", "rich"]'
            ),
            "main.py": "import toolbox\nimport demo_project\nimport requests\n",
            "toolbox/__init__.py": "",
            "src/demo_project/__init__.py": "",
        },
    )

    for env_manager in (Uv, Venv):
        names = {
            dep.name for dep in env_manager(Config(cwd=tmp_path))._get_dependencies()
        }

        assert names == {"rich", "requests"}, env_manager.__name__


def test_failed_pyenv_install_stops_the_build(tmp_path, monkeypatch):
    uv = Uv(Config(cwd=tmp_path))
    uv.selected_language_version = "3.11"
    monkeypatch.setattr(
        utils,
        "run",
        lambda command, config, timeout=None: subprocess.CompletedProcess(
            command, 1, "", "pyenv: no such version"
        ),
    )

    result = uv.pre_dependencies_installation()

    assert not result.success
    assert "pyenv could not install python version: 3.11" in result.errors[0].message
    assert "pyenv: no such version" in result.errors[0].message


def test_uv_drops_a_bare_name_when_a_manifest_constrains_it(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": "httpx>=0.28\n",
            "main.py": "import httpx\nimport rich\n",
        },
    )

    deps = Uv(Config(cwd=tmp_path))._get_dependencies()

    assert deps == {
        Dependency(name="httpx", version=">=0.28"),
        Dependency(name="rich", version=""),
    }


def test_version_universe_is_configurable(tmp_path):
    # the caller names the interpreters it has
    config = Config(
        cwd=tmp_path, env={"ACCRA_PYTHON_VERSIONS": "3.8, 3.12,3.13,2.7,3.4,x"}
    )
    assert utils.get_python_minor_versions(">=3.9", config) == {"3.12", "3.13"}
    assert utils.get_python_minor_versions(config=config) == {"3.8", "3.12", "3.13"}

    default = utils.get_python_minor_versions(config=Config(cwd=tmp_path, env={}))
    assert default == {f"3.{i}" for i in range(5, 15)}

    manifest = Requirements_txt(config)
    assert manifest.get_supported_language_versions() == {"3.8", "3.12", "3.13"}


def _fake_run(calls, returncode=0, stdout="", stderr=""):
    def run(command, config, timeout=None, **_kwargs):
        calls.append((list(command), timeout))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return run


def test_adhoc_without_python_source_does_not_limit_the_version(tmp_path, monkeypatch):
    # vermin exits 1 with "No files specified to analyze!"
    calls = []
    monkeypatch.setattr(
        utils, "run", _fake_run(calls, 1, "No files specified to analyze!\n")
    )
    config = Config(cwd=tmp_path, env={"ACCRA_PYTHON_VERSIONS": "3.11,3.12"})

    assert Adhoc(config).get_supported_language_versions() == {"3.11", "3.12"}


def test_adhoc_reports_a_vermin_failure(tmp_path, monkeypatch):
    # any other failure is an error, not an empty version set
    calls = []
    monkeypatch.setattr(
        utils, "run", _fake_run(calls, 2, "", "error: Failed to spawn: `vermin`")
    )

    result = Adhoc(Config(cwd=tmp_path)).get_supported_language_versions()

    assert isinstance(result, AccraError)
    assert "vermin could not determine" in result.message
    assert "Failed to spawn" in result.message


def test_install_targets_the_venv_with_the_budget_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    # --python, and subprocess_timeout() caps every call
    calls = []
    monkeypatch.setattr(utils, "run", _fake_run(calls))
    uv = Uv(Config(cwd=tmp_path, timeout=100))
    uv.subprocess_timeout = lambda: 7.0

    assert uv._install_dependency(Dependency(name="six", version=">=1")).success
    # the install, then the version for the Dockerfile pin and the import check;
    # every call carries the budget timeout
    command, _timeout = calls[0]
    assert command == ["uv", "pip", "install", "--python", ".venv/bin/python", "six>=1"]
    assert {t for _, t in calls} == {7.0}


def test_failed_install_fills_the_error_fields(tmp_path, monkeypatch):
    # command, exit code and stderr tail, where accra-language has them
    if "exit_code" not in AccraError.model_fields:
        pytest.skip("accra-language without AccraError.command/exit_code/stderr")
    calls = []
    monkeypatch.setattr(
        utils, "run", _fake_run(calls, 1, "", "x" * 5000 + "\nno match")
    )
    dependency = Dependency(name="nope", version="")

    for env_manager in (Uv, Venv):
        [error] = (
            env_manager(Config(cwd=tmp_path))._install_dependency(dependency).errors
        )
        assert error.dependency == dependency
        assert error.exit_code == 1
        assert error.command[-1] == "nope"
        assert len(error.stderr) == 2000 and error.stderr.endswith("no match")
        assert "(exit code 1: no match)" in error.message
