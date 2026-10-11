import os
import re
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils


class Requirements_txt(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="requirements.txt", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        requirements_txt_path: Path = self.spec.config.cwd / Path(self.spec.name)
        return Path.exists(requirements_txt_path)

    def _parse_requirements(
        req_file_path: Path, visited_files: set[Path]
    ) -> set[Dependency]:
        """
        Parses requirements recursively.
        """
        dependencies: set[Dependency] = set()

        if not Path.exists(req_file_path) or req_file_path in visited_files:
            return

        visited_files.add(req_file_path)

        with open(req_file_path, "r", encoding="utf-8", errors="ignore") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Nested requirement files
                if line.startswith(("-r ", "--requirement ")):
                    nested_file = line.split(maxsplit=1)[-1].strip()
                    nested_path = os.path.join(
                        os.path.dirname(req_file_path), nested_file
                    )

                    dependencies.update(
                        Requirements_txt._parse_requirements(nested_path, visited_files)
                    )
                    continue

                # Skip editable installs and pip options
                if line.startswith(
                    (
                        "-e",
                        "--editable",
                        "--index-url",
                        "--find-links",
                        "--extra-index-url",
                        "--",
                    )
                ):
                    continue

                # TODO take into account version specifiers
                base_pkg = re.split(r"[<>=~!]", line)[0].strip()
                if base_pkg:
                    dependencies.add(Dependency(name=base_pkg, version=""))

        return dependencies

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        req_file_path: Path = self.spec.config.cwd / Path(self.spec.name)
        visited_files: set[Path] = set()
        dependencies: set[Dependency] = Requirements_txt._parse_requirements(
            req_file_path, visited_files
        )

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        # NOTE requirements.txt does not specify version restrictions
        return utils.get_python_minor_versions()
