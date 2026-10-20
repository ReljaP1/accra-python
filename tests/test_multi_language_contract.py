"""The optional accra-language contract parts, for Python: presence,
toolchain, environment facts and entry points."""

import subprocess
from pathlib import Path

import pytest
from accra_language import AccraError, Config, Dependency

from accra_python import Python3, Uv, Venv


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_presence_is_read_only(tmp_path):
    assert Python3(Config(cwd=tmp_path)).is_present() is False

    _write(tmp_path, {".venv/lib/x.py": "", "notes/readme.md": ""})
    assert Python3(Config(cwd=tmp_path)).is_present() is False  # only in the venv

    _write(tmp_path, {"tools/run.py": "print(1)\n"})
    assert Python3(Config(cwd=tmp_path)).is_present() is True

    other = tmp_path / "rust"
    _write(other, {"Cargo.toml": "[package]\n", "requirements.txt": ""})
    assert Python3(Config(cwd=other)).is_present() is True  # a Python manifest

    before = sorted(str(p) for p in tmp_path.rglob("*"))
    Python3(Config(cwd=tmp_path)).is_present()
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_toolchain_is_declared(tmp_path):
    toolchain = Python3(Config(cwd=tmp_path)).spec.toolchain
    assert {"uv", "pyenv"} <= set(toolchain.executables)
    assert "build-essential" in toolchain.packages


def test_environment_facts_without_a_build(tmp_path):
    for env_manager, env_dir in ((Uv, ".venv"), (Venv, "venv")):
        manager = env_manager(Config(cwd=tmp_path))
        assert manager.environment_path() == tmp_path / env_dir
        run_env = manager.run_environment({"PATH": "/usr/bin", "HOME": "/h"})
        assert run_env["VIRTUAL_ENV"] == str(tmp_path / env_dir)
        assert run_env["PATH"] == f"{tmp_path / env_dir / 'bin'}:/usr/bin"
        assert run_env["HOME"] == "/h"

    uv = Uv(Config(cwd=tmp_path))
    assert uv.parse_dependency("numpy>=1.2").version == ">=1.2"
    assert uv.parse_dependency("git+https://x/y.git").name == "git+https://x/y.git"
    assert isinstance(uv.parse_dependency("not a requirement !"), AccraError)
    # a bare dotted import path names its top level; a requirement keeps its name
    assert (
        uv.parse_dependency("scipy.stats").name,
        uv.parse_dependency("scipy.stats").version,
    ) == ("scipy", "")
    assert uv.parse_dependency("ruamel.yaml>=0.17").name == "ruamel.yaml"
    assert uv.parse_dependency("zope.interface[x]").name == "zope.interface[x]"


def test_entry_points_are_discovered(tmp_path):
    _write(
        tmp_path,
        {
            "train.py": 'def main(): pass\n\nif __name__ == "__main__":\n    main()\n',
            "lib.py": "def helper(): pass\n",
            "cli.py": "import argparse\nargparse.ArgumentParser().parse_args()\n",
            "argv.py": "import sys\nprint(sys.argv)\n",
            "ask.py": "name = input('name? ')\n",
            # Python 2 code is the python2 plugin's, not this one's
            "legacy.py": "name = raw_input('name? ')\nprint name\n",
            "src/demo/__init__.py": "",
            "src/demo/run.py": "if __name__ == '__main__':\n    pass\n",
            "setup.py": "if __name__ == '__main__':\n    pass\n",
            "tests/test_x.py": "if __name__ == '__main__':\n    pass\n",
            "docs/conf.py": "import sys\nprint(sys.argv)\n",
            ".venv/lib/inside.py": "if __name__ == '__main__':\n    pass\n",
            "build/gen.py": "if __name__ == '__main__':\n    pass\n",
            "empty.py": "",
        },
    )
    uv = Uv(Config(cwd=tmp_path))
    python = str(tmp_path / ".venv/bin/python")

    entries = {
        Path(e.path).relative_to(tmp_path).as_posix(): e for e in uv.entry_points()
    }

    assert set(entries) == {
        "train.py",
        "cli.py",
        "argv.py",
        "ask.py",
        "src/demo/run.py",
    }
    assert entries["train.py"].kind == "script"
    assert entries["train.py"].command == (python, str(tmp_path / "train.py"))
    module = entries["src/demo/run.py"]
    assert (module.kind, module.command) == ("module", (python, "-m", "src.demo.run"))
    assert Python3(Config(cwd=tmp_path)).entry_points() == set(uv.entry_points())


@pytest.mark.integration
def test_installed_usable_and_uninstall_in_a_real_venv(tmp_path):
    subprocess.run(["uv", "venv", "--seed", "-q", str(tmp_path / ".venv")], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--python",
            str(tmp_path / ".venv/bin/python"),
            "six",
        ],
        check=True,
    )
    uv = Uv(Config(cwd=tmp_path))

    installed = uv.installed_dependencies()
    assert not isinstance(installed, AccraError)
    assert any(d.name == "six" and d.version.startswith("==") for d in installed)
    assert uv.is_usable(Dependency(name="six", version="")) is True
    assert uv.is_usable(Dependency(name="no-such-module-accra", version="")) is False

    assert uv.uninstall_dependencies([Dependency(name="six", version="")]).success
    assert uv.is_usable(Dependency(name="six", version="")) is False
