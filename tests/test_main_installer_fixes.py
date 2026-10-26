"""ACCRa `main`'s installer fixes (a corrected import-name table, the import
check by the modules a distribution ships, requirement markers and inline
comments), as the plugin has them."""

import re
import subprocess
from pathlib import Path

import pytest
from accra_language import Config, Dependency

from accra_python import Uv, utils
from accra_python.adhoc import Adhoc
from accra_python.import_names import IMPORT_NAMES, INSTALL_TO_IMPORT
from accra_python.python_environment import import_name_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

# facts main removed because they were wrong; they must not come back
_REMOVED_IMPORTS = {
    "python_dotenv",  # the import is dotenv
    "google",  # PyPI `google` is a search scraper (googlesearch)
    "azure",  # deprecated empty package, never installs
    "volcengine_ml_platform",  # repo-local modules or source-built extensions
    "hptr_modules",
    "srmrpy",
    "diff_gaussian_rasterization",
    "fda_query",
    "foldseek",
    "diff_surfel_rasterization",
    "task_workload",
}


# -- the import-name table -----------------------------------------------------


def test_the_table_is_well_formed_and_keeps_the_removed_facts_out():
    assert len(IMPORT_NAMES) > 150
    for import_name, install_name in IMPORT_NAMES.items():
        assert _PROJECT_NAME_RE.match(install_name), install_name
        assert "-" not in import_name, import_name
    assert not _REMOVED_IMPORTS & set(IMPORT_NAMES)
    # fixed facts
    assert IMPORT_NAMES["lavis"] == "salesforce-lavis"
    assert IMPORT_NAMES.get("wget", "wget") == "wget"
    assert IMPORT_NAMES["google.cloud.storage"] == "google-cloud-storage"
    assert IMPORT_NAMES["google.cloud.bigquery"] == "google-cloud-bigquery"
    assert IMPORT_NAMES["google.cloud.pubsub"] == "google-cloud-pubsub"
    assert IMPORT_NAMES["azure.storage.blob"] == "azure-storage-blob"
    assert IMPORT_NAMES["google.auth"] == "google-auth"
    assert IMPORT_NAMES["googleapiclient"] == "google-api-python-client"


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("python-dotenv", "dotenv"),
        ("google-auth", "google.auth"),
        ("google-cloud-storage", "google.cloud.storage"),
        ("google-api-python-client", "googleapiclient"),
        ("markdown-it-py", "markdown_it"),
        ("scikit-learn", "sklearn"),
        ("radonpy-pypi", "radonpy"),
        ("Python_Dotenv", "dotenv"),
        # the first fact is an identity fact: the package imports under its name
        ("matplotlib", None),
        ("pymupdf", None),
        ("numpy", None),
    ],
)
def test_the_first_non_dashed_import_name_is_the_reverse_mapping(package, expected):
    assert import_name_for(package) == expected


def test_the_reverse_table_holds_real_import_names_only():
    assert len(INSTALL_TO_IMPORT) > 100
    for install_name, import_name in INSTALL_TO_IMPORT.items():
        assert "-" not in import_name, import_name
        assert import_name in IMPORT_NAMES
        assert install_name == utils.canonical_name(install_name)
    # a package whose first fact is an identity fact imports under its own name
    assert "matplotlib" not in INSTALL_TO_IMPORT


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("from google.protobuf import text_format\n", {"protobuf"}),
        ("import google.cloud.storage\n", {"google-cloud-storage"}),
        ("from google.auth import credentials\n", {"google-auth"}),
        # the PyPI `google` and `azure` are not those namespaces
        ("import google.somethingelse\n", set()),
        ("import azure.somethingelse\n", set()),
        ("import dotenv\n", {"python-dotenv"}),
    ],
)
def test_imports_under_the_google_and_azure_namespaces(code, expected):
    assert Adhoc._get_canonical_packages_from_code(code) == expected


# -- the import check by the modules the distribution ships ---------------------


def _dist(project: Path, name: str, version: str, files: list[str]) -> None:
    site = project / ".venv/lib/python3.12/site-packages"
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    )
    rows = [f"{f},sha256=x,1" for f in files] + [f"{info.name}/METADATA,,"]
    (info / "RECORD").write_text("\n".join(rows) + "\n")


def _importing(importable: set[str]):
    def run(command, config, timeout=None, **kwargs):
        code = command[-1]
        ok = code.startswith("import ") and code[len("import ") :] in importable
        return subprocess.CompletedProcess(command, 0 if ok else 1, "", "")

    return run


@pytest.mark.parametrize(
    ("package", "files", "module"),
    [
        ("pycryptodome", ["Crypto/__init__.py", "Crypto/Hash/MD5.py"], "Crypto"),
        ("fonttools", ["fontTools/__init__.py", "fontTools/misc/x.py"], "fontTools"),
        ("pynacl", ["nacl/__init__.py", "_sodium.abi3.so"], "nacl"),
        ("biopython", ["Bio/__init__.py", "BioSQL/__init__.py"], "Bio"),
        ("pytest-xdist", ["xdist/__init__.py", "xdist/plugin.py"], "xdist"),
        ("pot", ["ot/__init__.py", "ot/utils.py"], "ot"),
    ],
)
def test_a_package_importing_under_another_name_is_confirmed_by_its_record(
    tmp_path, monkeypatch, package, files, module
):
    # the name and its underscore form do not import; the shipped module does
    _dist(tmp_path, package, "1.0", files)
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _importing({module}))
    dependency = Dependency(name=package, version="")

    assert module in uv._distribution_import_names(
        uv._distribution_dir(utils.canonical_name(package))
    )
    assert uv._is_importable(dependency) is True
    assert uv.import_check_error(dependency) is None


def test_a_package_whose_shipped_module_does_not_import_is_a_failed_dependency(
    tmp_path, monkeypatch
):
    _dist(tmp_path, "pycryptodome", "3.0", ["Crypto/__init__.py"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _importing(set()))

    error = uv.import_check_error(Dependency(name="pycryptodome", version=""))

    assert error is not None and "does not import" in error.message


def test_the_namespace_google_package_is_confirmed_by_its_shipped_module(
    tmp_path, monkeypatch
):
    # protobuf ships google/protobuf/...: its top-level name is `google`
    _dist(tmp_path, "protobuf", "5.0", ["google/protobuf/__init__.py"])
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _importing({"google"}))

    assert uv._is_importable(Dependency(name="protobuf", version="")) is True


def test_a_name_with_no_installed_distribution_is_still_unresolved(
    tmp_path, monkeypatch
):
    uv = Uv(Config(cwd=tmp_path))
    monkeypatch.setattr(utils, "run", _importing(set()))

    assert uv._is_importable(Dependency(name="accra-no-such-dist", version="")) is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "package",
    ["protobuf", "fonttools", "pycryptodome", "pynacl", "biopython", "pytest-xdist"],
)
def test_real_installs_import_under_the_names_they_ship(tmp_path, package):
    subprocess.run(["uv", "venv", "--seed", "-q", str(tmp_path / ".venv")], check=True)
    uv = Uv(Config(cwd=tmp_path))
    dependency = Dependency(name=package, version="")

    result = uv._install_dependency(dependency)

    assert not result.errors, [e.message for e in result.errors]
    assert uv.import_check_error(dependency) is None


# -- requirement markers and inline comments ---------------------------------------

PYPROJECT = """
[project]
name = "demo"
requires-python = ">=3.12"
dependencies = [
    "python-dotenv>=1.0.0",
    "audioop-lts>=0.2.0; python_version>='3.13'",
    "tomli; python_version < '3.11'",
    "numpy<2; python_version < '3.9'",
    "numpy>=2; python_version >= '3.9'",
    "weird-marker; this is not a marker",
]
"""

REQUIREMENTS = """
requests>=2  # HTTP client
pandas # we use pandas as default data format
colorama; sys_platform == "win32"
tomli
"""


def _names(dependencies) -> set[str]:
    return {utils.dependency_base_name(d) for d in dependencies}


def _project(tmp_path: Path, version: str) -> Uv:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    uv = Uv(Config(cwd=tmp_path))
    uv.selected_language_version = version
    return uv


def test_inline_comments_and_markers_do_not_leak_into_names(tmp_path):
    uv = _project(tmp_path, "3.12.4")

    dependencies = uv._get_dependencies()
    names = {d.name for d in dependencies}

    assert {"requests", "pandas"} <= names
    assert not any("#" in n or ";" in n or " " in n for n in names), names


def test_packages_whose_markers_are_all_false_are_dropped(tmp_path):
    kept = _names(_project(tmp_path, "3.12.4")._get_dependencies())

    assert "audioop-lts" not in kept
    assert "colorama" not in kept
    # also listed without a marker: the conditional listing cannot drop it
    assert "tomli" in kept
    # two conditional listings: kept while either applies
    assert "numpy" in kept
    # an unreadable marker keeps its package
    assert "weird-marker" in kept
    assert "python-dotenv" in kept


def test_a_package_comes_back_when_its_marker_holds(tmp_path):
    kept = _names(_project(tmp_path, "3.13.1")._get_dependencies())

    assert "audioop-lts" in kept


def test_nothing_is_dropped_without_a_selected_version(tmp_path):
    kept = _names(_project(tmp_path, "")._get_dependencies())

    assert {"audioop-lts", "colorama"} <= kept


def test_a_bare_name_survives_a_conditional_constraint(tmp_path):
    dependencies = {
        Dependency(name="tomli", version=""),
        Dependency(name="tomli", version="; python_version < '3.11'"),
        Dependency(name="six", version=""),
        Dependency(name="six", version=">=1"),
    }

    kept = utils.project_dependencies(dependencies, None)

    # conditional: the bare name stays; unconditional: the bare name is redundant
    assert Dependency(name="tomli", version="") in kept
    assert Dependency(name="six", version="") not in kept
    assert Dependency(name="six", version=">=1") in kept


def test_without_inapplicable_reads_python_versions_by_minor_and_full():
    dependencies = {
        Dependency(name="a", version="; python_version < '3.9'"),
        Dependency(name="b", version="; python_full_version >= '3.12.2'"),
    }

    assert _names(utils.without_inapplicable(dependencies, "3.12.1")) == set()
    assert _names(utils.without_inapplicable(dependencies, "3.12.4")) == {"b"}
    assert _names(utils.without_inapplicable(dependencies, "3.8.10")) == {"a"}
