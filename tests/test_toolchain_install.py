"""The plugin's toolchain: what it installs and the environment it sets."""

import subprocess

import pytest
from accra_language import Config

from accra_python import (
    DOCKER_NATIVE_PACKAGES,
    Python3,
    Uv,
    Venv,
    docker_python_toolchain,
    utils,
)
from accra_python.utils import docker_builds_python_from_source


def test_toolchain_installs_pyenv_and_the_python3_versions(tmp_path):
    toolchain = Python3(Config(cwd=tmp_path)).spec.toolchain
    rendered = toolchain.for_home("/home/u")

    assert toolchain.executables == ("uv", "pyenv", "git")
    assert dict(rendered.environment) == {
        "PYENV_ROOT": "/home/u/.pyenv",
        "UV_PYTHON_INSTALL_DIR": "/home/u/uv-pythons",
    }
    assert rendered.path == ("/home/u/.pyenv/bin", "/home/u/.pyenv/shims")

    script = "\n".join(rendered.install)
    assert "git clone https://github.com/pyenv/pyenv.git /home/u/.pyenv" in script
    assert "pyenv install --skip-existing 3.5.10" in script
    assert "pyenv install --skip-existing 3.6.15" in script
    assert "uv python install 3.8.20 3.9.22 3.10.17 3.11.12 3.12.10 3.13.2" in script
    assert 'ln -sfn "$d" "$PYENV_ROOT/versions/$ver"' in script
    assert "{home}" not in script
    # Python 2 is the python2 plugin's
    assert "2.7" not in script

    # what analysed projects build and load
    for package in ("build-essential", "libssl-dev", "tk-dev", "libgl1", "virtualenv"):
        assert package in toolchain.packages


def test_toolchain_environment_applies(tmp_path):
    rendered = Python3(Config(cwd=tmp_path)).spec.toolchain.for_home(tmp_path)
    env = rendered.applied_to({"PATH": "/usr/bin"})
    assert env["PATH"] == f"{tmp_path}/.pyenv/bin:{tmp_path}/.pyenv/shims:/usr/bin"
    assert env["PYENV_ROOT"] == f"{tmp_path}/.pyenv"


@pytest.mark.parametrize(
    "version", ["3.5.10", "3.6.15", "3.7.17", "3.8.20", "3.11.12", "3.12", "3.14.0"]
)
@pytest.mark.parametrize("manager", [Uv, Venv])
def test_pre_steps_start_with_the_toolchain_for_every_version(
    tmp_path, monkeypatch, manager, version
):
    # the caller's base image is a bare Debian: no Python, no uv
    monkeypatch.setattr(utils, "installed_patch", lambda v, *_: v)
    monkeypatch.setattr(utils, "pyenv_project_setup", lambda *_: None)
    environment = manager(Config(cwd=tmp_path, language_version=version))
    environment.ensure_pkg_resources = lambda: None
    monkeypatch.setattr(
        utils,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
    )

    result = environment.pre_dependencies_installation()

    assert result.success
    steps = [str(step) for step in result.dockerfile.pre_dependencies_installation]
    toolchain = [str(s) for s in docker_python_toolchain(version)]
    assert steps[: len(toolchain)] == toolchain
    assert steps[len(toolchain)] == str(DOCKER_NATIVE_PACKAGES)
    assert "install.sh" in toolchain[0]
    assert "FROM" not in "".join(steps)


def test_prebuilt_versions_install_uv_pythons_into_a_shared_directory():
    steps = [str(s) for s in docker_python_toolchain("3.11.12")]
    assert steps[1:] == [
        "ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python",
        "RUN uv python install 3.11.12",
    ]


@pytest.mark.parametrize("version", ["3.5.10", "3.6.15", "3.7.17"])
def test_old_versions_are_compiled_with_pyenv(version):
    steps = [str(s) for s in docker_python_toolchain(version)]
    script = "\n".join(steps)
    minor = ".".join(version.split(".")[:2])

    assert docker_builds_python_from_source(version)
    assert "uv python install" not in script
    # the build dependencies come before the compile, which comes before the link
    positions = [
        next(i for i, s in enumerate(steps) if marker in s)
        for marker in (
            "apt-get install -y build-essential",
            "git clone --depth 1 https://github.com/pyenv/pyenv.git /opt/pyenv",
            f"pyenv install --skip-existing {version}",
            f"/usr/local/bin/python{minor}",
        )
    ]
    assert positions == sorted(positions)
    assert "libssl-dev" in script


def test_prebuilt_versions_are_not_compiled():
    for version in ("3.8", "3.9.22", "3.13.2", "3.14"):
        assert not docker_builds_python_from_source(version)
