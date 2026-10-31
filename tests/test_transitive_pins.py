"""The Dockerfile pins the transitive packages of the analysed environment: one
`--no-deps` install of every installed package the direct dependency lines do not
pin, so an image built from it gets the same versions."""

import json
import subprocess

import pytest
from accra_language import (
    AccraError,
    AccraInstallError,
    AccraResult,
    Config,
    Dependency,
)

from accra_python import Uv, Venv, utils


def _dep(name, version="1.0"):
    return Dependency(name=name, version=f"=={version}")


def _manager(cls, tmp_path, monkeypatch, *, installed, direct=(), own=()):
    """A manager with the pip list, the manifests' dependencies and the project's
    own names stubbed."""
    manager = cls(Config(cwd=tmp_path, image_workdir="/srv/app"))
    monkeypatch.setattr(cls, "installed_dependencies", lambda self: installed)
    monkeypatch.setattr(
        cls,
        "_get_dependencies",
        lambda self: {Dependency(name=n, version="") for n in direct},
    )
    monkeypatch.setattr(cls, "project_names", lambda self: set(own))
    return manager


def _line(manager):
    [line] = manager.transitive_pins()
    return str(line)


@pytest.mark.parametrize(
    ("cls", "command"),
    [
        (Uv, "uv pip install --no-deps"),
        (Venv, "venv/bin/python -m pip install --no-deps"),
    ],
)
def test_transitive_packages_are_pinned_in_one_line(
    tmp_path, monkeypatch, cls, command
):
    manager = _manager(
        cls,
        tmp_path,
        monkeypatch,
        installed={
            _dep("numpy", "2.1.0"),
            _dep("six", "1.16.0"),
            _dep("requests", "2.31.0"),
        },
        direct=["requests"],
    )

    # sorted by name, the direct dependency left to its own line
    assert _line(manager) == f"RUN {command} numpy==2.1.0 six==1.16.0"


def test_direct_dependencies_are_not_repeated_whatever_their_spelling(
    tmp_path, monkeypatch
):
    manager = _manager(
        Uv,
        tmp_path,
        monkeypatch,
        installed={
            _dep("Ruamel.Yaml", "0.17"),
            _dep("typing_extensions", "4.9"),
            _dep("idna", "3.6"),
        },
        direct=["ruamel-yaml>=0.17", "typing-extensions[x]"],
    )

    assert _line(manager) == "RUN uv pip install --no-deps idna==3.6"


def test_tooling_and_the_project_itself_are_left_out(tmp_path, monkeypatch):
    manager = _manager(
        Uv,
        tmp_path,
        monkeypatch,
        installed={
            _dep("pip", "24.0"),
            _dep("setuptools", "70.0"),
            _dep("wheel", "0.43"),
            _dep("uv", "0.5"),
            _dep("pkg_resources", "1"),
            _dep("demo-setup", "0.1"),
            _dep("attrs", "23.2"),
        },
        own={"demo-setup"},
    )

    assert _line(manager) == "RUN uv pip install --no-deps attrs==23.2"


def test_a_local_version_label_and_an_empty_version_are_left_out(tmp_path, monkeypatch):
    manager = _manager(
        Uv,
        tmp_path,
        monkeypatch,
        installed={
            _dep("torch", "2.1.0+cu118"),
            Dependency(name="broken", version="=="),
            _dep("attrs", "23.2"),
        },
    )

    assert _line(manager) == "RUN uv pip install --no-deps attrs==23.2"


def test_an_install_from_a_location_is_left_out(tmp_path, monkeypatch):
    site = tmp_path / ".venv" / "lib" / "python3.11" / "site-packages"
    for dist, direct_url in [
        ("from_git-1.0.dist-info", {"url": "https://github.com/x/y", "vcs_info": {}}),
        ("local_pkg-2.0.dist-info", {"url": "file:///tmp/local_pkg"}),
        ("on_index-3.0.dist-info", None),
    ]:
        (site / dist).mkdir(parents=True)
        if direct_url is not None:
            (site / dist / "direct_url.json").write_text(json.dumps(direct_url))
    manager = _manager(
        Uv,
        tmp_path,
        monkeypatch,
        installed={_dep("from-git"), _dep("local_pkg", "2.0"), _dep("on-index", "3.0")},
    )

    assert manager._location_distributions() == {"from-git", "local-pkg"}
    assert _line(manager) == "RUN uv pip install --no-deps on-index==3.0"


def test_no_line_when_there_is_nothing_to_add(tmp_path, monkeypatch):
    only_direct = _manager(
        Uv, tmp_path, monkeypatch, installed={_dep("requests")}, direct=["requests"]
    )
    empty = _manager(Venv, tmp_path, monkeypatch, installed=set())

    assert only_direct.transitive_pins() == []
    assert empty.transitive_pins() == []


def test_no_line_when_the_package_list_cannot_be_read(tmp_path, monkeypatch):
    manager = _manager(
        Uv,
        tmp_path,
        monkeypatch,
        installed=AccraInstallError(message="pip could not list"),
    )

    assert isinstance(manager.installed_dependencies(), AccraError)
    assert manager.transitive_pins() == []


def test_no_line_when_listing_the_packages_times_out(tmp_path, monkeypatch):
    manager = _manager(Uv, tmp_path, monkeypatch, installed=set())

    def listing(self):
        raise subprocess.TimeoutExpired("pip list", 1)

    monkeypatch.setattr(Uv, "installed_dependencies", listing)

    assert manager.transitive_pins() == []


def test_the_line_is_deterministic(tmp_path, monkeypatch):
    names = ["zlib-ng", "attrs", "mccabe", "bar", "Foo"]
    first = _manager(Uv, tmp_path, monkeypatch, installed={_dep(n) for n in names})
    second = _manager(
        Uv, tmp_path, monkeypatch, installed={_dep(n) for n in reversed(names)}
    )

    assert _line(first) == _line(second)
    assert _line(first).split()[-5:] == [
        "attrs==1.0",
        "bar==1.0",
        "Foo==1.0",
        "mccabe==1.0",
        "zlib-ng==1.0",
    ]


def _post_step(cls, tmp_path, monkeypatch, *, installed, direct):
    (tmp_path / "main.py").write_text("print(1)\n")
    manager = _manager(
        cls, tmp_path, monkeypatch, installed=installed, direct=direct, own={"main"}
    )
    monkeypatch.setattr(cls, "_install_dependency", lambda self, d: AccraResult())
    monkeypatch.setattr(cls, "_is_importable", lambda self, d, extra=(): True)
    monkeypatch.setattr(
        utils,
        "run",
        lambda command, config, timeout=None, **kw: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    return manager.post_dependencies_installation()


@pytest.mark.parametrize("cls", [Uv, Venv])
def test_the_post_step_adds_the_pins_to_the_dependency_lines(
    tmp_path, monkeypatch, cls
):
    result = _post_step(
        cls,
        tmp_path,
        monkeypatch,
        installed={_dep("requests"), _dep("urllib3", "2.2.0")},
        direct=["requests"],
    )

    # in the dependency section, so they come before the project's own install
    lines = [str(line) for line in result.dockerfile.dependencies_installation]
    assert lines[-1].endswith("--no-deps urllib3==2.2.0")
    assert not any("requests==" in line for line in lines)
    assert not any(
        "urllib3" in str(l) for l in result.dockerfile.post_dependencies_installation
    )


def test_the_post_step_adds_no_line_for_a_project_with_no_extra_packages(
    tmp_path, monkeypatch
):
    result = _post_step(
        Uv, tmp_path, monkeypatch, installed={_dep("requests")}, direct=["requests"]
    )

    assert not any(
        "--no-deps" in str(line) for line in result.dockerfile.dependencies_installation
    )
