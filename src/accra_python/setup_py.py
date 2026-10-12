import re
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils


class Setup_py(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="setup.py", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        setuppy_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            return Path.exists(setuppy_path)
        except Exception:  # noqa: BLE001
            return False

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        dependencies: set[Dependency] = set()
        setuppy_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            in_install_requires = False
            with open(setuppy_path, "r", encoding="utf-8", errors="ignore") as f:
                #  Removes whitespace and skips empty lines or comments
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    if line.startswith("install_requires"):
                        if "[" in line:
                            in_install_requires = True
                            # Finds all string inside single or double-quoted
                            for dep_tuple in re.findall(
                                r"'([^']+)'|\"([^\"]+)\"", line
                            ):
                                dep = dep_tuple[0] or dep_tuple[1]
                                # Removes the version specifiers
                                pkg = re.split(r"[<>=~!]", dep)[0].strip()
                                if pkg:
                                    # TODO extract dependency version!
                                    dependencies.add(Dependency(name=pkg, version=""))

                            # If there is only one line
                            if "]" in line:
                                in_install_requires = False
                        continue

                    if in_install_requires:
                        if "]" in line:
                            in_install_requires = False
                        dep_match = re.match(r"['\"]([^'\"]+)['\"]", line)
                        if dep_match:
                            dep = dep_match.group(1)
                            pkg = re.split(r"[<>=~!]", dep)[0].strip()
                            if pkg:
                                # TODO extract dependency version!
                                dependencies.add(Dependency(name=pkg, version=""))

        except Exception:  # noqa: BLE001
            return None

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        setuppy_path: Path = self.spec.config.cwd / Path(self.spec.name)
        requires_python: str = ""

        try:
            with open(setuppy_path, "r", encoding="utf-8", errors="ignore") as f:
                #  Removes whitespace and skips empty lines or comments
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    match = re.search(
                        r'(?:python_requires|requires_python)\s*=\s*[\'"]([^\'"]+)[\'"]',
                        line,
                    )
                    if match:
                        requires_python = match.group(1)

        except Exception:  # noqa: BLE001, S110
            pass

        return utils.get_python_minor_versions(requires_python)
