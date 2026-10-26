import ast
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import override

from accra_language import (
    AccraError,
    AccraInstallError,
    Config,
    Dependency,
    Manifest,
    ManifestSpec,
)

from . import utils
from .import_names import IMPORT_NAMES
from .sources import MAX_SOURCE_BYTES

_ADHOC_PACKAGES: Mapping[str, str] = {
    "google.auth": "google-auth",
    "google.api_core": "google-api-core",
    "google.cloud.storage": "google-cloud-storage",
    "googleapiclient": "google-api-python-client",
    "pydantic_settings": "pydantic-settings",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "jwt": "PyJWT",
    "attr": "attrs",
    "markdown_it": "markdown-it-py",
    "google.protobuf": "protobuf",
}

# top-level names that are namespaces shared by many distributions: a
# dotted import under them is mapped by `IMPORT_NAMES` or not at all
_NAMESPACE_ONLY = frozenset({"google", "azure"})

# `IMPORT_NAMES` looked up ignoring case; the first entry of a name wins
_IMPORT_NAMES_LOWER: dict[str, str] = {}
for _name, _package in IMPORT_NAMES.items():
    _IMPORT_NAMES_LOWER.setdefault(_name.lower(), _package)


def mapped_package(name: str) -> str | None:
    """The installable name of the import `name` (a dotted prefix is not tried):
    `_ADHOC_PACKAGES` first, then `IMPORT_NAMES`."""
    return (
        _ADHOC_PACKAGES.get(name)
        or IMPORT_NAMES.get(name)
        or _IMPORT_NAMES_LOWER.get(name.lower())
    )


# Imports whose installable name differs from the import name. Each file
# yields the package of the LAST rule that matches (a substring test).
_ADHOC_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pydantic_settings", "from pydantic_settings"), "pydantic-settings"),
    (("import googleapiclient", "from googleapiclient"), "google-api-python-client"),
    (("import google.auth", "from google.auth"), "google-auth"),
    (("import google.api_core", "from google.api_core"), "google-api-core"),
)


def adhoc_package_rule(code: str) -> str | None:
    """The package of the last of the four name rules (`_ADHOC_RULES`) that
    matches `code`, else None."""
    proper_name: str | None = None
    for needles, package in _ADHOC_RULES:
        if any(needle in code for needle in needles):
            proper_name = package
    return proper_name


# Usage patterns that need a package (or an extra) the imports do not name,
# e.g. pydantic's EmailStr needs `pydantic[email]`.
_USAGE_PACKAGES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), package)
    for pattern, package in (
        (r"\bEmailStr\b", "pydantic[email]"),
        (r"\bSecretStr\b", "pydantic[email]"),
        (r"\bfrom\s+github\b", "PyGithub"),
        (r"\bimport\s+github\b", "PyGithub"),
        (r"\bimport\s+material\b", "mkdocs-material"),
        (r"\bfrom\s+material\b", "mkdocs-material"),
        (r"\bimport\s+PIL\b", "pillow"),
        (r"\bfrom\s+PIL\b", "pillow"),
        (r"\bimport\s+yaml\b", "pyyaml"),
        (r"\bfrom\s+yaml\b", "pyyaml"),
        (r"\bimport\s+uvicorn\b", "uvicorn"),
        (r"\bfrom\s+uvicorn\b", "uvicorn"),
        (r"\bimport\s+starlette\b", "starlette"),
        (r"\bfrom\s+starlette\b", "starlette"),
        (r"\bimport\s+python_multipart\b", "python-multipart"),
        (r"\bfrom\s+python_multipart\b", "python-multipart"),
        (r"\bimport\s+email_validator\b", "pydantic[email]"),
        (r"\bfrom\s+email_validator\b", "pydantic[email]"),
        (r"\bimport\s+orjson\b", "orjson"),
        (r"\bfrom\s+orjson\b", "orjson"),
        (r"\bForm\(", "python-multipart"),
        (r"\bOAuth2PasswordBearer\(", "python-jose"),
        (r"\bUploadFile\(", "python-multipart"),
        (r"\bDepends\(Security\(", "python-jose"),
        (r"\bJWT\(", "python-jose"),
        (r"\bCryptContext\(", "passlib"),
    )
)


def usage_packages(code: str) -> set[str]:
    """The packages that `code`'s usage patterns need (`_USAGE_PACKAGES`); a
    plain text scan, so it also works for code that does not parse."""
    return {package for pattern, package in _USAGE_PACKAGES if pattern.search(code)}


# vermin's message (exit code 1) when the path holds no Python file
_VERMIN_NO_FILES = "No files specified to analyze"

# Directories we never want to walk into when harvesting import-time
# dependencies — they belong to dev/test/build tooling rather than the
# package's runtime and would pull in noisy extras (pytest plugins etc).
_EXCLUDED_SUBDIRS: set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "tests",
    "test",
    "docs",
    "doc",
    "examples",
    "example",
    "benchmarks",
    "benchmark",
}

# File and directory names the flat/src layout scan of the project's own
# top-level modules skips.
_NOT_MODULES: set[str] = {
    "__init__",
    "setup",
    "conftest",
    "distribute_setup",
    "ez_setup",
}
_NOT_PACKAGE_DIRS: set[str] = {
    "tests",
    "test",
    "docs",
    "doc",
    "examples",
    "example",
    "__pycache__",
}

# Directories the whole-project text scans never enter: environments (the
# plugin's own and others') and caches hold no project code.
_SCAN_SKIPPED_DIRS: set[str] = {"__pycache__", "site-packages", "node_modules"}


def _own_top_level_packages(project: Path) -> list[str]:
    """The project's own top-level modules and packages at its root and in
    `src/`, lower-cased."""
    names: set[str] = set()
    for root, is_root in ((project, True), (project / "src", False)):
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.name.endswith(".py"):
                if entry.stem not in _NOT_MODULES:
                    names.add(entry.stem.lower())
            elif entry.is_dir():
                if is_root and entry.name in _NOT_PACKAGE_DIRS:
                    continue
                if (entry / "__init__.py").is_file():
                    names.add(entry.name.lower())
    return sorted(names)


def _try_body_node_ids(tree: ast.AST) -> set[int]:
    """ids of the nodes inside a `try:` body (not `except`, `else`, `finally`):
    imports there are optional."""
    optional: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in node.body:
                for descendant in ast.walk(child):
                    optional.add(id(descendant))
    return optional


class Adhoc(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="adhoc", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        return True

    def _get_imported_modules(code: str) -> set[str]:
        """Dotted module names that `code` imports at import time: absolute
        imports outside `try:` bodies (those are optional imports)."""
        modules: set[str] = set()
        tree = utils.safe_parse(code)
        if tree is None:
            raise SyntaxError("source cannot be parsed")
        optional = _try_body_node_ids(tree)

        for node in ast.walk(tree):
            if id(node) in optional:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif (
                isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            ):  # skip relative imports
                modules.add(node.module)

        return modules

    def _resolve_package(module: str) -> str | None:
        """Map a dotted import name to an installable package name; None for
        the standard library of any Python 3 version."""
        top = module.split(".")[0]

        if top in utils.standard_modules():
            return None

        # explicit overrides, most specific match first
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            key = ".".join(parts[:i])
            package = mapped_package(key)
            if package:
                return package

        # the PyPI packages `google` and `azure` are not the namespaces those
        # imports belong to (a web-search scraper, an empty meta-package)
        if top in _NAMESPACE_ONLY:
            return None

        # fall back to the import name itself
        return top

    # The names of imports in a file that does not parse: a regex fallback,
    # so a file that is not valid Python 3 still contributes.
    _IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][\w\.]*)", re.MULTILINE)
    _FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w\.]*)\s+import\s+", re.MULTILINE)

    def _get_canonical_packages_from_code(code: str) -> set[str]:
        """Returns a set of canonical package names extracted from `code`."""
        try:
            modules = Adhoc._get_imported_modules(code)
        except SyntaxError:
            modules = set(Adhoc._IMPORT_RE.findall(code)) | set(
                Adhoc._FROM_RE.findall(code)
            )
        resolved = (Adhoc._resolve_package(module) for module in modules)

        # stdlib modules resolve to None and are skipped, not turned into
        # Dependency(name=None), which would discard the file's other packages
        return {package for package in resolved if package}

    def _is_local_repo_module(project: Path, name: str, packages: list[str]) -> bool:
        """`name` is a module or package of the project: at its root or in
        `src/`, or inside one of its own top-level packages."""
        parts = name.split(".")
        roots = [project, project / "src"]

        def exists(base: Path) -> bool:
            target = base.joinpath(*parts)
            return (
                target.with_name(target.name + ".py").is_file()
                or (target / "__init__.py").is_file()
            )

        return any(exists(root) for root in roots) or any(
            exists(root / package) for package in packages for root in roots
        )

    def _file_bytes(self, path: Path) -> bytes | None:
        """The bytes of a project file, read once per `extract_dependencies`
        call: its two scans read the files of the project's own packages both.
        None when unreadable or over the source cap. The
        cache lives only for that call (see `extract_dependencies`), so a
        changed tree is always re-read."""
        cache = getattr(self, "_bytes", None)
        if cache is not None and path in cache:
            return cache[path]
        data: bytes | None
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                data = None
            else:
                data = path.read_bytes()
        except OSError:
            data = None
        if cache is not None:
            cache[path] = data
        return data

    def _import_time_dependencies(self) -> set[str]:
        """The packages the project's own top-level packages import at import
        time. Only the files of those packages are read (a package's whole
        tree minus `_EXCLUDED_SUBDIRS`, or the module file), imports in `try:`
        bodies and relative imports are skipped, and the project's own names
        are removed."""
        project = Path(self.spec.config.cwd or ".")
        packages = _own_top_level_packages(project)
        found: set[str] = set()

        def scan(path: Path) -> None:
            data = self._file_bytes(path)
            if data is None:
                return
            code = data.decode("utf-8", errors="ignore")
            found.update(Adhoc._get_canonical_packages_from_code(code))

        for package in packages:
            for root in (project, project / "src"):
                if (root / package / "__init__.py").is_file():
                    for dirpath, dirnames, filenames in os.walk(root / package):
                        dirnames[:] = [
                            d for d in dirnames if d.lower() not in _EXCLUDED_SUBDIRS
                        ]
                        for filename in filenames:
                            if filename.endswith(".py"):
                                scan(Path(dirpath) / filename)
                elif (root / f"{package}.py").is_file():
                    scan(root / f"{package}.py")

        return {
            name
            for name in found
            if name.lower() not in packages
            and not Adhoc._is_local_repo_module(project, name, packages)
        }

    def _scan_whole_project(self) -> tuple[set[str], set[str]]:
        """The packages of the four name rules (`_ADHOC_RULES`) and of the usage
        patterns (`_USAGE_PACKAGES`), over every `.py` file of the project
        that is not in an environment or cache."""
        named: set[str] = set()
        usage: set[str] = set()
        for root, dirs, files in os.walk(self.spec.config.cwd):
            dirs[:] = [
                d
                for d in dirs
                if d not in _SCAN_SKIPPED_DIRS
                and not (Path(root) / d / "pyvenv.cfg").is_file()
            ]
            for file in files:
                if not file.endswith(".py"):
                    continue
                data = self._file_bytes(Path(root) / file)
                if data is None:
                    continue
                try:
                    code = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                package = adhoc_package_rule(code)
                if package:
                    named.add(package)
                usage |= usage_packages(code)
        return named, usage

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        self._bytes: dict[Path, bytes | None] = {}
        try:
            named, usage = self._scan_whole_project()
            return self._dependencies_from(named, usage)
        finally:
            self._bytes = None  # type: ignore[assignment]

    def _dependencies_from(
        self, named: set[str], usage: set[str]
    ) -> set[Dependency] | None:

        # one dependency per package: `yaml` resolves to PyYAML and the usage
        # scan names pyyaml, which pip treats as the same
        by_name: dict[str, Dependency] = {}
        for package in sorted(self._import_time_dependencies()) + sorted(named | usage):
            by_name.setdefault(
                utils.canonical_name(package), Dependency(name=package, version="")
            )

        return set(by_name.values())

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        requires_python: str = "3"

        # NOTE vermin from the plugin's own interpreter (a dependency of
        # accra-python): `uv run vermin` would first sync a pyproject project,
        # writing .venv and uv.lock into it
        result = utils.run(
            [
                sys.executable,
                "-c",
                # vermin has no __main__; this is its console script
                "import sys, vermin; sys.argv[0] = 'vermin'; sys.exit(vermin.main())",
                "-f",
                "parsable",
                str(self.spec.config.cwd or Path(".")),
            ],
            self.spec.config,
        )

        if result.returncode != 0:
            if _VERMIN_NO_FILES in result.stdout + result.stderr:
                # no Python source: the code puts no limit on the version
                return utils.get_python_minor_versions(config=self.spec.config)

            # e.g. vermin or uv missing, or uv could not set up the run
            return utils.failure_error(
                AccraInstallError,
                "vermin could not determine the supported python versions",
                result,
            )

        lines = result.stdout.splitlines()
        if not lines:
            return utils.get_python_minor_versions(config=self.spec.config)

        # vermin's last line: `<py2>:<py3>:` (`!2` / `!3`: no such version runs it)
        columns = lines[-1].strip().split(":")
        marks = [x.strip("~") for x in columns if x]
        py3_version = marks[-1] if marks else ""
        if (
            len(marks) >= 2
            and marks[-2].startswith("!")
            and py3_version.startswith("!")
        ):
            # mixed Python 2 and 3 files: neither runs it all, so no constraint
            return utils.get_python_minor_versions(config=self.spec.config)
        if py3_version.startswith("!"):
            # the code does not run on any Python 3
            return set()

        requires_python = py3_version or requires_python
        requires_python = ">=" + requires_python

        return utils.get_python_minor_versions(requires_python, self.spec.config)
