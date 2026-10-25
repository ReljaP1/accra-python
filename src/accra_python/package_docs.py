"""The project's package as PyPI publishes it: for an entry point with no
usable `--help` output, the summary, the usage part of the long description
and the project links.

The package name is `pyproject.toml`'s (`[project]`, else `[tool.poetry]`),
else `setup.py`'s `name=`, else the name the caller gives. Any failure gives
None.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from accra_language import PackageDocs

from . import utils

REGISTRY = "PyPI"
DEFAULT_TIMEOUT = 8
MAX_DESCRIPTION_BYTES = 3000
PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"


def package_name(project: Path, fallback: str = "") -> str | None:
    """The name the project's manifests give its package, else `fallback`."""
    return (
        _read_pyproject_name(project / "pyproject.toml")
        or _read_setup_py_name(project / "setup.py")
        or (fallback.strip() or None)
    )


def package_documentation(
    project: Path, fallback: str = "", timeout: float | None = None
) -> PackageDocs | None:
    name = package_name(project, fallback)
    if not name:
        return None
    url = PYPI_JSON_URL.format(name=urllib.parse.quote(name, safe=""))
    seconds = timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ):
        # no entry (404), an HTTP error, no network, a timeout or not JSON
        return None

    info = (payload.get("info") or {}) if isinstance(payload, dict) else {}
    summary = (info.get("summary") or "").strip()
    description = (info.get("description") or "").strip()
    project_urls = {
        str(k): str(v) for k, v in (info.get("project_urls") or {}).items() if k and v
    }
    excerpt = _usage_excerpt(description)[:MAX_DESCRIPTION_BYTES]
    if not summary and not excerpt and not project_urls:
        return None
    return PackageDocs(
        registry=REGISTRY,
        package_name=name,
        source_url=url,
        summary=summary,
        description_excerpt=excerpt,
        project_urls=project_urls,
    )


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _read_pyproject_name(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        return None
    for table in (r"\[project\]", r"\[tool\.poetry\]"):
        block = re.search(
            rf"^\s*{table}\s*\n(.*?)(?=^\s*\[|\Z)", text, re.DOTALL | re.MULTILINE
        )
        if block:
            name = re.search(r'^\s*name\s*=\s*"([^"]+)"', block.group(1), re.MULTILINE)
            if name:
                return name.group(1).strip() or None
    return None


def _read_setup_py_name(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        return None
    return utils.setup_call_name(text)


_USAGE_HEADING = re.compile(
    r"(?:^|\n)(#+\s*(?:Usage|Quick ?start|Getting ?started|Command ?line)"
    r"|(?:Usage|Quick ?start|Getting ?started|Command ?line)\s*\n[-=]{3,})",
    re.IGNORECASE,
)


def _usage_excerpt(description: str) -> str:
    """The long description from its usage heading on, else its first 40
    non-empty lines."""
    if not description:
        return ""
    match = _USAGE_HEADING.search(description)
    if match:
        start = match.start()
        return description[start : start + MAX_DESCRIPTION_BYTES].strip()
    lines = [line for line in description.splitlines() if line.strip()]
    return "\n".join(lines[:40]).strip()
