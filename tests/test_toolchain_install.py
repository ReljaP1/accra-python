"""The plugin's toolchain: what it installs and the environment it sets."""

from accra_language import Config

from accra_python import Python3


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
