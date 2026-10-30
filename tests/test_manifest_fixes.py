"""The standard library of any Python 3 version is
never a dependency, Poetry's constraints are kept, and setup.py's
install_requires is read with `ast`, versions included."""

from accra_language import Config, Dependency

from accra_python.adhoc import Adhoc
from accra_python.pyproject_toml import Pyproject_toml
from accra_python.setup_py import Setup_py, install_requires
from accra_python.utils import poetry_dependency, poetry_specifier, standard_modules


def _deps(manifest_class, tmp_path, name, text):
    (tmp_path / name).write_text(text)
    manifest = manifest_class(Config(cwd=tmp_path))
    assert manifest.detect()
    return manifest.extract_dependencies()


# -- Standard library is never a dependency -----------------------------------------------------------------------------


def test_removed_standard_modules_are_never_dependencies(tmp_path):
    assert {"os", "typing", "distutils", "imp", "asyncore", "telnetlib", "lib2to3"} <= (
        standard_modules()
    )
    assert "numpy" not in standard_modules()
    deps = _deps(
        Adhoc,
        tmp_path,
        "tool.py",
        "import distutils.core, imp, telnetlib, asyncore\nimport numpy\n",
    )
    assert {d.name for d in deps} == {"numpy"}


# -- Poetry constraints are kept -----------------------------------------------------------------------------


def test_poetry_constraints_become_pep440():
    assert poetry_specifier("^1.2.3") == ">=1.2.3,<2.0.0"
    assert poetry_specifier("^0.2.3") == ">=0.2.3,<0.3.0"
    assert poetry_specifier("^0.0.3") == ">=0.0.3,<0.0.4"
    assert poetry_specifier("~1.2.3") == ">=1.2.3,<1.3.0"
    assert poetry_specifier("~1") == ">=1,<2.0"
    assert poetry_specifier("1.2.3") == "==1.2.3"
    assert poetry_specifier("1.2.*") == "==1.2.*"
    assert poetry_specifier(">= 1.0, < 2") == ">=1.0,<2"
    assert poetry_specifier("~=1.4") == "~=1.4"
    for unconstrained in ("*", "", "^1 || ^2", "^abc"):
        assert poetry_specifier(unconstrained) == ""


def test_poetry_entries():
    assert poetry_dependency("requests", {"version": "^2.28", "extras": ["socks"]}) == (
        Dependency(name="requests[socks]", version=">=2.28,<3.0")
    )
    assert poetry_dependency("pkg", {"git": "https://g/o/r.git", "rev": "abc"}) == (
        Dependency(name="pkg @ git+https://g/o/r.git@abc", version="")
    )
    assert poetry_dependency("local", {"path": "../x"}) is None
    assert poetry_dependency("opt", {"version": "^1", "optional": True}) is None
    assert poetry_dependency("python", "^3.8") is None
    assert poetry_dependency("numpy", [{"version": "^1.24"}, {"version": "^1.21"}]) == (
        Dependency(name="numpy", version=">=1.24,<2.0")
    )


def test_poetry_project(tmp_path):
    deps = _deps(
        Pyproject_toml,
        tmp_path,
        "pyproject.toml",
        '[tool.poetry]\nname = "proj"\n\n[tool.poetry.dependencies]\n'
        'python = "^3.9"\nnumpy = "^1.24"\nrich = "*"\n'
        'httpx = { version = "~0.27", extras = ["http2"] }\n',
    )
    assert deps == {
        Dependency(name="numpy", version=">=1.24,<2.0"),
        Dependency(name="rich", version=""),
        Dependency(name="httpx[http2]", version=">=0.27,<0.28"),
    }
    manifest = Pyproject_toml(Config(cwd=tmp_path))
    manifest.detect()
    versions = manifest.get_supported_language_versions()
    assert "3.8" not in versions and {"3.9", "3.12"} <= versions


# -- setup.py install_requires is read with ast -----------------------------------------------------------------------------


def test_setup_py_read_with_ast():
    assert install_requires(
        "from setuptools import setup\n"
        "base = ['numpy>=1.20', 'scipy']\n"
        "setup(name='x', install_requires=base + ['torch[cuda]>=2'])\n"
    ) == ["numpy>=1.20", "scipy", "torch[cuda]>=2"]
    assert install_requires(
        "import setuptools\nREQ = ('requests>=2', 'pyyaml')\n"
        "setuptools.setup(install_requires=REQ)\n"
    ) == ["requests>=2", "pyyaml"]
    assert install_requires("setup(install_requires=open('r.txt').read())") is None
    assert install_requires("print 'not python 3'") is None


def test_setup_py_versions_kept(tmp_path):
    deps = _deps(
        Setup_py,
        tmp_path,
        "setup.py",
        "from setuptools import setup\nREQS = [\n    'numpy>=1.20',\n"
        "    'pandas; python_version > \"3.8\"',\n]\n"
        "setup(name='x', install_requires=REQS + ['scipy'])\n",
    )
    assert deps == {
        Dependency(name="numpy", version=">=1.20"),
        Dependency(name="pandas", version='; python_version > "3.8"'),
        Dependency(name="scipy", version=""),
    }


def test_setup_py_falls_back_to_lines(tmp_path):
    deps = _deps(
        Setup_py,
        tmp_path,
        "setup.py",
        "import os\nfrom setuptools import setup\n"
        "setup(name='x',\n      install_requires=['six>=1.0', 'attrs'],\n"
        "      long_description=open(os.path.join('README')).read() + undefined)\n"
        "print 'python 2'\n",
    )
    assert {d.name for d in deps} == {"six", "attrs"}
