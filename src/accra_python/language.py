import logging
import tomllib
from pathlib import Path
from typing import override

from accra_language import (
    Config,
    EnvironmentManager,
    Language,
    LanguageSpec,
    Toolchain,
)

from . import package_docs, run_reading, scripts
from .analyzers import Memray, Scalene
from .project_analyzers import Network, Parallelism
from .python_environment import is_python_project
from .uv import Uv

logger = logging.getLogger(__name__)

# What the env managers run on the host: uv and pyenv (which builds Pythons
# from source, hence its build dependencies), and git for `uv` VCS installs.
# `install` sets them up for the user that runs the plugin: pyenv with the
# Python 3 versions the version universe allows, 3.5 and 3.6 compiled (no prebuilt
# standalone build exists) and 3.8 to 3.13 as uv's prebuilt CPythons linked
# into pyenv's versions. Python 2.7 is the python2 plugin's.
_PYENV_PYTHONS = ("3.5.10", "3.6.15")
_UV_PYTHONS = ("3.8.20", "3.9.22", "3.10.17", "3.11.12", "3.12.10", "3.13.2")
_SHELL_SETUP = (
    'export PYENV_ROOT="{home}/.pyenv"',
    'export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"',
    'eval "$(pyenv init --path)"',
    'eval "$(pyenv init -)"',
    'eval "$(pyenv virtualenv-init -)"',
)
_TOOLCHAIN = {
    "toolchain": Toolchain(
        executables=("uv", "pyenv", "git"),
        packages=(
            "git",
            "curl",
            "build-essential",
            "gcc",
            "g++",
            "make",
            "python3-dev",
            "libssl-dev",
            "zlib1g-dev",
            "libbz2-dev",
            "libreadline-dev",
            "libsqlite3-dev",
            "libffi-dev",
            "liblzma-dev",
            "libncurses5-dev",
            "libncursesw5-dev",
            "xz-utils",
            "tk-dev",
            "virtualenv",
            # shared libraries many analysed projects' wheels load at run
            # time (OpenCV, PyQt, VTK/Open3D/PyVista, MuJoCo)
            "libgl1",
            "libglib2.0-0",
            "libxext6",
            "libsm6",
            "libxrender1",
        ),
        notes="uv from https://astral.sh/uv/install.sh; pyenv from https://github.com/pyenv/pyenv",
        install=(
            (
                "command -v uv >/dev/null 2>&1 || curl -LsSf "
                "https://astral.sh/uv/install.sh | env UV_INSTALL_DIR={home}/.local/bin "
                "INSTALLER_NO_MODIFY_PATH=1 sh"
            ),
            (
                "[ -d {home}/.pyenv/.git ] || git clone "
                "https://github.com/pyenv/pyenv.git {home}/.pyenv"
            ),
            (
                "[ -d {home}/.pyenv/plugins/pyenv-virtualenv ] || git clone "
                "https://github.com/pyenv/pyenv-virtualenv.git "
                "{home}/.pyenv/plugins/pyenv-virtualenv"
            ),
            # interactive shells get pyenv as the plugin's processes do
            "grep -q 'pyenv init --path' {home}/.bashrc 2>/dev/null || "
            "cat >> {home}/.bashrc <<'EOF'\n" + "\n".join(_SHELL_SETUP) + "\nEOF",
            " && ".join(f"pyenv install --skip-existing {v}" for v in _PYENV_PYTHONS),
            "uv python install " + " ".join(_UV_PYTHONS),
            (
                'for d in "$UV_PYTHON_INSTALL_DIR"/cpython-3.*-linux-*gnu*; do '
                "ver=$(basename \"$d\" | sed -E 's/cpython-([0-9]+\\.[0-9]+\\.[0-9]+).*/\\1/'); "
                'ln -sfn "$d" "$PYENV_ROOT/versions/$ver"; done && pyenv rehash'
            ),
        ),
        environment=(
            ("PYENV_ROOT", "{home}/.pyenv"),
            ("UV_PYTHON_INSTALL_DIR", "{home}/uv-pythons"),
        ),
        path=("{home}/.pyenv/bin", "{home}/.pyenv/shims"),
    )
}


def _analyzers(config: Config) -> dict:
    """memray and scalene, and the static network and parallelism analyses."""
    return {
        "analyzers": {Memray(config), Scalene(config)},
        "project_analyzers": {Network(config), Parallelism(config)},
    }


class Python3(Language):
    def __init__(self, config: Config, env_manager: EnvironmentManager | None = None):
        spec = LanguageSpec(
            name="python3",
            version="3",
            config=config,
            supported_env_managers={Uv(config)},
            **_TOOLCHAIN,
            **_analyzers(config),
        )
        super().__init__(spec, env_manager=env_manager)

    @override
    def covered_manifests(self) -> set[Path]:
        """The `Cargo.toml` a Rust-extension build backend (maturin,
        setuptools-rust) compiles as part of the Python build."""
        project = Path(self.spec.config.cwd or ".")
        try:
            with open(project / "pyproject.toml", "rb") as f:
                pyproject = tomllib.load(f)
        except FileNotFoundError:
            return set()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("cannot read %s: %s", project / "pyproject.toml", exc)
            return set()
        build = pyproject.get("build-system", {})
        requires = " ".join(str(r) for r in build.get("requires", []))
        backend = str(build.get("build-backend", ""))
        if (
            "maturin" not in backend + requires
            and "setuptools-rust" not in requires.replace("_", "-")
        ):
            return set()
        manifest = pyproject.get("tool", {}).get("maturin", {}).get("manifest-path")
        cargo = project / (manifest or "Cargo.toml")
        return {cargo} if cargo.is_file() else set()

    @override
    def is_present(self) -> bool:
        # NOTE the Adhoc manifest is present in every project, so the default
        # (any present manifest) would claim every project for Python
        return is_python_project(Path(self.spec.config.cwd or "."))

    # -- usage scripts ------------------------------------------

    def script_profile(self):
        return scripts.PYTHON_SCRIPT_PROFILE

    def check_script(self, path: Path):
        return scripts.check_script(path)

    def read_script(self, source: str, symbols, inferred_inputs=()):
        return scripts.read_script(source, symbols, inferred_inputs)

    # -- reading runs and documentation -------------------------

    def diagnose_run(self, output: str):
        return run_reading.diagnose_run(output)

    def standard_modules(self) -> frozenset[str]:
        return run_reading.standard_modules()

    def package_documentation(self, name: str = "", timeout: float | None = None):
        return package_docs.package_documentation(
            Path(self.spec.config.cwd or "."), name, timeout
        )
