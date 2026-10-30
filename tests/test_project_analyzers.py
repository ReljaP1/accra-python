"""The static network and parallelism analyses."""

from pathlib import Path

import accra_language
from accra_language import Config, Metrics

from accra_python import Python3


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _analyze(tmp_path, metric):
    python3 = Python3(Config(cwd=tmp_path, language_version="3.11"))
    assert python3.detect() is True
    results = python3.analyze_project({metric})
    [(name, by_entry)] = results.items()
    # a whole-project measure is keyed by None
    [(key, profiles)] = by_entry.items()
    assert key is None
    assert not isinstance(profiles, accra_language.AccraError), profiles.message
    [profile] = profiles
    return name, profile


def test_network(tmp_path):
    _write(
        tmp_path,
        {
            "requirements.txt": "requests\nnumpy\n",
            "client.py": "import requests\nrequests.get('https://example.org')\n",
            "server.py": "from flask import Flask\napp = Flask(__name__)\n",
            "both.py": "import requests, socket\n",
            "plain.py": "import os\n",
            ".venv/lib/inside.py": "import requests\n",
            "empty.py": "",
        },
    )
    name, profile = _analyze(tmp_path, Metrics.NETWORK)
    assert name == "python-network"
    assert profile.requires_network
    assert profile.dependencies == ("requests",)
    assert {(u.name, u.count) for u in profile.imports} == {
        ("requests", 2),
        ("socket", 1),
    }
    assert [u.name for u in profile.web_frameworks] == ["flask"]
    assert [(u.name, u.count) for u in profile.api_calls] == [
        (r"requests\.(get|post|put|delete|patch|head)", 1)
    ]


def test_no_network(tmp_path):
    _write(tmp_path, {"plain.py": "import os\nprint(os.getcwd())\n"})
    _name, profile = _analyze(tmp_path, Metrics.NETWORK)
    assert not profile.requires_network
    assert profile.imports == () and profile.dependencies == ()


def test_parallelism(tmp_path):
    _write(
        tmp_path,
        {
            "threads.py": "import threading\nfrom concurrent.futures import ThreadPoolExecutor\n",
            "procs.py": "import multiprocessing\nfrom joblib import Parallel\n",
            # Python 2 code is the python2 plugin's, not this one's
            "legacy.py": "import threading\nprint 'python 2'\n",
            "build/skipped.py": "import multiprocessing\n",
        },
    )
    name, profile = _analyze(tmp_path, Metrics.PARALLELISM)
    assert name == "python-parallelism"
    threading_files = {
        Path(f.path).name: f.modules for f in profile.multithreading.files
    }
    assert threading_files == {"threads.py": ("threading",)}
    assert profile.multithreading.used
    processes = {Path(f.path).name: f.modules for f in profile.multiprocessing.files}
    assert processes == {"procs.py": ("joblib", "joblib.Parallel", "multiprocessing")}
    assert profile.multiprocessing.modules == (
        "joblib",
        "joblib.Parallel",
        "multiprocessing",
    )
    assert all(Path(f.path).is_absolute() for f in profile.multiprocessing.files)
