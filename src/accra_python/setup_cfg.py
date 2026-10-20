import configparser
import logging
import re
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils

logger = logging.getLogger(__name__)


class Setup_cfg(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="setup.cfg", version="", config=config)
        super().__init__(spec)

        self.install_requires: str | None = None
        self.python_requires: str | None = None

    @override
    def detect(self) -> bool:
        setupcfg_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            if not Path.exists(setupcfg_path):
                return False

            parser = configparser.ConfigParser(interpolation=None)
            parser.read(setupcfg_path)

            if parser.has_section("options") and parser.has_option(
                "options", "install_requires"
            ):
                self.install_requires = parser.get("options", "install_requires")

            if parser.has_section("options") and parser.has_option(
                "options", "python_requires"
            ):
                self.python_requires = parser.get("options", "python_requires")

            return True

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cannot read %s: %s: %s", self.spec.name, type(exc).__name__, exc
            )
            return False

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        dependencies: set[Dependency] = set()

        if not self.install_requires:
            return None

        for dep in self.install_requires.splitlines():
            dep = re.split(r"\s+#", dep.strip(), maxsplit=1)[0].strip()
            if not dep or dep.startswith(("#", "file:", "attr:")):
                continue
            dependency = utils.requirement_dependency(dep)
            if dependency is None:
                pkg = re.split(r"[<>=~!]", dep)[0].strip()
                dependency = Dependency(name=pkg, version="") if pkg else None
            if dependency is not None and not dependency.name.startswith("-"):
                dependencies.add(dependency)

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        return utils.get_python_minor_versions(
            self.python_requires or "", self.spec.config
        )
