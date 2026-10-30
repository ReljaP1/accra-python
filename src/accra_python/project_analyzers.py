"""Static analyses of a Python project's sources: networking and parallelism.

`sources.python_sources` selects and reads the files.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import override

from accra_language import (
    AccraAnalysisError,
    AnalysisResults,
    Analyzer,
    AnalyzerSpec,
    Config,
    EnvironmentManager,
    FileModules,
    LibraryUsage,
    Metrics,
    NetworkProfile,
    ParallelismProfile,
    ParallelismUsage,
)

from .sources import python_sources


def _counts(found: dict[str, list[str]]) -> tuple[LibraryUsage, ...]:
    counts = Counter(name for names in found.values() for name in names)
    return tuple(
        LibraryUsage(name=name, count=count) for name, count in sorted(counts.items())
    )


_NETWORK_PACKAGES = frozenset(
    {"requests", "httpx", "urllib3", "socket", "paramiko", "asyncio", "ftplib"}
)
_WEB_FRAMEWORKS = ("flask", "django", "fastapi", "tornado", "pyramid", "bottle")
_NETWORK_CALLS = (
    r"requests\.(get|post|put|delete|patch|head)",
    r"httpx\.(get|post|put|delete|patch|head)",
    r"urllib\.request\.(urlopen|Request)",
    r"socket\.(connect|send|recv|bind|listen|accept)",
    r"ftplib\.(FTP|connect|login)",
)


class Network(Analyzer):
    """Whether the project needs the network: networking packages among its
    dependencies and imports, network calls in its code, web frameworks."""

    def __init__(self, config: Config):
        super().__init__(
            AnalyzerSpec(
                name="python-network", config=config, metrics={Metrics.NETWORK}
            )
        )

    @override
    def analyze_project(
        self, env_manager: EnvironmentManager, runs=()
    ) -> AnalysisResults:
        return {None: self._measure(env_manager)}

    def _measure(self, env_manager: EnvironmentManager):
        started = time.monotonic()
        project = Path(self.spec.config.cwd or ".")
        try:
            declared = {d.name for d in (env_manager._get_dependencies() or ())}
            sources = python_sources(project)
        except (OSError, ValueError) as exc:
            return AccraAnalysisError(message=f"could not read the project: {exc}")

        dependencies = sorted(declared & _NETWORK_PACKAGES)
        imports: dict[str, list[str]] = {}
        frameworks: dict[str, list[str]] = {}
        calls: dict[str, list[str]] = {}
        for path, content, file_imports in sources:
            found = sorted(_NETWORK_PACKAGES & file_imports)
            if found:
                imports[str(path)] = found
            web = [fw for fw in _WEB_FRAMEWORKS if fw in file_imports]
            if web:
                frameworks[str(path)] = web
            used = [
                pattern for pattern in _NETWORK_CALLS if re.search(pattern, content)
            ]
            if used:
                calls[str(path)] = used

        return {
            NetworkProfile(
                duration_ms=int((time.monotonic() - started) * 1000),
                requires_network=bool(dependencies or imports or calls or frameworks),
                dependencies=tuple(dependencies),
                imports=_counts(imports),
                api_calls=_counts(calls),
                web_frameworks=_counts(frameworks),
            )
        }


_MULTIPROCESSING = (
    "multiprocessing",
    "ProcessPoolExecutor",
    "concurrent.futures.process",
    "joblib",
    "dask.distributed",
    "pathos",
    "ray",
    "celery",
    "billiard",
    "mpire",
)
_THREADING = (
    "threading",
    "concurrent.futures.threadpool",
    "ThreadPoolExecutor",
    "gevent",
    "eventlet",
    "twisted.internet",
    "asyncio",
    "multiprocessing.dummy",
)


def _usage(found: dict[str, set[str]]) -> ParallelismUsage:
    return ParallelismUsage(
        used=bool(found),
        modules=tuple(sorted(set().union(*found.values()))) if found else (),
        files=tuple(
            FileModules(path=path, modules=tuple(sorted(modules)))
            for path, modules in found.items()
        ),
    )


class Parallelism(Analyzer):
    """Which files use threading and which multiprocessing libraries (an
    indicator appearing in an imported name, case-insensitively)."""

    def __init__(self, config: Config):
        super().__init__(
            AnalyzerSpec(
                name="python-parallelism", config=config, metrics={Metrics.PARALLELISM}
            )
        )

    @override
    def analyze_project(
        self, env_manager: EnvironmentManager, runs=()
    ) -> AnalysisResults:
        return {None: self._measure(env_manager)}

    def _measure(self, env_manager: EnvironmentManager):
        started = time.monotonic()
        project = Path(self.spec.config.cwd or ".")
        try:
            sources = python_sources(project)
        except (OSError, ValueError) as exc:
            return AccraAnalysisError(message=f"could not read the project: {exc}")

        threads: dict[str, set[str]] = {}
        processes: dict[str, set[str]] = {}
        for path, _content, file_imports in sources:
            for module in file_imports:
                if any(i.lower() in module for i in _MULTIPROCESSING):
                    processes.setdefault(str(path), set()).add(module)
                if any(i.lower() in module for i in _THREADING):
                    threads.setdefault(str(path), set()).add(module)

        return {
            ParallelismProfile(
                duration_ms=int((time.monotonic() - started) * 1000),
                multithreading=_usage(threads),
                multiprocessing=_usage(processes),
            )
        }
