import re
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class Requirements_txt(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="requirements.txt", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        requirements_txt_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            return Path.exists(requirements_txt_path)
        except OSError:
            return False

    def _parse_requirements(
        req_file_path: Path, visited_files: set[Path], root: Path | None = None
    ) -> set[Dependency]:
        """
        Parses requirements recursively.
        """
        dependencies: set[Dependency] = set()
        req_file_path = Path(req_file_path).resolve()

        try:
            if not req_file_path.is_file() or req_file_path in visited_files:
                return dependencies
        except OSError:
            return dependencies

        visited_files.add(req_file_path)

        try:
            text = req_file_path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            return dependencies
        # a trailing backslash continues the requirement on the next line
        text = re.sub(r"\\\r?\n", " ", text)

        for line in text.splitlines():
            # an inline comment starts with whitespace and `#`
            line = re.split(r"\s+#", line.strip(), maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue

            # Nested requirement files: `-r f`, `-rf`, `--requirement[=| ]f`
            nested = re.match(r"^(?:-r|--requirement)(?:\s*=\s*|\s*)(\S+)$", line)
            if nested:
                target = (req_file_path.parent / nested.group(1)).resolve()
                # a nested file stays inside the project
                if root is None or _inside(target, root):
                    dependencies.update(
                        Requirements_txt._parse_requirements(
                            target, visited_files, root
                        )
                    )
                continue

            # Skip editable installs, constraint files and pip options
            if line.startswith("-"):
                continue

            # per-requirement options (`--hash=sha256:...`, `--config-settings`)
            line = re.split(r"\s+--?[A-Za-z]", line, maxsplit=1)[0].strip()
            if not line:
                continue

            # A bare VCS or URL spec has no name, so it is not a PEP 508
            # requirement; the installer takes it as written
            if re.match(r"^(?:(?:git|hg|svn|bzr)\+|https?://|file:)", line):
                dependencies.add(Dependency(name=line, version=""))
                continue

            dependency = utils.requirement_dependency(line)
            if dependency is None:
                # else the name before the first specifier
                name = re.split(r"[<>=~!]", line)[0].strip()
                if name and not name.startswith("-"):
                    dependency = Dependency(name=name, version="")
            if dependency is not None and not dependency.name.startswith("-"):
                dependencies.add(dependency)

        return dependencies

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        req_file_path: Path = self.spec.config.cwd / Path(self.spec.name)
        visited_files: set[Path] = set()
        dependencies: set[Dependency] = Requirements_txt._parse_requirements(
            req_file_path, visited_files, self.spec.config.cwd.resolve()
        )

        return dependencies

    @override
    def get_supported_language_versions(self) -> set[str] | AccraError:
        # NOTE requirements.txt does not specify version restrictions
        return utils.get_python_minor_versions(config=self.spec.config)
