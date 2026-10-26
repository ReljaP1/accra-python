"""Environment facts and entry points shared by the Python env managers (the
optional parts of the accra-language contract)."""

import csv
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from accra_language import (
    AccraBuildError,
    AccraError,
    AccraFailedDependencyInstallError,
    AccraInstallError,
    AccraResult,
    AccraRunError,
    Dependency,
    DockerfileInstruction,
    EntryPoint,
)

from . import utils
from .import_names import IMPORT_NAMES
from .sources import is_main_file, python_sources

# Directories that never hold the project's own entry points.
_EXCLUDED_DIRS: set[str] = {
    "__pycache__",
    "venv",
    "env",
    "build",
    "dist",
    "site-packages",
    "node_modules",
}


# A spec installed as written: VCS, URL or local archive or path.
_DIRECT_SPEC_RE = re.compile(r"^(?:(?:git|hg|svn|bzr)\+|https?://|file:)")


# the longest an import check may take: it only decides whether a package that
# installed also imports
_IMPORT_CHECK_TIMEOUT = 60.0

# install name (canonical) -> import name; the first entry of a name wins
_INSTALL_TO_IMPORT: dict[str, str] = {}
for _import, _install in IMPORT_NAMES.items():
    _INSTALL_TO_IMPORT.setdefault(utils.canonical_name(_install), _import)

# directory names that are never the project's primary package
_NOT_PRIMARY_NAMES = frozenset(
    {"tests", "test", "docs", "doc", "documentation", "examples", "example"}
)

_EXTENSION_SUFFIXES = (".so", ".pyd")
_NOT_IMPORT_NAMES = {"__pycache__", "bin", "scripts", "include", "share"}


def import_name_for(package: str) -> str | None:
    """The import name of an installable package, from
    `import_names.IMPORT_NAMES` reversed, None when the mapping does not know
    it."""
    return _INSTALL_TO_IMPORT.get(utils.canonical_name(package))


def is_location_spec(spec: str) -> bool:
    """A spec that names a place rather than an index package: a VCS or URL
    spec, `name @ url`, or a local path."""
    text = spec.strip()
    return bool(
        _DIRECT_SPEC_RE.match(text)
        or "://" in text
        or "@" in text
        or "/" in text
        or text.startswith(".")
    )


def _is_dotted_identifier(name: str) -> bool:
    return bool(name) and all(part.isidentifier() for part in name.split("."))


class PythonEnvironment:
    """Mixin for a Python `EnvironmentManager`; `_ENV_DIR` is the environment's
    directory relative to the project."""

    _ENV_DIR: str = ".venv"

    def _project(self) -> Path:
        return Path(self.spec.config.cwd or ".")

    def _python(self) -> str:
        return str(self.environment_path() / "bin" / "python")

    def _run_in_environment(self, command: list[str]):
        return utils.run(command, self.spec.config, utils.call_timeout(self))

    # -- environment facts ---------------------------------------------

    def environment_path(self) -> Path:
        return self._project() / self._ENV_DIR

    def run_environment(self, base_env: Mapping[str, str]) -> dict[str, str]:
        """The venv activated (`VIRTUAL_ENV`, `PATH`) and the project's import
        roots first on `PYTHONPATH`, so it also runs from source."""
        env_path = self.environment_path()
        env = dict(base_env)
        env["VIRTUAL_ENV"] = str(env_path)
        env["PATH"] = str(env_path / "bin") + os.pathsep + env.get("PATH", "")
        project = self._project().resolve()
        roots = [
            str(project if root == "." else project / root)
            for root in utils.import_roots(project)
        ]
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(roots + ([existing] if existing else []))
        return env

    # -- after a dependency install ---------------------

    def _site_packages(self) -> list[Path]:
        env_path = self.environment_path()
        found: list[Path] = []
        try:
            for pattern in ("lib/python*/site-packages", "lib64/python*/site-packages"):
                for site in sorted(env_path.glob(pattern)):
                    if site.is_dir() and site not in found:
                        found.append(site)
        except OSError:
            pass
        return found

    def _distribution_dir(self, name: str) -> Path | None:
        """The `.dist-info` (else `.egg-info`) directory of the installed
        distribution `name`, found by its canonical name; None when it is not
        installed. Read from the files, so it works on every Python."""
        wanted = utils.canonical_name(name)
        fallback = None
        for site in self._site_packages():
            try:
                entries = sorted(site.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.endswith(".dist-info"):
                    stem = entry.name[: -len(".dist-info")].rpartition("-")[0]
                elif entry.name.endswith(".egg-info") and entry.is_dir():
                    stem = entry.name[: -len(".egg-info")].split("-")[0]
                else:
                    continue
                if stem and utils.canonical_name(stem) == wanted:
                    if entry.name.endswith(".dist-info"):
                        return entry
                    fallback = fallback or entry
        return fallback

    @staticmethod
    def _distribution_version(info: Path) -> str | None:
        for metadata in ("METADATA", "PKG-INFO"):
            try:
                text = (info / metadata).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    break  # the headers end at the first blank line
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip() or None
        stem = info.name.rsplit(".", 1)[0]
        _, _, version = stem.partition("-")
        return version.split("-")[0] or None

    @staticmethod
    def _distribution_import_names(info: Path) -> list[str]:
        """The top-level modules and packages a distribution installed, from
        its `RECORD` (else `top_level.txt`): top-level directories holding
        Python code and top-level `.py` / extension modules, never the
        metadata, caches, scripts or `.pth` files."""
        names: list[str] = []

        def add(name: str) -> None:
            if (
                name not in names
                and name not in _NOT_IMPORT_NAMES
                and _is_dotted_identifier(name)
                and "." not in name
            ):
                names.append(name)

        try:
            record = (info / "RECORD").read_text(encoding="utf-8", errors="replace")
        except OSError:
            record = None
        if record is not None:
            code_dirs: set[str] = set()
            modules: list[str] = []
            for row in csv.reader(record.splitlines()):
                if not row or not row[0]:
                    continue
                parts = row[0].replace("\\", "/").split("/")
                if parts[0] in ("", "..", ".") or ".." in parts:
                    continue  # scripts and data installed outside site-packages
                top = parts[0]
                if top.endswith((".dist-info", ".egg-info", ".data", ".libs", ".pth")):
                    continue
                if len(parts) > 1:
                    if parts[-1].endswith((".py", *_EXTENSION_SUFFIXES)):
                        code_dirs.add(top)
                else:
                    if top.endswith(".py"):
                        modules.append(top[:-3])
                    elif top.endswith(_EXTENSION_SUFFIXES):
                        modules.append(top.split(".")[0])
            for name in sorted(code_dirs):
                add(name)
            for name in modules:
                add(name)
        else:
            try:
                listed = (info / "top_level.txt").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                listed = ""
            for name in listed.split():
                add(name.split("/")[0])
        return names

    def installed_version(self, dependency: Dependency) -> str | None:
        """The version of the dependency's distribution installed in the
        environment, None when it is not installed (also when an environment
        marker excluded it) or the name is a location."""
        if is_location_spec(dependency.name):
            return None
        try:
            info = self._distribution_dir(utils.dependency_base_name(dependency))
            return self._distribution_version(info) if info is not None else None
        except (subprocess.SubprocessError, OSError):
            return None

    def pinned_spec(self, dependency: Dependency) -> str:
        """The spec the Dockerfile installs for a dependency just installed:
        the name with `==<installed version>`; a location spec, or a dependency
        whose version cannot be read (or that was not installed: a false
        marker), stays as written."""
        spec = dependency.name + dependency.version
        try:
            version = self.installed_version(dependency)
        except (subprocess.SubprocessError, OSError):
            version = None
        if version is None:
            return spec
        # the extras stay (`pydantic[email]==2.5`); the specifier and the marker
        # were met by the install
        return re.split(r"[<>=!~;\s(]", dependency.name, maxsplit=1)[0] + (
            f"=={version}"
        )

    def import_check_error(self, dependency: Dependency) -> AccraError | None:
        """A failed-dependency error when an install that succeeded left a
        package that does not import. Not checked: a location spec, a
        distribution that is not installed (an environment marker excluded
        it), a package the check cannot decide, a check that fails to run."""
        if is_location_spec(dependency.name):
            return None
        try:
            if self.installed_version(dependency) is None:
                return None
            usable = self._is_importable(dependency)
        except (subprocess.SubprocessError, OSError):
            return None
        if usable is not False:
            return None
        return AccraFailedDependencyInstallError(
            message=(
                f"{self.spec.name} installed {dependency.name + dependency.version} "
                "but it does not import"
            ),
            dependency=dependency,
            environment_manager=self,
        )

    def ensure_pkg_resources(self) -> AccraError | None:
        """After the environment is created, repairs `pkg_resources` with
        `setuptools<82` (and `wheel`) when it does not import: setuptools 82
        removed it.
        A repair that fails, or leaves it unimportable, is a build failure."""
        python = self._python()
        if (
            self._run_in_environment([python, "-c", "import pkg_resources"]).returncode
            == 0
        ):
            return None
        repair = self._run_in_environment(
            [
                python,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "setuptools<82",
                "wheel",
                "--retries",
                utils.PIP_RETRIES,
                "--no-input",
            ]
        )
        if repair.returncode != 0:
            return utils.failure_error(
                AccraBuildError,
                "could not repair setuptools/pkg_resources in the environment",
                repair,
            )
        again = self._run_in_environment([python, "-c", "import pkg_resources"])
        if again.returncode != 0:
            return utils.failure_error(
                AccraBuildError,
                "setuptools is installed but pkg_resources is still not importable",
                again,
            )
        return None

    # -- the project itself ---------------------------

    def project_names(self) -> set[str]:
        """The project's top-level modules and packages, its distribution
        names, and what its editable install registered."""
        return utils.project_own_names(self._project()) | self._editable_names()

    def _editable_names(self) -> set[str]:
        """Distributions installed in editable mode, from their PEP 660
        `__editable__.<name>-<version>.pth` markers."""
        names: set[str] = set()
        try:
            for site in self.environment_path().glob("lib/python*/site-packages"):
                for marker in site.glob("__editable__.*.pth"):
                    payload = marker.name[len("__editable__.") : -len(".pth")]
                    # setuptools escapes the name (no hyphens), so the last
                    # hyphen separates it from the version
                    name, _, _version = payload.rpartition("-")
                    if (name or payload).strip():
                        names.add(utils.canonical_name((name or payload).strip()))
        except OSError:
            pass
        return names

    def _source_mode_lines(self) -> list[DockerfileInstruction]:
        workdir = getattr(self.spec.config, "image_workdir", "/project").rstrip("/")
        roots = utils.import_roots(self._project())
        pythonpath = ":".join(workdir if r == "." else f"{workdir}/{r}" for r in roots)
        return [DockerfileInstruction(f"ENV PYTHONPATH={pythonpath}")]

    def _has_project_install(self) -> bool:
        """A `pyproject.toml`, `setup.py` or `setup.cfg`."""
        project = self._project()
        return any(
            (project / name).is_file()
            for name in ("pyproject.toml", "setup.py", "setup.cfg")
        )

    def _primary_candidates(self) -> list[str]:
        """The project's own names that can be its primary package: never its
        tests, documentation or examples."""
        return sorted(
            name
            for name in self.project_names()
            if utils.canonical_name(name) not in _NOT_PRIMARY_NAMES
        )

    def _project_imports(self, candidates: Iterable[str]) -> bool:
        """Whether one of the primary candidates imports in the environment."""
        return any(
            self._is_importable(Dependency(name=name, version="")) is True
            for name in candidates
        )

    def post_dependencies_installation(self) -> AccraResult:
        """Installs the project: after `setuptools`, `pip install -e <project>`,
        else `pip install <project>`. A project without `pyproject.toml`,
        `setup.py` or `setup.cfg` (or whose install fails) runs from source, so
        its Dockerfile puts the import roots on PYTHONPATH.

        Python runs from source, so this never fails the build: an error is
        recorded only when the project ended up unusable, that is when none of
        its primary packages imports (an install that failed but left no
        primary package, or a project with nothing to import, is source mode,
        not an error). No error means the project is initialised."""
        # the user and `chown` are the composing caller's
        has_install = self._has_project_install()
        candidates = self._primary_candidates()
        result = AccraResult()
        failure: AccraError | None = None
        installed = False

        if has_install:
            # setuptools is a helper: its failure is not the project's
            try:
                setup = self._install_dependency(
                    Dependency(name="setuptools", version="")
                )
            except (subprocess.SubprocessError, OSError):
                setup = AccraResult()
            result.dockerfile.extend(setup.dockerfile)
            installed, failure = self._pip_install_project(result)
        if not installed:
            result.dockerfile.post_dependencies_installation.extend(
                self._source_mode_lines()
            )

        if self._project_imports(candidates) or (has_install and not candidates):
            return result
        reason = (
            failure.message
            if failure is not None
            else "no primary local package of the project imports"
        )
        result.errors.append(
            AccraInstallError(message=f"the project is not usable: {reason}")
        )
        return result

    def _pip_install_project(
        self, result: AccraResult
    ) -> tuple[bool, AccraError | None]:
        """`pip install -e`, else `pip install`, of the project; whether one
        worked (its Dockerfile line is added to `result`) and, if not, why."""
        project = self._project()
        env = dict(self.spec.config.env or os.environ)
        timeout = _positive_seconds(env.get("ACCRA_PROJECT_INSTALL_TIMEOUT"))
        capped = utils.call_timeout(self)
        if timeout is None or (capped is not None and capped < timeout):
            timeout = capped
        options = ["--retries", utils.PIP_RETRIES, "--no-input"]
        python = self._python()
        completed = None
        failure: AccraError | None = None
        for command in (
            [python, "-m", "pip", "install", "-e", str(project), *options],
            [python, "-m", "pip", "install", str(project), *options],
        ):
            try:
                completed = utils.run(command, self.spec.config, timeout)
            except (subprocess.SubprocessError, OSError) as exc:
                completed = None
                failure = AccraInstallError(
                    message=f"pip could not install the project: {exc}"
                )
                continue
            if completed.returncode == 0:
                result.dockerfile.post_dependencies_installation.append(
                    DockerfileInstruction("RUN python -m pip install --no-cache-dir .")
                )
                return True, None
        if completed is not None:
            failure = utils.failure_error(
                AccraInstallError, "pip could not install the project", completed
            )
        return False, failure

    def parse_dependency(self, spec: str) -> Dependency | AccraError:
        text = spec.strip()
        if _DIRECT_SPEC_RE.match(text):
            return Dependency(name=text, version="")
        dependency = utils.requirement_dependency(text)
        if dependency is None:
            return AccraInstallError(message=f"not a Python requirement: {spec!r}")
        # A bare dotted name (no version, extras or marker) is an import path
        # (`scipy.stats`): it names its top-level package, never a guessed
        # distribution. A requirement keeps its full name (`ruamel.yaml>=0.17`).
        if "." in dependency.name and not dependency.version and "[" not in text:
            return Dependency(name=dependency.name.split(".")[0], version="")
        return dependency

    def installed_dependencies(self) -> set[Dependency] | AccraError:
        listed = self._run_in_environment(
            [self._python(), "-m", "pip", "list", "--format=json"]
        )
        if listed.returncode != 0:
            return utils.failure_error(
                AccraInstallError, "pip could not list the installed packages", listed
            )
        try:
            packages = json.loads(listed.stdout or "[]")
            return {
                Dependency(name=str(p["name"]), version=f"=={p['version']}")
                for p in packages
            }
        except (ValueError, KeyError, TypeError) as exc:
            return AccraInstallError(message=f"unreadable pip list output: {exc}")

    def uninstall_dependencies(self, dependencies: Iterable[Dependency]) -> AccraResult:
        names = sorted({utils.dependency_base_name(d) for d in dependencies})
        if not names:
            return AccraResult()
        removed = self._run_in_environment(
            [self._python(), "-m", "pip", "uninstall", "-y", *names]
        )
        if removed.returncode != 0:
            return AccraResult(
                errors=[
                    utils.failure_error(
                        AccraInstallError, f"pip could not uninstall {names}", removed
                    )
                ],
                success=False,
            )
        return AccraResult()

    def _import_timeout(self) -> float:
        base = utils.call_timeout(self)
        if base is None:
            base = getattr(self.spec.config, "timeout", None)
        return min(_IMPORT_CHECK_TIMEOUT, base) if base else _IMPORT_CHECK_TIMEOUT

    def _is_importable(
        self, dependency: Dependency, extra_names: Iterable[str] = ()
    ) -> bool | AccraError:
        """Whether one of the dependency's import names imports: the name, its
        underscore form, or a top-level module the installed distribution ships
        (read from its `RECORD`). A name with no installed distribution is still
        tried as is (a local package from source); an
        installed distribution that ships no importable name, or whose check
        cannot run, is undecidable (an error)."""
        name = re.split(r"[\[<>=!~;@\s(]", dependency.name, maxsplit=1)[0]
        info = self._distribution_dir(utils.canonical_name(name))
        shipped = self._distribution_import_names(info) if info is not None else []
        seen: set[str] = set()
        undecided = False
        for candidate in [name, name.replace("-", "_").lower(), *shipped, *extra_names]:
            if candidate in seen or not _is_dotted_identifier(candidate):
                continue
            seen.add(candidate)
            try:
                imported = utils.run(
                    [self._python(), "-c", f"import {candidate}"],
                    self.spec.config,
                    self._import_timeout(),
                )
            except (subprocess.SubprocessError, OSError):
                undecided = True
                continue
            if imported.returncode == 0:
                return True
        if undecided or (info is not None and not shipped):
            return AccraRunError(
                message=f"cannot tell whether {name} imports: no import name found"
            )
        return False

    def already_importable(self, dependency: Dependency) -> bool:
        """Whether a package imports in the environment from the project as it
        is, so that installing it would add nothing (or shadow a local module,
        e.g. a root directory `plot/` without `__init__.py`): `import
        <candidate>` with the environment's interpreter, for the import name
        the mapping gives, the install name, its underscore form and the base
        name; a version constraint is not looked at. Not checked: a location
        spec; a check that cannot decide or run."""
        if is_location_spec(dependency.name):
            return False
        base = re.split(r"[\[<>=!~;@\s(]", dependency.name, maxsplit=1)[0]
        mapped = import_name_for(base)
        try:
            usable = self._is_importable(dependency, [mapped] if mapped else ())
        except (subprocess.SubprocessError, OSError):
            return False
        return usable is True

    # -- running ---------------------------------------------------------

    def entry_point_for(self, path: Path):
        """A file inside a package runs as a module (`python -m pkg.run`, from
        the project directory), any other file as a script."""
        project = self._project()
        path = Path(path)
        if not path.is_absolute():
            path = project / path
        python = self._python()
        module = module_name(path, project)
        if module and (path.parent / "__init__.py").exists():
            return EntryPoint(
                name=module, kind="module", command=(python, "-m", module), path=path
            )
        return EntryPoint(
            name=str(path), kind="script", command=(python, str(path)), path=path
        )

    def entry_points(self) -> set | AccraError:
        """The files ACCRa runs as the project's entry points: those that take
        input (a main guard, argparse, sys.argv or input()), apart from
        packaging and test helpers and tests/docs directories
        (`sources.is_main_file`). Each is run as `entry_point_for` says (a
        module inside a package)."""
        entries = set()
        for path, content, imports in python_sources(self._project()):
            if is_main_file(path, content, imports):
                entry = self.entry_point_for(path)
                if not isinstance(entry, AccraError):
                    entries.add(entry)
        return entries


def _positive_seconds(value: str | None) -> float | None:
    """A positive number of seconds from text, None for anything else."""
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 < seconds < float("inf") else None


def module_name(path: Path, project: Path) -> str | None:
    """The dotted module name of a `.py` file under `project` (`pkg/run.py`
    -> `pkg.run`; a package's `__init__.py` names the package), else None."""
    try:
        relative = Path(path).resolve().relative_to(Path(project).resolve())
    except (OSError, ValueError):
        return None
    if relative.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def is_python_project(project: Path) -> bool:
    """Read-only check for Python code: a Python manifest, or a `.py`
    file outside environments and caches."""
    manifests = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((project / name).is_file() for name in manifests):
        return True
    try:
        for root, dirs, files in os.walk(project):
            dirs[:] = [
                d for d in dirs if d not in _EXCLUDED_DIRS and not d.startswith(".")
            ]
            if any(file.endswith(".py") for file in files):
                return True
    except OSError:
        return False
    return False
