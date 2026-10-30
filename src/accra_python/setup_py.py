import ast
import re
from pathlib import Path
from typing import override

from accra_language import AccraError, Config, Dependency, Manifest, ManifestSpec

from . import utils

_MAX_NAME_DEPTH = 10


def install_requires(source: str) -> list[str] | None:
    """The `install_requires` of `source`'s `setup(...)` call, read without
    running it: string literals, lists and tuples of them, module-level names
    bound to those, and `+` of them. None when the source does not parse or
    the value cannot be read this way (e.g. read from a file)."""
    tree = utils.safe_parse(source)
    if tree is None:
        return None

    bound: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                bound[target.id] = node.value

    def strings(expr: ast.expr, depth: int = 0) -> list[str] | None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return [expr.value]
        if isinstance(expr, (ast.List, ast.Tuple)):
            found: list[str] = []
            for element in expr.elts:
                value = strings(element, depth)
                if value is None:
                    return None
                found.extend(value)
            return found
        if isinstance(expr, ast.Name) and expr.id in bound and depth < _MAX_NAME_DEPTH:
            return strings(bound[expr.id], depth + 1)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left, right = strings(expr.left, depth), strings(expr.right, depth)
            return None if left is None or right is None else left + right
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg == "install_requires":
                found = strings(keyword.value)
                return [r for r in found if r.strip()] if found is not None else None
    return None


class Setup_py(Manifest):
    def __init__(self, config: Config):
        spec = ManifestSpec(name="setup.py", version="", config=config)
        super().__init__(spec)

    @override
    def detect(self) -> bool:
        setuppy_path: Path = self.spec.config.cwd / Path(self.spec.name)

        try:
            return Path.exists(setuppy_path)
        except OSError:
            return False

    @override
    def extract_dependencies(self) -> set[Dependency] | None:
        setuppy_path: Path = self.spec.config.cwd / Path(self.spec.name)
        try:
            source = setuppy_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        # the setup() call's install_requires, read with `ast`, versions kept
        # the line reader below when that resolves nothing
        requirements = install_requires(source)
        if requirements:
            dependencies: set[Dependency] = set()
            for requirement in requirements:
                dependency = utils.requirement_dependency(requirement)
                if dependency is None:
                    name = re.split(r"[<>=~!;\s\[]", requirement.strip())[0]
                    dependency = Dependency(name=name, version="") if name else None
                if dependency is not None:
                    dependencies.add(dependency)
            return dependencies
        return self._extract_dependencies_by_line(setuppy_path)

    def _extract_dependencies_by_line(
        self, setuppy_path: Path
    ) -> set[Dependency] | None:
        """A line reader: string literals on `install_requires = [`
        lines, versions dropped."""
        dependencies: set[Dependency] = set()

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

        except OSError:
            pass

        return utils.get_python_minor_versions(requires_python, self.spec.config)
