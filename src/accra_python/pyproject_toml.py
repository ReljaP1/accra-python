import re
import tomllib
from pathlib import Path
from typing import Any, override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils


class Pyproject_toml(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="pyproject.toml", version="", config=config)
        super().__init__(spec)

        # TODO check the types
        self.project_section: dict[str, Any] | None = None
        self.tool_section: dict[str, Any] | None = None
        self.poetry_section: dict[str, Any] | None = None

    @override
    def detect(self) -> bool:
        pyproject_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            if not Path.exists(pyproject_path):
                return False

            with open(pyproject_path, "rb") as f:
                pyproject = tomllib.load(f)

                self.project_section = pyproject.get("project", {})
                self.tool_section = pyproject.get("tool", {})
                self.poetry_section = self.tool_section.get("poetry", {})

        except tomllib.TOMLDecodeError:
            return False

        except Exception:  # noqa: BLE001
            return False

        return True

    def _extract_PEP_621_dependencies(self) -> set[Dependency] | None:
        result: set[Dependency] = set()

        if not self.project_section:
            return None

        deps = self.project_section.get("dependencies", [])
        if not deps:
            return None

        for dep in deps:
            pkg = dep.split(";")[0].strip()
            pkg = re.split(r"[<>=~!]", pkg)  # [0].strip()

            pkg_name = pkg[0].strip()
            pkg_ver = pkg[1].strip() if len(pkg) > 2 else ""

            if pkg_name:
                result.add(Dependency(name=pkg_name, version=pkg_ver))

        return result

    def _extract_poetry_dependencies(self) -> set[Dependency] | None:
        result: set[Dependency] = set()

        if not self.poetry_section:
            return None

        deps = self.poetry_section.get("dependencies", {})
        if not deps:
            return None

        for dep in deps:
            if dep.lower() == "python":
                continue

            pkg = dep.split(";")[0].strip()
            pkg = re.split(r"[<>=~!]", pkg)  # [0].strip()

            pkg_name = pkg[0].strip()
            pkg_ver = pkg[1].strip() if len(pkg) > 2 else ""

            if pkg_name:
                result.add(Dependency(name=pkg_name, version=pkg_ver))

        return result

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        dependencies: set[Dependency] = set()

        pep_621_deps = self._extract_PEP_621_dependencies()
        if pep_621_deps:
            dependencies.update(pep_621_deps)

        poetry_deps = self._extract_poetry_dependencies()
        if poetry_deps:
            dependencies.update(poetry_deps)

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        requires_python = self.project_section.get("requires-python")
        return utils.get_python_minor_versions(requires_python or "")
