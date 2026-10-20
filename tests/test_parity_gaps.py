"""The import check after a pip install, ad-hoc package rules, pinned Dockerfile
versions, pip's timeout and the venv install."""

import os
import subprocess
from pathlib import Path

import pytest
from accra_language import (
    AccraBuildError,
    AccraFailedDependencyInstallError,
    Config,
    Dependency,
)

from accra_python import Adhoc, Python3, Uv, Venv, utils
from accra_python.python_environment import PythonEnvironment


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _fake_run(calls, answers=None):
    """A `utils.run` whose result per command is `answers(command)`
    ((returncode, stdout)), default success."""

    def run(command, config, timeout=None, **kwargs):
        calls.append((list(command), kwargs))
        returncode, stdout = answers(list(command)) if answers else (0, "")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    return run


# -- Venv and dependency install ------------------------------------------------------------
# the venv, the dependencies one by one, then `pip install -e` with source mode
# as the fallback.


def _python3(project: Path) -> Python3:
    env = {**os.environ, "UV_NO_PROJECT": "1"}
    python3 = Python3(Config(cwd=project, repo="https://fake_repo", env=env))
    assert python3.detect()
    return python3


@pytest.mark.integration
def test_poetry_only_project_gets_its_dependencies_and_is_installed(tmp_path):
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[build-system]\n"
                'requires = ["poetry-core>=1.0"]\n'
                'build-backend = "poetry.core.masonry.api"\n'
                "\n"
                "[tool.poetry]\n"
                'name = "demo-poetry"\n'
                'version = "0.1.0"\n'
                'description = ""\n'
                "authors = []\n"
                "\n"
                "[tool.poetry.dependencies]\n"
                'python = "^3.8"\n'
                'six = "^1.16"'
            ),
            "demo_poetry/__init__.py": "VALUE = 7\n",
        },
    )
    python3 = _python3(tmp_path)

    built = python3.build()

    assert built.success, [e.message for e in built.errors]
    assert any(
        line.startswith("RUN uv pip install six==")
        for line in built.dockerfile.dependencies_installation
    )
    installed = python3.install_project()
    assert installed.success, [e.message for e in installed.errors]
    imported = subprocess.run(
        [
            str(tmp_path / ".venv/bin/python"),
            "-c",
            "import demo_poetry, six; print(demo_poetry.VALUE)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.stdout.strip() == "7", imported.stderr


@pytest.mark.integration
def test_a_failing_project_install_is_not_a_build_failure(tmp_path):
    # a failed project install leaves the project in source mode
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[build-system]\n"
                'requires = ["no-such-build-backend-zzz-1"]\n'
                'build-backend = "no_such_backend"\n'
                "\n"
                "[project]\n"
                'name = "demo-broken"\n'
                'version = "0.1.0"\n'
                'dependencies = ["six"]'
            ),
            "demo_broken/__init__.py": "",
        },
    )
    python3 = _python3(tmp_path)

    built = python3.build()
    assert built.success, [e.message for e in built.errors]

    installed = python3.install_project()
    assert not installed.success
    assert any(
        "PYTHONPATH" in str(line)
        for line in installed.dockerfile.post_dependencies_installation
    )


# -- pip timeout reaches uv's HTTP timeout ------------------------------------------------------------
# uv ignores PIP_TIMEOUT.


def test_uv_http_timeout_comes_from_pip_timeout():
    assert utils.uv_timeout_env(Config(env={"PIP_TIMEOUT": "900"})) == {
        "UV_HTTP_TIMEOUT": "900",
        "UV_HTTP_RETRIES": "5",
    }
    # without PIP_TIMEOUT only the retries are set
    assert utils.uv_timeout_env(Config(env={})) == {"UV_HTTP_RETRIES": "5"}
    assert utils.uv_timeout_env(
        Config(env={"PIP_TIMEOUT": "900", "UV_HTTP_TIMEOUT": "30"})
    ) == {"UV_HTTP_RETRIES": "5"}
    assert (
        utils.uv_timeout_env(
            Config(
                env={
                    "PIP_TIMEOUT": "900",
                    "UV_HTTP_TIMEOUT": "30",
                    "UV_HTTP_RETRIES": "2",
                }
            )
        )
        == {}
    )


def test_uv_commands_carry_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    calls = []
    monkeypatch.setattr(utils, "run", _fake_run(calls))
    uv = Uv(Config(cwd=tmp_path, env={"PIP_TIMEOUT": "120"}))

    uv._install_dependency(Dependency(name="six", version=""))

    install = calls[0]
    assert install[0][:3] == ["uv", "pip", "install"]
    assert install[1]["extra_env"] == {
        "UV_HTTP_TIMEOUT": "120",
        "UV_HTTP_RETRIES": "5",
    }


# -- Ad-hoc package rules and extras detection ------------------------------------------------------------


def _adhoc(tmp_path) -> set[str]:
    return {d.name for d in Adhoc(Config(cwd=tmp_path)).extract_dependencies()}


def test_only_the_own_packages_imports_are_dependencies(tmp_path):
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "import numpy\nfrom . import sub\n",
            "pkg/sub.py": "import pandas\nimport pkg.other\nimport os\n",
            "pkg/other.py": "",
            "scripts/run.py": "import torch\n",  # not an own package
            "tests/test_x.py": "import pytest\n",
        },
    )

    assert _adhoc(tmp_path) == {"numpy", "pandas"}


def test_imports_in_try_bodies_and_relative_imports_are_skipped(tmp_path):
    _write(
        tmp_path,
        {
            "pkg/__init__.py": (
                "try:\n"
                "    import optional_gpu\n"
                "except ImportError:\n"
                "    import fallback_cpu\n"
                "from .sub import thing\n"
                "import requests"
            ),
            "pkg/sub.py": "thing = 1\n",
        },
    )

    assert _adhoc(tmp_path) == {"fallback_cpu", "requests"}


def test_a_module_of_the_root_is_scanned_and_names_resolve(tmp_path):
    _write(tmp_path, {"run.py": "import skimage\nimport sklearn\nimport helper\n"})
    _write(tmp_path, {"helper.py": ""})

    assert _adhoc(tmp_path) == {"scikit-image", "scikit-learn"}


def test_a_file_that_does_not_parse_still_counts_by_regex(tmp_path):
    _write(
        tmp_path,
        {"pkg/__init__.py": "import numpy\nprint 'py2'\nfrom scipy.io import x\n"},
    )

    assert _adhoc(tmp_path) == {"numpy", "scipy"}


def test_the_four_name_rules_apply_in_every_file(tmp_path):
    # last matching rule wins per file, anywhere in the project
    _write(
        tmp_path,
        {
            "deep/er/a.py": "from pydantic_settings import BaseSettings\n",
            "docs/b.py": "import googleapiclient\n",
            "deep/c.py": "import google.auth\nimport google.api_core\n",
        },
    )

    assert _adhoc(tmp_path) == {
        "pydantic-settings",
        "google-api-python-client",
        "google-api-core",
    }


def test_the_extras_scan_names_plain_imports_too(tmp_path):
    _write(
        tmp_path, {"deep/x.py": "import uvicorn\nimport orjson\nfrom PIL import X\n"}
    )

    assert _adhoc(tmp_path) == {"uvicorn", "orjson", "pillow"}


def test_one_dependency_per_package(tmp_path):
    # `yaml` resolves to PyYAML and the extras scan names pyyaml
    _write(tmp_path, {"run.py": "import yaml\n"})

    assert len(Adhoc(Config(cwd=tmp_path)).extract_dependencies()) == 1


# -- Import check after a pip install -----------------------------------------------------------


def test_an_install_that_does_not_import_is_a_failed_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    monkeypatch.setattr(utils, "run", _fake_run([]))
    for manager in (Uv, Venv):
        env_manager = manager(Config(cwd=tmp_path))
        monkeypatch.setattr(env_manager, "is_usable", lambda dependency: False)
        monkeypatch.setattr(env_manager, "installed_version", lambda d: "1.16.0")

        result = env_manager._install_dependency(Dependency(name="six", version=""))

        assert result.success
        [error] = result.errors
        assert isinstance(error, AccraFailedDependencyInstallError)
        assert error.dependency == Dependency(name="six", version="")


def test_an_install_that_imports_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    monkeypatch.setattr(utils, "run", _fake_run([]))
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(uv, "is_usable", lambda dependency: True)

    assert not uv._install_dependency(Dependency(name="six", version="")).errors


def test_a_location_spec_is_not_import_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "run", _fake_run([]))
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(uv, "is_usable", lambda dependency: False)

    spec = "git+https://github.com/example/pkg"
    assert not uv._install_dependency(Dependency(name=spec, version="")).errors


def test_pkg_resources_is_repaired_with_setuptools_below_82(tmp_path, monkeypatch):
    calls = []
    state = {"repaired": False}

    def answers(command):
        if command[-2:] == ["-c", "import pkg_resources"]:
            return (0, "") if state["repaired"] else (1, "")
        if "setuptools<82" in command:
            state["repaired"] = True
        return (0, "")

    monkeypatch.setattr(utils, "run", _fake_run(calls, answers))

    assert Uv(Config(cwd=tmp_path)).ensure_pkg_resources() is None
    repair = [c for c, _ in calls if "setuptools<82" in c]
    assert len(repair) == 1 and "--upgrade" in repair[0] and "wheel" in repair[0]


def test_pkg_resources_that_imports_is_left_alone(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(utils, "run", _fake_run(calls))

    assert Uv(Config(cwd=tmp_path)).ensure_pkg_resources() is None
    assert len(calls) == 1


def test_a_failed_pkg_resources_repair_fails_the_build(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "run", _fake_run([], lambda command: (1, "")))

    error = Uv(Config(cwd=tmp_path)).ensure_pkg_resources()

    assert isinstance(error, AccraBuildError)


# -- Dockerfile pins the installed versions -----------------------------------------------------------
# each direct dependency is pinned `name==version`.


def test_the_dockerfile_line_pins_the_installed_version(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    monkeypatch.setattr(utils, "run", _fake_run([]))
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(uv, "installed_version", lambda dependency: "1.16.0")
    monkeypatch.setattr(uv, "is_usable", lambda dependency: True)

    result = uv._install_dependency(Dependency(name="six", version=">=1.0"))
    extras = uv._install_dependency(
        Dependency(name="pydantic[email]", version=">=2; python_version>'3.8'")
    )

    assert [str(i) for i in result.dockerfile.dependencies_installation] == [
        "RUN uv pip install six==1.16.0"
    ]
    assert [str(i) for i in extras.dockerfile.dependencies_installation] == [
        "RUN uv pip install 'pydantic[email]==1.16.0'"
    ]


def test_a_location_spec_stays_as_written_in_the_dockerfile(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "run", _fake_run([], lambda c: (0, "9.9\n")))
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(uv, "is_usable", lambda dependency: True)
    spec = "git+https://github.com/example/pkg"

    result = uv._install_dependency(Dependency(name=spec, version=""))

    assert [str(i) for i in result.dockerfile.dependencies_installation] == [
        f"RUN uv pip install {spec}"
    ]
