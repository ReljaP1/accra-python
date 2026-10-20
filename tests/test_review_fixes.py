"""The Python environment, an invalid requires-python, hostile import-root names
and the package docs."""

import http.client
import os
import subprocess
from pathlib import Path

import pytest
from accra_language import AccraError, Config, Dependency

from accra_python import (
    Adhoc,
    Python3,
    Requirements_txt,
    Setup_cfg,
    Uv,
    package_docs,
    utils,
)
from accra_python.python_environment import PythonEnvironment


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _dist(project: Path, name: str, version: str, files: list[str]) -> None:
    """A fake installed distribution (no top_level.txt) in the project's venv."""
    site = project / ".venv/lib/python3.6/site-packages"
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    )
    rows = [f"{f},sha256=x,1" for f in files] + [f"{info.name}/METADATA,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n")


def _run_importing(importable: set[str], calls: list | None = None):
    def run(command, config, timeout=None, **kwargs):
        if calls is not None:
            calls.append((list(command), timeout))
        code = command[-1]
        ok = code.startswith("import ") and code[len("import ") :] in importable
        return subprocess.CompletedProcess(command, 0 if ok else 1, "", "")

    return run


# -- Environment and version detection -------------------------------------------------------


@pytest.mark.parametrize(
    "dist, files, module",
    [
        ("scikit-learn", ["sklearn/__init__.py", "sklearn/base.py"], "sklearn"),
        ("beautifulsoup4", ["bs4/__init__.py", "bs4/x.py"], "bs4"),
        ("scikit-image", ["skimage/__init__.py"], "skimage"),
        ("protobuf", ["google/protobuf/message.py"], "google"),
        ("markdown-it-py", ["markdown_it/__init__.py"], "markdown_it"),
        ("tinymod", ["tinymod.py", "../../../bin/tinytool", "x.pth"], "tinymod"),
    ],
)
def test_import_names_come_from_the_installed_files(
    tmp_path, monkeypatch, dist, files, module
):
    _dist(tmp_path, dist, "1.0", [*files, "__pycache__/z.cpython-36.pyc"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _run_importing({module}))

    info = uv._distribution_dir(dist)
    assert uv._distribution_import_names(info) == [module]
    assert uv.is_usable(Dependency(name=dist, version="")) is True
    assert uv.import_check_error(Dependency(name=dist, version="")) is None


def test_installed_but_nothing_imports_is_a_failure(tmp_path, monkeypatch):
    _dist(tmp_path, "broken", "1.0", ["broken/__init__.py"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _run_importing(set()))

    assert uv.import_check_error(Dependency(name="broken", version="")) is not None


def test_no_import_name_cannot_be_decided(tmp_path, monkeypatch):
    # a distribution that ships only scripts and metadata
    _dist(tmp_path, "scripts-only", "1.0", ["../../../bin/tool"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _run_importing(set()))
    dependency = Dependency(name="scripts-only", version="")

    assert isinstance(uv.is_usable(dependency), AccraError)
    assert uv.import_check_error(dependency) is None


def test_a_local_module_with_no_distribution_is_tried_from_source(
    tmp_path, monkeypatch
):
    # ACCRa's source mode asks whether the project's own package imports from
    # the clone; it is no distribution
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _run_importing({"toolbox"}))

    assert uv.is_usable(Dependency(name="toolbox", version="")) is True
    assert uv.is_usable(Dependency(name="absent", version="")) is False


def test_a_distribution_that_is_not_installed_is_not_checked(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(utils, "run", _run_importing(set(), calls))
    uv = Uv(Config(cwd=tmp_path))
    dependency = Dependency(name="tomli", version="; python_version < '3.11'")

    assert uv.installed_version(dependency) is None
    assert uv.import_check_error(dependency) is None
    assert uv.pinned_spec(dependency) == "tomli; python_version < '3.11'"
    assert calls == []


def test_a_false_marker_install_is_no_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonEnvironment, "already_importable", lambda *_: False)
    monkeypatch.setattr(
        utils,
        "run",
        lambda command, *a, **k: subprocess.CompletedProcess(command, 0, "", ""),
    )
    uv = Uv(Config(cwd=tmp_path))
    dependency = Dependency(name="colorama", version="; platform_system == 'Windows'")

    result = uv._install_dependency(dependency)

    assert result.success and not result.errors
    [line] = result.dockerfile.dependencies_installation
    assert "colorama" in str(line) and "Windows" in str(line)


def test_installed_version_of_dotted_names(tmp_path):
    _dist(tmp_path, "ruamel.yaml", "0.18.6", ["ruamel/yaml/__init__.py"])
    _dist(tmp_path, "zope.interface", "6.1", ["zope/interface/__init__.py"])
    uv = Uv(Config(cwd=tmp_path))

    assert uv.installed_version(Dependency(name="ruamel.yaml", version=">=0.17")) == (
        "0.18.6"
    )
    assert uv.installed_version(Dependency(name="zope.interface", version="")) == "6.1"
    assert uv.pinned_spec(Dependency(name="ruamel.yaml", version=">=0.17")) == (
        "ruamel.yaml==0.18.6"
    )


@pytest.mark.integration
def test_a_real_install_with_renamed_imports_and_false_markers(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": (
                "scikit-learn\n"
                "beautifulsoup4\n"
                "tomli; python_version<'3.11'\n"
                "colorama; platform_system=='Windows'"
            )
            + "\n",
            "app.py": "import sklearn, bs4\n",
        },
    )
    env = {**os.environ, "UV_NO_PROJECT": "1"}
    python3 = Python3(Config(cwd=tmp_path, repo="https://fake_repo", env=env))
    assert python3.detect()

    result = python3.build()

    assert result.success
    assert not result.errors, result.errors
    lines = [str(i) for i in result.dockerfile.dependencies_installation]
    assert any("scikit-learn==" in line for line in lines)
    assert any("beautifulsoup4==" in line for line in lines)
    assert any("colorama" in line and "Windows" in line for line in lines)


# -- vermin output ----------------------------------------------------------------


def _vermin(monkeypatch, stdout):
    monkeypatch.setattr(
        utils,
        "run",
        lambda command, config, timeout=None, **kw: subprocess.CompletedProcess(
            command, 0, stdout, ""
        ),
    )


def test_mixed_python_2_and_3_files_give_every_version(tmp_path, monkeypatch):
    _vermin(monkeypatch, "Minimum required versions: ...\n:::!2:!3:\n")
    config = Config(cwd=tmp_path)

    assert Adhoc(config).get_supported_language_versions() == (
        utils.get_python_minor_versions(config=config)
    )


def test_python_2_only_code_stays_the_empty_set(tmp_path, monkeypatch):
    _vermin(monkeypatch, "2.7:!3:\n")

    assert Adhoc(Config(cwd=tmp_path)).get_supported_language_versions() == set()


# -- requirements.txt parsing ----------------------------------------------------------------


def test_requirements_with_hashes_continuations_and_bom(tmp_path):
    (tmp_path / "requirements.txt").write_bytes(
        b"\xef\xbb\xbfsix==1.16.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\n"
        b"requests>=2 --hash=sha256:123\n"
        b"--hash=sha256:zzz\n"
        b"!!not a requirement>=1\n"
        b"-e ./local\n"
    )
    found = Requirements_txt(Config(cwd=tmp_path)).extract_dependencies()

    assert Dependency(name="six", version="==1.16.0") in found
    assert Dependency(name="requests", version=">=2") in found
    assert Dependency(name="!!not a requirement", version=">=1") in found or all(
        not d.name.startswith("-") for d in found
    )
    assert all(not d.name.startswith("-") for d in found)


def test_nested_requirements_stay_inside_the_project(tmp_path):
    project = tmp_path / "project"
    _write(
        project,
        {
            "requirements.txt": "-r sub/more.txt\n-r ../outside.txt\nsix\n",
            "sub/more.txt": "attrs\n",
        },
    )
    (tmp_path / "outside.txt").write_text("secret-package\n")

    found = Requirements_txt(Config(cwd=project)).extract_dependencies()

    assert {d.name for d in found} == {"six", "attrs"}


def test_setup_cfg_requirements_keep_markers_and_skip_directives(tmp_path):
    _write(
        tmp_path,
        {
            "setup.cfg": "[options]\ninstall_requires =\n"
            "    six>=1.0  # a comment\n"
            "    tomli; python_version<'3.11'\n"
            "    file: requirements.txt\n"
            "    attr: pkg.deps\n"
            "    weird name>=1\n"
        },
    )
    manifest = Setup_cfg(Config(cwd=tmp_path))
    assert manifest.detect()
    found = manifest.extract_dependencies()

    assert Dependency(name="six", version=">=1.0") in found
    assert Dependency(name="tomli", version='; python_version < "3.11"') in found
    assert Dependency(name="weird name", version="") in found
    assert not any(d.name.startswith(("file", "attr")) for d in found)


def test_dependency_names_starting_with_a_dash_are_rejected():
    assert utils.requirement_dependency("-e foo") is None
    assert utils.poetry_dependency("--index-url", "1.0") is None
    dependencies = {
        Dependency(name="--evil", version=""),
        Dependency(name="six", version=""),
    }
    assert utils.project_dependencies(dependencies, None) == {
        Dependency(name="six", version="")
    }


# -- Timeouts ----------------------------------------------------------------


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired("cmd", 1)


def test_timeouts_in_the_import_check_do_not_raise(tmp_path, monkeypatch):
    _dist(tmp_path, "six", "1.16.0", ["six.py"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _timeout)
    dependency = Dependency(name="six", version="")

    assert uv.import_check_error(dependency) is None
    assert uv.pinned_spec(dependency) == "six==1.16.0"


def test_a_failing_version_lookup_leaves_the_spec_as_written(tmp_path, monkeypatch):
    uv = Uv(Config(cwd=tmp_path))

    def boom(dependency):
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(uv, "installed_version", boom)
    assert uv.pinned_spec(Dependency(name="six", version=">=1")) == "six>=1"
    assert uv.import_check_error(Dependency(name="six", version="")) is None


def test_the_import_check_has_its_own_short_timeout(tmp_path, monkeypatch):
    _dist(tmp_path, "six", "1.16.0", ["six.py"])
    calls: list = []
    monkeypatch.setattr(utils, "run", _run_importing({"six"}, calls))
    uv = Uv(Config(cwd=tmp_path, timeout=900))

    uv.is_usable(Dependency(name="six", version=""))

    assert calls and all(timeout <= 60 for _, timeout in calls)


def test_project_install_timeouts_fall_back_and_keep_source_mode(tmp_path, monkeypatch):
    _write(
        tmp_path,
        {
            "setup.py": "from setuptools import setup\nsetup(name='demo')\n",
            "demo/__init__.py": "",
            "scripts/tool.py": "",
        },
    )
    uv = Uv(Config(cwd=tmp_path, env={"ACCRA_PROJECT_INSTALL_TIMEOUT": "oops"}))
    seen: list = []

    def run(command, config, timeout=None, **kwargs):
        seen.append((list(command), timeout))
        if "pip" in command and "-e" in command:
            raise subprocess.TimeoutExpired(command, 1)
        if command[:3] == [uv._python(), "-m", "pip"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(utils, "run", run)

    ok = uv.install_project()  # `-e` timed out, the plain install worked
    assert ok.success
    assert any(t is None or t > 0 for _, t in seen)  # bad value: the default

    def always(command, config, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(utils, "run", always)
    failed = uv.install_project()
    assert not failed.success and failed.errors
    lines = [str(i) for i in failed.dockerfile.post_dependencies_installation]
    assert any(line.startswith("ENV PYTHONPATH=/project") for line in lines)


# -- Invalid requires-python is no constraint -----------------------------------------------------------------


def test_an_invalid_requires_python_is_no_constraint():
    universe = utils.get_python_minor_versions(">=0")
    assert utils.get_python_minor_versions("not a specifier") == universe
    assert utils.get_python_minor_versions(None) == universe  # type: ignore[arg-type]


def test_safe_parse_survives_deep_nesting():
    deep = "x = " + "[" * 200000 + "]" * 200000
    assert utils.safe_parse(deep) is None
    assert utils.safe_parse("x = 1") is not None
    assert utils.safe_parse("x = \x00") is None


def test_adhoc_survives_unparseable_source():
    code = "import yaml\nx = " + "[" * 200000 + "]" * 200000
    assert Adhoc._get_canonical_packages_from_code(code) == {"PyYAML"}


def test_check_script_reads_inside_the_guard(tmp_path):
    from accra_python import scripts

    bad = tmp_path / "bad.py"
    bad.write_bytes(b"x = '\xff'\n")
    assert isinstance(scripts.check_script(bad), AccraError)
    assert isinstance(scripts.check_script(tmp_path / "missing.py"), AccraError)


def test_run_replaces_undecodable_output(tmp_path):
    result = utils.run(
        ["python3", "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"],
        Config(cwd=tmp_path),
    )
    assert result.stdout == "a�b"


# -- Hostile directory names are not import roots -----------------------------------------------------------------


def test_hostile_directory_names_are_not_import_roots(tmp_path):
    for name in ("good", "a b", "x:y", "$HOME", "q'uote", "semi;colon"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "m.py").write_text("")
    uv = Uv(Config(cwd=tmp_path))

    assert utils.import_roots(tmp_path) == [".", "good"]
    [line] = uv._source_mode_lines()
    assert str(line) == "ENV PYTHONPATH=/project:/project/good"
    pythonpath = uv.run_environment({})["PYTHONPATH"]
    assert pythonpath.split(os.pathsep) == [
        str(tmp_path.resolve()),
        str(tmp_path.resolve() / "good"),
    ]


# -- package docs ----------------------------------------------------------


def test_package_docs_quote_the_name_and_survive_http_errors(tmp_path, monkeypatch):
    seen = []

    def urlopen(request, timeout=None):
        seen.append(request.full_url)
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(package_docs.urllib.request, "urlopen", urlopen)

    assert package_docs.package_documentation(tmp_path, "we ird/name") is None
    assert seen == ["https://pypi.org/pypi/we%20ird%2Fname/json"]


def test_setup_py_name_is_the_setup_calls_name_keyword(tmp_path):
    _write(
        tmp_path,
        {
            "setup.py": "from setuptools import setup\n"
            "meta = dict(filename='nope', author_name='nobody')\n"
            "setup(author_name='x', filename='y', name='real-name')\n"
        },
    )
    assert package_docs.package_name(tmp_path) == "real-name"
    assert utils.setup_call_name("setup(filename='y')") is None
    assert utils.setup_call_name("setup(((") is None
