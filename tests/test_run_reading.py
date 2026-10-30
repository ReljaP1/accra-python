"""Reading Python runs and documentation."""

import io
import json
import re
import urllib.error

from accra_language import Config, RunDiagnosis

from accra_python import Python3, package_docs
from accra_python.scripts import PYTHON_SCRIPT_PROFILE

FAILED_RUN = """Traceback (most recent call last):
  File "run.py", line 1, in <module>
ModuleNotFoundError: No module named 'yaml'
ImportError: No module named "torch.nn"
FileNotFoundError: [Errno 2] No such file or directory: 'data/in.csv'
usage: run.py [-h] --config CONFIG input
run.py: error: the following arguments are required: --config, input
ModuleNotFoundError: No module named 'yaml'
"""


def test_diagnose_run(tmp_path):
    diagnosis = Python3(Config(cwd=tmp_path)).diagnose_run(FAILED_RUN)
    assert diagnosis.missing_dependencies == ("yaml", "torch.nn", "yaml")
    assert diagnosis.missing_files == ("data/in.csv",)
    assert diagnosis.missing_arguments == ("--config, input",)
    assert Python3(Config(cwd=tmp_path)).diagnose_run("") == RunDiagnosis()


def test_standard_modules(tmp_path):
    names = Python3(Config(cwd=tmp_path)).standard_modules()
    assert {"os", "sys", "typing", "pathlib", "subprocess", "__future__"} <= names
    assert "numpy" not in names


# ACCRa's paper-excerpt relevance signals: the profile has the Python ones,
# ACCRa keeps `--flag` and shell fences.
_ACCRA_RELEVANCE = re.compile(
    r"pip(?:3)?\s+install"
    r"|conda\s+install"
    r"|python(?:3)?\s+-m\s+"
    r"|python(?:3)?\s+\w[\w./]*\.py"
    r"|requirements(?:[-_]\w+)?\.txt"
    r"|--[a-z][\w-]+"
    r"|```\s*(?:bash|sh|shell|python)"
    r"|\$\s*python(?:3)?"
    r"|\$\s*pip(?:3)?",
    re.IGNORECASE,
)


def test_documentation_patterns_score_as_before():
    combined = re.compile(
        "|".join(PYTHON_SCRIPT_PROFILE.usage_patterns)
        + r"|--[a-z][\w-]+|```\s*(?:bash|sh|shell)",
        re.IGNORECASE,
    )
    text = (
        "Install with `pip install -e .` or conda install x; see requirements-dev.txt.\n"
        "```bash\n$ python3 -m pkg.run --epochs 3\n$ python train.py --lr 0.1\n```\n"
        "```python\nimport pkg\n```\n$ pip3 install pkg\nPIP INSTALL loud\n"
    )
    assert len(combined.findall(text)) == len(_ACCRA_RELEVANCE.findall(text)) > 8
    command = re.compile(rf"\b(?:{PYTHON_SCRIPT_PROFILE.command_pattern})\b")
    assert command.search("run python3 x.py") and not command.search("pythonic")
    assert PYTHON_SCRIPT_PROFILE.package_registry == "PyPI"


def test_package_name(tmp_path):
    assert package_docs.package_name(tmp_path, "repo") == "repo"
    assert package_docs.package_name(tmp_path) is None
    (tmp_path / "setup.py").write_text("setup(name='from-setup')\n")
    assert package_docs.package_name(tmp_path, "repo") == "from-setup"
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "from-poetry"\n[build-system]\nx = 1\n'
    )
    assert package_docs.package_name(tmp_path, "repo") == "from-poetry"
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "from-pep621"\n')
    assert package_docs.package_name(tmp_path, "repo") == "from-pep621"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_package_documentation(tmp_path, monkeypatch):
    seen = {}
    description = "Intro\n\n## Usage\n\n    pkg run --n 1\n"

    def fake_urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        body = {
            "info": {
                "summary": " A package. ",
                "description": description,
                "project_urls": {"Docs": "https://d", "": "x"},
            }
        }
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(package_docs.urllib.request, "urlopen", fake_urlopen)
    docs = Python3(Config(cwd=tmp_path)).package_documentation("pkg", timeout=3)
    assert seen == {"url": "https://pypi.org/pypi/pkg/json", "timeout": 3}
    assert (docs.registry, docs.package_name, docs.summary) == (
        "PyPI",
        "pkg",
        "A package.",
    )
    assert docs.description_excerpt == "## Usage\n\n    pkg run --n 1"
    assert dict(docs.project_urls) == {"Docs": "https://d"}

    def not_found(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(package_docs.urllib.request, "urlopen", not_found)
    assert Python3(Config(cwd=tmp_path)).package_documentation("pkg") is None

    def empty(request, timeout):
        return _Response(b'{"info": {}}')

    monkeypatch.setattr(package_docs.urllib.request, "urlopen", empty)
    assert Python3(Config(cwd=tmp_path)).package_documentation("pkg") is None
    assert Python3(Config(cwd=tmp_path)).package_documentation("") is None
