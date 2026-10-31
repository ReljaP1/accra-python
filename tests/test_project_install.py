"""For Python: the project install, its names and covered
manifests, the run environment, the installed-patch choice, and read-only
version detection."""

import subprocess
from pathlib import Path

import pytest
from accra_language import AccraInstallError, AccraResult, Config

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

    result = uv.post_dependencies_installation()

    assert result.success and not result.errors, [e.message for e in result.errors]
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


@pytest.mark.integration
def test_failing_project_install_falls_back_to_source(tmp_path):
    _write(tmp_path, {"setup.py": "raise SystemExit('broken setup')\n", "lib.py": ""})
    _venv(tmp_path)
    uv = Uv(Config(cwd=tmp_path))

    result = uv.post_dependencies_installation()

    # lib imports from source: the project is usable, nothing to report
    assert result.success and not result.errors
    assert result.dockerfile.post_dependencies_installation == [
        "ENV PYTHONPATH=/project"
    ]


# -- the project's install is the post step: no error means initialised --------------------------------


def _post_step(tmp_path, monkeypatch, files, *, pip_works, importable, record=None):
    """The post step with pip and the import check stubbed: `pip_works` for
    `pip install`, `importable` the names that import."""
    _write(tmp_path, files)
    uv = Uv(Config(cwd=tmp_path, image_workdir="/srv/app"))
    monkeypatch.setattr(
        Uv, "_install_dependency", lambda self, dependency: AccraResult()
    )

    def run(command, config, timeout=None, **kwargs):
        return subprocess.CompletedProcess(command, 0 if pip_works else 1, "", "boom")

    monkeypatch.setattr(utils, "run", run)

    def importable_check(self, dependency, extra=()):
        if record is not None:
            record.append(dependency.name)
        return dependency.name in importable

    monkeypatch.setattr(Uv, "_is_importable", importable_check)
    return uv.post_dependencies_installation()


SETUP = {"setup.py": "from setuptools import setup\nsetup(name='demo')\n"}


def test_project_without_install_step_runs_from_source(tmp_path, monkeypatch):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {"main.py": "print(1)\n", "tools/run.py": "", "pkg/__init__.py": ""},
        pip_works=True,
        importable={"main", "pkg"},
    )

    assert result.success and not result.errors
    assert result.dockerfile.post_dependencies_installation == [
        "ENV PYTHONPATH=/srv/app:/srv/app/tools"
    ]


def test_a_project_with_nothing_importable_is_reported_but_builds(
    tmp_path, monkeypatch
):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {"main.py": "print(1)\n"},
        pip_works=True,
        importable=set(),
    )

    # Python runs from source: never a failed build, the error says why
    assert result.success
    [error] = result.errors
    assert isinstance(error, AccraInstallError)
    assert "not usable" in error.message


def test_a_project_with_no_package_of_its_own_runs_from_source(tmp_path, monkeypatch):
    # a repository with no Python package or module (an R project, say) has
    # nothing to import: source mode, as on main, not an error
    result = _post_step(
        tmp_path,
        monkeypatch,
        {"src/interface.cpp": "", "test/demo.R": "", "README.md": ""},
        pip_works=True,
        importable=set(),
    )

    assert result.success and not result.errors


def test_an_installed_project_that_imports_has_no_error(tmp_path, monkeypatch):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {**SETUP, "demo/__init__.py": ""},
        pip_works=True,
        importable={"demo"},
    )

    assert result.success and not result.errors
    assert result.dockerfile.post_dependencies_installation == [
        "RUN python -m pip install --no-cache-dir ."
    ]


def test_an_installed_project_whose_package_does_not_import_is_reported(
    tmp_path, monkeypatch
):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {**SETUP, "demo/__init__.py": ""},
        pip_works=True,
        importable=set(),
    )

    assert result.success and len(result.errors) == 1


def test_a_failed_install_with_a_package_that_imports_is_source_mode(
    tmp_path, monkeypatch
):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {**SETUP, "demo/__init__.py": ""},
        pip_works=False,
        importable={"demo"},
    )

    assert result.success and not result.errors
    assert result.dockerfile.post_dependencies_installation == [
        "ENV PYTHONPATH=/srv/app"
    ]


def test_a_failed_install_with_no_primary_package_is_source_mode(tmp_path, monkeypatch):
    # nothing but tests and docs to import: as on main, source mode, no error
    result = _post_step(
        tmp_path,
        monkeypatch,
        {
            "setup.py": "from setuptools import setup\nsetup()\n",
            "tests/__init__.py": "",
            "docs/conf.py": "",
        },
        pip_works=False,
        importable=set(),
    )

    assert result.success and not result.errors
    assert result.dockerfile.post_dependencies_installation[-1].startswith(
        "ENV PYTHONPATH="
    )


def test_a_distribution_name_is_not_something_to_import(tmp_path, monkeypatch):
    # the code lives in a namespace directory (no __init__.py) under src/ and the
    # install fails: the distribution name (`demo-dist`) is not an import name, so
    # there is no primary package to check and the project runs from source
    result = _post_step(
        tmp_path,
        monkeypatch,
        {
            "pyproject.toml": '[project]\nname = "demo-dist"\nversion = "0.1"\n',
            "src/demo_ns/data.py": "VALUE = 1\n",
            "scripts/run.py": "",
        },
        pip_works=False,
        importable=set(),
    )

    assert result.success and not result.errors, [e.message for e in result.errors]
    assert result.dockerfile.post_dependencies_installation[-1].startswith(
        "ENV PYTHONPATH="
    )


def test_a_script_project_checks_its_scripts_by_their_own_names(tmp_path, monkeypatch):
    # the candidates are the module names as written on disk, not their canonical
    # (lower case, dashed) form
    seen = []
    result = _post_step(
        tmp_path,
        monkeypatch,
        {"Run_Model.py": "print(1)\n"},
        pip_works=True,
        importable={"Run_Model"},
        record=seen,
    )

    assert result.success and not result.errors
    assert "Run_Model" in seen


def test_a_failed_install_that_leaves_nothing_importable_is_reported(
    tmp_path, monkeypatch
):
    result = _post_step(
        tmp_path,
        monkeypatch,
        {**SETUP, "demo/__init__.py": ""},
        pip_works=False,
        importable=set(),
    )

    assert result.success
    [error] = result.errors
    assert "could not install the project" in error.message


def test_run_environment_puts_the_import_roots_first(tmp_path):
    _write(tmp_path, {"scripts/a.py": "", "pkg/__init__.py": ""})
    env = Uv(Config(cwd=tmp_path)).run_environment({"PYTHONPATH": "/x", "PATH": "/bin"})
    root = tmp_path.resolve()
    assert env["PYTHONPATH"] == f"{root}:{root / 'scripts'}:/x"


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
