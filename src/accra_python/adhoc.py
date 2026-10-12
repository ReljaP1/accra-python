import ast
import os
import subprocess
import sys
from collections.abc import Mapping
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils

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
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "jwt": "PyJWT",
    "attr": "attrs",
    "markdown_it": "markdown-it-py",
}

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


class Adhoc(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="adhoc", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        return True

    def _get_imported_modules(code: str) -> set[str]:
        """Return full dotted module names imported by the code (absolute imports only)."""
        modules: set[str] = set()

        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif (
                isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            ):  # skip relative imports
                modules.add(node.module)

        return modules

    def _resolve_package(
        module: str, installed: Mapping[str, list[str]] | None = None
    ) -> str | None:
        """Map a dotted import name to an installable package name."""
        top = module.split(".")[0]

        if top in sys.stdlib_module_names:  # py3.10+
            return None

        # explicit overrides, most specific match first
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            key = ".".join(parts[:i])
            if key in _ADHOC_PACKAGES:
                return _ADHOC_PACKAGES[key]

        # fall back to the import name itself
        return top

    def _get_canonical_packages_from_code(code: str) -> set[str]:
        """Returns a set of canonical package names extracted from `code`."""
        installed: Mapping[str, list[str]] = packages_distributions()
        packages: set[str] = {
            Adhoc._resolve_package(pkg, installed)
            for pkg in Adhoc._get_imported_modules(code)
            if pkg
        }

        return packages

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        dependencies: set[Dependency] = set()

        for root, dirs, files in os.walk(self.spec.config.cwd):
            dirs[:] = [d for d in dirs if d.lower() not in _EXCLUDED_SUBDIRS]

            for file in files:
                if file.endswith(".py"):
                    file_path: Path = Path(root) / Path(file)
                    try:
                        with open(
                            file_path, "r", encoding="utf-8", errors="ignore"
                        ) as f:
                            code: str = f.read()
                            pkgs: set[str] = Adhoc._get_canonical_packages_from_code(
                                code
                            )

                            dependencies.update(
                                {Dependency(name=pkg, version="") for pkg in pkgs}
                            )

                    except Exception:  # noqa: BLE001, S110
                        pass

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        requires_python: str = "3"

        result = subprocess.run(
            [
                "uv",
                "run",
                "vermin",
                "-f",
                "parsable",
                self.spec.config.cwd or Path("."),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if result.returncode != 0:
            # NOTE this is just vermin failing to find any Python
            # code, so we return an empty set
            return set()

        lines = result.stdout.splitlines()
        if not lines:
            return set()

        summary = [x for x in lines[-1].strip().split(":") if x]
        py3_version = summary[-1].strip("~")

        if py3_version.startswith("!"):
            return set()

        requires_python = py3_version or requires_python
        requires_python = ">=" + requires_python

        return utils.get_python_minor_versions(requires_python)
