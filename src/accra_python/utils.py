import ast
import configparser
import os
import re
import shlex
import subprocess
import sys
import tomllib
from functools import cache
from pathlib import Path

from accra_language import (
    AccraError,
    AccraInstallError,
    Config,
    Dependency,
    DockerfileInstruction,
)
from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

# NOTE pyenv cannot install minor version < 5 on Debian
_MIN_MINOR_VERSION = 5
_MAX_MINOR_VERSION = 14

# Comma-separated Python minor versions (e.g. "3.8,3.9,3.12") a caller can
# offer, read from `Config.env` (else the process environment). Unset, every
# version from 3.5 to 3.14 is a candidate.
PYTHON_VERSIONS_ENV = "ACCRA_PYTHON_VERSIONS"


def _python_version_universe(config: Config | None) -> tuple[str, ...]:
    """The Python 3 minor versions that may be chosen, from `PYTHON_VERSIONS_ENV`
    when it names any, else 3.5 to 3.14."""
    env = (config.env if config is not None else None) or os.environ
    configured: tuple[str, ...] = tuple(
        version
        for version in re.split(r"[\s,]+", env.get(PYTHON_VERSIONS_ENV, ""))
        if re.fullmatch(r"3\.\d+", version)
        and int(version.split(".")[1]) >= _MIN_MINOR_VERSION
    )
    if configured:
        return tuple(sorted(set(configured), key=_version_key))

    return tuple(f"3.{i}" for i in range(_MIN_MINOR_VERSION, _MAX_MINOR_VERSION + 1))


@cache
def _versions_allowed(requires_python: str, universe: tuple[str, ...]) -> frozenset:
    try:
        specifier = SpecifierSet(requires_python)
        return frozenset(v for v in universe if specifier.contains(Version(v)))
    except (InvalidSpecifier, InvalidVersion, TypeError, ValueError):
        # an unreadable constraint is no constraint
        return frozenset(universe)


def get_python_minor_versions(
    requires_python: str = ">=0", config: Config | None = None
) -> set[str]:
    """The candidate Python minor versions that satisfy `requires_python`."""
    return set(_versions_allowed(requires_python, _python_version_universe(config)))


def safe_parse(source: str, filename: str = "<unknown>") -> ast.Module | None:
    """`ast.parse(source)`, None when the source cannot be parsed: a syntax
    error, a null byte, or code nested so deeply that the parser runs out of
    stack or memory."""
    try:
        return ast.parse(source, filename)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def run(
    command: list[str],
    config: Config,
    timeout: float | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Runs a command for the project described by `config`.

    `timeout` defaults to `config.timeout`; env managers pass their
    `subprocess_timeout()` so the whole-build budget caps every call.
    `env` replaces `config.env`, `extra_env` is added to it (or to the process
    environment), and `cwd` replaces `config.cwd`."""
    if env is None:
        env = config.env
    if extra_env:
        env = {**(env if env is not None else os.environ), **extra_env}
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        cwd=config.cwd if cwd is None else cwd,
        env=env,
        shell=config.shell,
        timeout=config.timeout if timeout is None else timeout,
    )


PIP_RETRIES = "5"


def uv_timeout_env(config: Config) -> dict[str, str] | None:
    """`UV_HTTP_TIMEOUT` from `PIP_TIMEOUT`, so uv's downloads are bounded,
    and `UV_HTTP_RETRIES=5`; each left out when already set."""
    env = config.env if config.env is not None else os.environ
    timeout = env.get("PIP_TIMEOUT")
    extra: dict[str, str] = {}
    if timeout and not env.get("UV_HTTP_TIMEOUT"):
        extra["UV_HTTP_TIMEOUT"] = timeout
    if not env.get("UV_HTTP_RETRIES"):
        extra["UV_HTTP_RETRIES"] = PIP_RETRIES
    return extra


def call_timeout(env_manager) -> float | None:
    """The timeout for `env_manager`'s next subprocess: its
    `subprocess_timeout()`, `Config.timeout` capped by the whole-build budget."""
    return env_manager.subprocess_timeout()


_STDERR_TAIL_CHARS = 2000


def failure_message(message: str, completed: subprocess.CompletedProcess[str]) -> str:
    """`message` with the exit code and the last line of stderr."""
    lines: list[str] = (completed.stderr or "").strip().splitlines()
    detail: str = f": {lines[-1].strip()}" if lines else ""
    return f"{message} (exit code {completed.returncode}{detail})"


def failure_fields(completed: subprocess.CompletedProcess[str]) -> dict:
    """The `AccraError` fields describing a failed command: `command`,
    `exit_code` and the tail of `stderr`."""
    command = completed.args
    return {
        "command": command if isinstance(command, str) else [str(c) for c in command],
        "exit_code": completed.returncode,
        "stderr": (completed.stderr or "")[-_STDERR_TAIL_CHARS:] or None,
    }


def failure_error(
    error_class: type[AccraError],
    message: str,
    completed: subprocess.CompletedProcess[str],
    **fields,
) -> AccraError:
    """An `error_class` for a failed command, with its exit code and stderr in
    the message and in the error's fields."""
    return error_class(
        message=failure_message(message, completed),
        **failure_fields(completed),
        **fields,
    )


def pyenv_project_setup(
    python_version: str, config: Config, timeout: float | None = None
) -> AccraError | None:
    pyenv_result = run(
        ["pyenv", "install", "--skip-existing", python_version], config, timeout
    )

    if pyenv_result.returncode != 0:
        return failure_error(
            AccraInstallError,
            f"pyenv could not install python version: {python_version}",
            pyenv_result,
        )

    # NOTE no `pyenv local`: it writes .python-version into the analyzed
    # project. Commands that need this interpreter select it themselves
    # (`uv venv --python`, or PYENV_VERSION for `python -m venv`).


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric sort key of a version such as `3.10`; non-numeric parts sort lowest."""
    return tuple(int(part) if part.isdigit() else -1 for part in version.split("."))


def highest_version(versions: set[str]) -> str:
    """The highest version, ordered numerically ("3.10" > "3.9")."""
    return max(versions, key=lambda version: (_version_key(version), version))


# Stable modern versions first, then the others, highest first.
PREFERRED_PYTHON_VERSIONS: tuple[str, ...] = (
    "3.11",
    "3.10",
    "3.9",
    "3.8",
    "3.7",
    "3.12",
    "3.6",
)


def version_order(versions: set[str]) -> list[str]:
    """`versions` most preferred first: each of `PREFERRED_PYTHON_VERSIONS` in
    that order (its patch versions highest first), then the rest, highest
    first."""
    ordered: list[str] = []
    for preferred in PREFERRED_PYTHON_VERSIONS:
        matches = {
            v for v in versions if v == preferred or v.startswith(preferred + ".")
        }
        ordered.extend(sorted(matches, key=_descending))
    rest = versions - set(ordered)
    return ordered + sorted(rest, key=_descending)


def _descending(version: str) -> tuple[tuple[int, ...], str]:
    key = _version_key(version)
    return tuple(-part for part in key), version


_PATCH_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def installed_patch(version: str, config: Config, timeout: float | None = None) -> str:
    """A minor version (`3.11`) resolved to the highest installed pyenv patch
    (`3.11.12`), so `pyenv install --skip-existing` does not compile CPython.
    Unchanged when it has a patch already or none is installed."""
    if not re.fullmatch(r"\d+\.\d+", version):
        return version
    try:
        listed = run(["pyenv", "versions", "--bare"], config, timeout)
    except (OSError, subprocess.SubprocessError):
        return version
    if listed.returncode != 0:
        return version
    matches = {
        v
        for v in (listed.stdout or "").split()
        if _PATCH_VERSION_RE.fullmatch(v) and v.startswith(version + ".")
    }
    return highest_version(matches) if matches else version


# -- Dockerfile parts -------------------------------------------------
#
# The caller composes the image: it chooses the base, clones the project into
# `Config.image_workdir`, makes it the working directory and creates and
# switches the user. These are the Python plugin's own parts.

# uv into /usr/local/bin: the installer's default, ~/.local/bin, is not on
# PATH; skipped when an earlier part installed it already.
DOCKER_INSTALL_UV = DockerfileInstruction(
    "RUN command -v uv >/dev/null 2>&1 || (curl -LsSf https://astral.sh/uv/install.sh"
    " | UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh)"
)

# What Python packages commonly need to build and run on Debian: the build
# toolchain and common native libraries.
_DOCKER_NATIVE_PACKAGES: tuple[str, ...] = (
    "curl",
    "build-essential",
    "gcc",
    "g++",
    "make",
    "libffi-dev",
    "libssl-dev",
    "zlib1g-dev",
    "libbz2-dev",
    "libreadline-dev",
    "libsqlite3-dev",
    "liblzma-dev",
    "libgl1",
    "libglib2.0-0",
    "libxext6",
    "libsm6",
    "libxrender1",
)
DOCKER_NATIVE_PACKAGES = DockerfileInstruction(
    "RUN apt-get update && apt-get install -y " + " ".join(_DOCKER_NATIVE_PACKAGES)
)


# Where the image keeps the interpreters it installs: readable by `appuser`,
# since the steps run as root and a venv links to its interpreter.
DOCKER_UV_PYTHON_DIR = "/opt/uv-python"
DOCKER_PYENV_ROOT = "/opt/pyenv"

# uv installs prebuilt CPythons from 3.8 on; older ones are compiled with pyenv.
_FIRST_PREBUILT_MINOR = 8

# What compiling CPython needs on a bare Debian image.
_DOCKER_PYENV_BUILD_PACKAGES: tuple[str, ...] = (
    "build-essential",
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
)


def docker_builds_python_from_source(version: str) -> bool:
    """Whether the image compiles Python `version` with pyenv (no prebuilt
    CPython of it exists) instead of installing uv's."""
    parts = version.split(".")
    return (
        len(parts) >= 2
        and parts[0] == "3"
        and parts[1].isdigit()
        and int(parts[1]) < _FIRST_PREBUILT_MINOR
    )


def docker_python_toolchain(version: str) -> list[DockerfileInstruction]:
    """Installs Python `version` on the caller's bare Debian image (`curl`,
    `git` and CA certificates exist, compilers do not): uv, then either uv's
    prebuilt CPython or, below 3.8, one compiled with pyenv and put on PATH as
    `python3.<minor>`."""
    steps = [
        DOCKER_INSTALL_UV,
        DockerfileInstruction(f"ENV UV_PYTHON_INSTALL_DIR={DOCKER_UV_PYTHON_DIR}"),
    ]
    if not docker_builds_python_from_source(version):
        steps.append(DockerfileInstruction(f"RUN uv python install {version}"))
        return steps

    pyenv = f"PYENV_ROOT={DOCKER_PYENV_ROOT} {DOCKER_PYENV_ROOT}/bin/pyenv"
    minor = ".".join(version.split(".")[:2])
    steps += [
        DockerfileInstruction(
            "RUN apt-get update && apt-get install -y "
            + " ".join(_DOCKER_PYENV_BUILD_PACKAGES)
        ),
        DockerfileInstruction(
            f"RUN [ -d {DOCKER_PYENV_ROOT}/.git ] || git clone --depth 1 "
            f"https://github.com/pyenv/pyenv.git {DOCKER_PYENV_ROOT}"
        ),
        DockerfileInstruction(f"RUN {pyenv} install --skip-existing {version}"),
        DockerfileInstruction(
            f'RUN ln -sf "$({pyenv} prefix {version})/bin/python" '
            f"/usr/local/bin/python{minor}"
        ),
    ]
    return steps


def docker_activate_environment(
    env_dir: str, workdir: str = "/project"
) -> list[DockerfileInstruction]:
    """Activates the venv `env_dir` (relative to the project directory
    `workdir`) for every later instruction and for the container, so `python`
    is the environment's interpreter."""
    env_path = f"{workdir.rstrip('/')}/{env_dir}"
    return [
        DockerfileInstruction(f"ENV VIRTUAL_ENV={env_path}"),
        DockerfileInstruction(f'ENV PATH="{env_path}/bin:$PATH"'),
    ]


def docker_install(command: str, spec: str) -> DockerfileInstruction:
    """`RUN <command> <spec>` with the spec shell-quoted (specifier characters
    such as `>` and `;` are shell-active)."""
    return DockerfileInstruction(f"RUN {command} {shlex.quote(spec)}")


# A directory name that goes into `PYTHONPATH` (a `:`-separated list) and into a
# Dockerfile `ENV` line unquoted: whitespace, control characters, the separator,
# `$` and quoting characters would break or extend either.
_UNSAFE_ROOT_NAME_RE = re.compile(r"[\s\x00-\x1f\x7f:$\"'`\\;]")


def is_safe_root_name(name: str) -> bool:
    """Whether a directory name can be an import root."""
    return bool(name) and not _UNSAFE_ROOT_NAME_RE.search(name)


def import_roots(project: Path) -> list[str]:
    """Directories (relative to `project`) that must be on PYTHONPATH to run the
    project from source: the project itself, and each top-level directory with
    Python files that is not a package. A package directory is left out: its
    submodules would become top-level modules and could shadow the stdlib
    (PiNN's `pinn/pinn/io/` shadows `io`)."""
    roots = ["."]
    try:
        entries = sorted(project.iterdir())
    except OSError:
        return roots
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not is_safe_root_name(entry.name):
            continue
        if (entry / "__init__.py").is_file():
            continue
        try:
            if any(child.suffix == ".py" for child in entry.iterdir()):
                roots.append(entry.name)
        except OSError:
            continue
    return roots


def canonical_name(name: str) -> str:
    """PEP 503 normalised form of a distribution or import name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_dependency(text: str) -> Dependency | None:
    """A Dependency from a PEP 508 requirement string, None if it is not one.

    `name` keeps the extras and `version` the specifier with its operator, plus
    any environment marker (`; python_version < "3.8"`), so `name + version` is
    a valid install spec that uv evaluates against the target environment.
    A direct reference (`pkg @ https://...`) is kept whole in `name`."""
    try:
        requirement = Requirement(text)
    except InvalidRequirement:
        return None
    if requirement.name.startswith("-"):
        return None

    if requirement.url:
        return Dependency(name=text.strip(), version="")

    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    version = str(requirement.specifier)
    if requirement.marker:
        version += f"; {requirement.marker}"

    return Dependency(name=requirement.name + extras, version=version)


def _poetry_bound(version: str, caret: bool) -> str | None:
    """`^version` (caret) or `~version` as a PEP 440 range, None if it is not
    a plain release."""
    parts = version.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    numbers = [int(p) for p in parts]
    if caret:
        # the first non-zero part may not change; all zeros: the last part
        index = next((i for i, n in enumerate(numbers) if n), len(numbers) - 1)
    else:
        index = 0 if len(numbers) == 1 else 1
    upper = [*numbers[:index], numbers[index] + 1]
    upper += [0] * (max(len(numbers), 2) - len(upper))
    return f">={version},<{'.'.join(str(n) for n in upper)}"


def poetry_specifier(constraint: str) -> str:
    """A Poetry version constraint as a PEP 440 specifier: `^`, `~`
    and bare versions converted, comparisons and wildcards kept; "" for any
    version or a constraint PEP 440 cannot express (e.g. `||`)."""
    text = (constraint or "").strip()
    if not text or text == "*" or "||" in text:
        return ""
    specifiers = []
    for part in (p.strip() for p in text.split(",")):
        if part.startswith("^") or (part.startswith("~") and not part.startswith("~=")):
            bound = _poetry_bound(part[1:].strip(), caret=part.startswith("^"))
            if bound is None:
                return ""
            specifiers.append(bound)
        elif part[:1].isdigit():
            specifiers.append(f"=={part}")
        else:
            specifiers.append(part.replace(" ", ""))
    specifier = ",".join(specifiers)
    try:
        SpecifierSet(specifier)
    except InvalidSpecifier:
        return ""
    return specifier


def poetry_dependency(name: str, spec) -> Dependency | None:
    """A `[tool.poetry.dependencies]` entry as a Dependency, None for
    `python`, an optional dependency (installed only with an extra) or a local
    path (the project's own code)."""
    if not name or name.lower() == "python" or name.startswith("-"):
        return None
    if isinstance(spec, list):  # multiple constraints: Poetry's first applies
        spec = spec[0] if spec else ""
    if isinstance(spec, dict):
        if spec.get("optional") or "path" in spec:
            return None
        for source in ("git", "url"):
            if source in spec:
                reference = spec[source]
                if source == "git":
                    revision = spec.get("rev") or spec.get("tag") or spec.get("branch")
                    reference = f"git+{reference}" + (
                        f"@{revision}" if revision else ""
                    )
                return Dependency(name=f"{name} @ {reference}", version="")
        extras = spec.get("extras") or []
        constraint = spec.get("version", "")
    else:
        extras, constraint = [], str(spec)
    suffix = f"[{','.join(sorted(extras))}]" if extras else ""
    return Dependency(name=name + suffix, version=poetry_specifier(str(constraint)))


_NOT_PROJECT_MODULES: set[str] = {
    "__init__",
    "setup",
    "conftest",
    "distribute_setup",
    "ez_setup",
}


def _local_top_level_names(cwd: Path) -> set[str]:
    """Top-level modules and packages of the project, at its root and in src/."""
    names: set[str] = set()

    for root in (cwd, cwd / "src"):
        if not root.is_dir():
            continue

        for entry in root.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".py":
                if entry.stem not in _NOT_PROJECT_MODULES:
                    names.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                names.add(entry.name)

    return names


def _distribution_names(cwd: Path) -> set[str]:
    """Names the project itself is distributed under, from its manifests."""
    names: set[str] = set()

    try:
        with open(cwd / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        names.add(pyproject.get("project", {}).get("name", ""))
        names.add(pyproject.get("tool", {}).get("poetry", {}).get("name", ""))
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        setup_cfg = configparser.ConfigParser(interpolation=None)
        setup_cfg.read(cwd / "setup.cfg", encoding="utf-8")
        names.add(setup_cfg.get("metadata", "name", fallback=""))
    except configparser.Error:
        pass

    try:
        setup_py = (cwd / "setup.py").read_text(encoding="utf-8", errors="ignore")
        setup_name = setup_call_name(setup_py)
        if setup_name:
            names.add(setup_name)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        pass

    return {name for name in names if isinstance(name, str) and name}


def setup_call_name(source: str) -> str | None:
    """The `name=` keyword (a string literal) of `setup(...)` in a setup.py
    source, read with `ast`; None when there is none or it does not parse."""
    tree = safe_parse(source)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called != "setup":
            continue
        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg == "name"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value.strip()
            ):
                return value.value.strip()
    return None


def project_own_names(cwd: Path | None) -> set[str]:
    """Canonical names that refer to the project itself, never to a dependency:
    its top-level modules and packages, and its distribution names."""
    if cwd is None:
        return set()

    cwd = Path(cwd)
    return {
        canonical_name(name)
        for name in _local_top_level_names(cwd) | _distribution_names(cwd)
    }


def dependency_base_name(dependency: Dependency) -> str:
    """Canonical distribution name of a dependency, without extras or specifier."""
    return canonical_name(re.split(r"[\[<>=!~;@\s(]", dependency.name, maxsplit=1)[0])


def _marker_of(dependency: Dependency) -> str:
    """The environment marker of a dependency (`; python_version < "3.8"`),
    empty when it has none."""
    return dependency.version.partition(";")[2].strip()


def _marker_applies(marker: str, python_version: str) -> bool:
    """Whether the environment marker holds for `python_version`; a marker that
    cannot be read counts as holding."""
    parts = python_version.split(".")
    environment = {
        "python_version": ".".join(parts[:2]),
        "python_full_version": ".".join((parts + ["0", "0"])[:3]),
    }
    try:
        return Marker(marker).evaluate(environment)
    except Exception:  # noqa: BLE001
        return True


def without_inapplicable(
    dependencies: set[Dependency], python_version: str | None
) -> set[Dependency]:
    """`dependencies` without the packages whose every environment marker is
    false for `python_version` (a package that is also listed without a marker,
    or whose marker cannot be read, stays). Without a version nothing is
    dropped."""
    if not python_version:
        return dependencies
    names_with_marker: dict[str, list[bool]] = {}
    for dependency in dependencies:
        marker = _marker_of(dependency)
        applies = _marker_applies(marker, python_version) if marker else True
        names_with_marker.setdefault(dependency_base_name(dependency), []).append(
            applies
        )
    dropped = {name for name, applies in names_with_marker.items() if not any(applies)}
    return {d for d in dependencies if dependency_base_name(d) not in dropped}


def project_dependencies(
    dependencies: set[Dependency],
    cwd: Path | None,
    python_version: str | None = None,
) -> set[Dependency]:
    """`dependencies` without the project itself, without redundant bare names
    and, given `python_version`, without the packages whose environment markers
    are all false for it (`without_inapplicable`).

    Imports of the project's own modules and packages, and its own distribution
    name, are not dependencies: installing them would pull an unrelated package
    of the same name from the index (or fail as a self-dependency). A bare name
    (e.g. from an import) adds nothing when a manifest declares the same package
    with an unconditional version constraint; a conditional one (an environment
    marker) leaves it."""
    own_names: set[str] = project_own_names(cwd)
    dependencies = {
        dependency
        for dependency in dependencies
        if dependency_base_name(dependency) not in own_names
        and not dependency.name.lstrip().startswith("-")
    }
    dependencies = without_inapplicable(dependencies, python_version)

    constrained: set[str] = {
        dependency_base_name(dependency)
        for dependency in dependencies
        if dependency.version and not _marker_of(dependency)
    }
    return {
        dependency
        for dependency in dependencies
        if dependency.version or dependency_base_name(dependency) not in constrained
    }


# Modules the standard library held in a Python 3 version the plugin builds
# (3.5 and later) and removed since: still never packages to install for a
# project that imports them.
_REMOVED_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "fpectl",  # 3.7
        "macpath",  # 3.8
        "_dummy_thread",  # 3.9
        "dummy_threading",
        "formatter",  # 3.10
        "parser",
        "symbol",
        "binhex",  # 3.11
        "asynchat",  # 3.12
        "asyncore",
        "distutils",
        "imp",
        "smtpd",
        "aifc",  # 3.13 (PEP 594)
        "audioop",
        "cgi",
        "cgitb",
        "chunk",
        "crypt",
        "imghdr",
        "lib2to3",
        "mailcap",
        "msilib",
        "nis",
        "nntplib",
        "ossaudiodev",
        "pipes",
        "sndhdr",
        "spwd",
        "sunau",
        "telnetlib",
        "uu",
        "xdrlib",
    }
)


def standard_modules() -> frozenset[str]:
    """The top-level modules of the standard library of any Python 3 version
    the plugin builds: this interpreter's, plus those removed since 3.5. The
    project's own version may lack some, but none is ever a package to install."""
    return frozenset(getattr(sys, "stdlib_module_names", ())) | _REMOVED_STDLIB_MODULES
