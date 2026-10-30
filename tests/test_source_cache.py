"""The project's Python sources are read once per unchanged tree, a changed
tree is re-read, and a huge generated file is skipped."""

from pathlib import Path

from accra_language import Config

from accra_python import sources
from accra_python.adhoc import Adhoc
from accra_python.project_analyzers import (
    Network,
    Parallelism,
)


def _count_reads(monkeypatch):
    calls = []
    real = sources._read

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(sources, "_read", counting)
    return calls


def _project(tmp_path):
    (tmp_path / "a.py").write_text("import os\nprint(1)\n")
    (tmp_path / "b.py").write_text("import threading\n")
    return tmp_path


def test_two_consumers_share_one_read(tmp_path, monkeypatch):
    sources.clear_cache()
    project = _project(tmp_path)
    calls = _count_reads(monkeypatch)
    first = sources.python_sources(project)
    second = sources.python_sources(project)
    assert len(calls) == 2  # a.py and b.py, once
    assert [p for p, _, _ in first] == [p for p, _, _ in second]


def test_analyzers_share_the_read(tmp_path, monkeypatch):
    sources.clear_cache()
    project = _project(tmp_path)
    calls = _count_reads(monkeypatch)
    config = Config(cwd=project)

    class Env:
        def _get_dependencies(self):
            return []

    Network(config).analyze_project(Env())
    Parallelism(config).analyze_project(Env())
    assert len(calls) == 2


def test_changed_tree_is_re_read(tmp_path, monkeypatch):
    sources.clear_cache()
    project = _project(tmp_path)
    calls = _count_reads(monkeypatch)
    sources.python_sources(project)
    (project / "c.py").write_text("import sys\n")
    names = {p.name for p, _, _ in sources.python_sources(project)}
    assert names == {"a.py", "b.py", "c.py"}
    # edit in place, size changes
    (project / "a.py").write_text("import json\nimport re\n")
    again = {p.name: i for p, _, i in sources.python_sources(project)}
    assert "json" in again["a.py"]
    # removal
    (project / "b.py").unlink()
    assert {p.name for p, _, _ in sources.python_sources(project)} == {
        "a.py",
        "c.py",
    }
    assert len(calls) > 2


def test_two_projects_do_not_mix(tmp_path):
    sources.clear_cache()
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "x.py").write_text("import os\n")
    (two / "y.py").write_text("import sys\n")
    assert [p.name for p, _, _ in sources.python_sources(one)] == ["x.py"]
    assert [p.name for p, _, _ in sources.python_sources(two)] == ["y.py"]


def test_huge_file_is_skipped(tmp_path, monkeypatch):
    sources.clear_cache()
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 1000)
    (tmp_path / "small.py").write_text("import os\n")
    (tmp_path / "data.py").write_text("X = [" + "1, " * 1000 + "]\n")
    assert [p.name for p, _, _ in sources.python_sources(tmp_path)] == ["small.py"]


def test_generated_scripts_stay_skipped(tmp_path):
    sources.clear_cache()
    (tmp_path / "a.py").write_text("import os\n")
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "z.py").write_text("import os\n")
    assert [p.name for p, _, _ in sources.python_sources(tmp_path)] == ["a.py"]


def test_adhoc_reads_each_file_once(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import numpy\n")
    reads = []

    real_bytes = Path.read_bytes

    def counting(self):
        reads.append(self.name)
        return real_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    Adhoc(Config(cwd=tmp_path)).extract_dependencies()
    assert reads.count("__init__.py") == 1
