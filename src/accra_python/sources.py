"""The Python files of a project: every non-empty `.py` file outside hidden,
environment, build and cache directories, with its imports as `ast` reads them.
A file that does not parse as Python 3 is not this plugin's (Python 2 code is
the python2 plugin's). Shared by the project analyzers and `entry_points()`.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import threading
from collections import OrderedDict
from fnmatch import fnmatch
from pathlib import Path

from . import utils

logger = logging.getLogger(__name__)

# Directories the selection never walks into.
_SKIP_DIR_PATTERNS = (
    ".*",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "*.egg-info",
    ".eggs",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    ".env",
    "env",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pypackages__",
)


def _skip_dir(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch(lower, pattern) for pattern in _SKIP_DIR_PATTERNS)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except OSError as exc:
            logger.warning("cannot read %s: %s", path, exc)
            return None
    except OSError as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return None


def _ast_imports(content: str) -> set[str] | None:
    """The dotted names `content` imports: each module, and each name imported
    from one (`pkg.mod`, `pkg.mod.name`); relative ones keep their dots. None
    when `ast` cannot parse it."""
    imports: set[str] = set()
    tree = utils.safe_parse(content)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0:
                module = "." * node.level + module
            imports.add(module)
            for alias in node.names:
                imports.add(f"{module}.{alias.name}" if module else alias.name)
    return imports


# Source files larger than this are not read for imports or entry points: a
# generated data module (a table of numbers, a dumped model) is not code the
# analyses need, and parsing it costs time and memory. No hand-written source
# file comes near it.
MAX_SOURCE_BYTES = 2 * 1024 * 1024

# Parsed sources per project root, kept so the consumers of one build (entry
# points, network, parallelism) share one read and parse. Each entry carries a
# fingerprint of the tree -- the sorted (path, mtime_ns, size) of its selected
# `.py` files -- and is used only while the tree still has it, so a changed
# tree (an edited, added or removed file) is re-read, and two projects in one
# process never see each other's files (the key is the resolved root).
# Bounded, least recently used first out.
_CACHE_LIMIT = 8
_cache: OrderedDict[str, tuple[str, list[tuple[Path, str, set[str]]]]] = OrderedDict()
_cache_lock = threading.Lock()


def _selected_files(project: Path):
    """(path, stat) of every `.py` file the selection rule covers, in walk
    order."""
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for filename in filenames:
            if filename.startswith(".") or not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            try:
                yield path, path.stat()
            except OSError as exc:
                logger.warning("cannot read %s: %s", path, exc)


def _fingerprint(files: list[tuple[Path, os.stat_result]]) -> str:
    digest = hashlib.sha1()
    for path, st in sorted(files, key=lambda item: str(item[0])):
        digest.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return digest.hexdigest()


def clear_cache() -> None:
    """Forget every cached read (tests; a caller that wants a re-read)."""
    with _cache_lock:
        _cache.clear()


def python_sources(project: Path) -> list[tuple[Path, str, set[str]]]:
    """(path, content, imports) of every selected Python 3 file, in walk
    order; a file `ast` cannot parse, an empty one and one
    over `MAX_SOURCE_BYTES` are left out. The read is shared by calls on the
    same, unchanged tree (see `_cache`); the walk and a `stat` per file are
    all a repeated call pays."""
    project = Path(project)
    try:
        key = str(project.resolve())
    except OSError:
        key = str(project)
    files = list(_selected_files(project))
    fingerprint = _fingerprint(files)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == fingerprint:
            _cache.move_to_end(key)
            return list(cached[1])

    sources = []
    for path, st in files:
        if st.st_size > MAX_SOURCE_BYTES:
            logger.debug(
                "skipping %s: %d bytes is over the %d byte source cap",
                path,
                st.st_size,
                MAX_SOURCE_BYTES,
            )
            continue
        content = _read(path)
        if content is None or not content.strip():
            continue
        imports = _ast_imports(content)
        if imports is None:
            continue
        sources.append((path, content, imports))
    with _cache_lock:
        _cache[key] = (fingerprint, sources)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return list(sources)


# The main-file rule: a file that takes input from a main guard, argparse, sys.argv or input(), that is not a
# packaging or test helper by name and does not live under a tests or docs
# directory.
_MAIN_GUARD = re.compile(
    r'^\s*if\s+__name__\s*==\s*["\']__main__["\']\s*:', re.MULTILINE
)
_NOT_ENTRY_NAMES = frozenset(
    {"__init__.py", "setup.py", "conftest.py", "distribute_setup.py", "ez_setup.py"}
)
_NOT_ENTRY_DIRS = ("/tests/", "/test/", "/testing/", "/docs/", "/doc/", "/doc_legacy/")


def takes_input(content: str, imports: set[str]) -> bool:
    """Whether the file reads input as a program does."""
    return bool(
        _MAIN_GUARD.search(content)
        or "argparse" in imports
        or ("sys" in imports and "sys.argv" in content)
        or "input(" in content
    )


def is_main_file(path: Path, content: str, imports: set[str]) -> bool:
    if not takes_input(content, imports):
        return False
    if path.name.lower() in _NOT_ENTRY_NAMES:
        return False
    normalized = str(path).replace("\\", "/").lower()
    return not any(d in normalized for d in _NOT_ENTRY_DIRS)
