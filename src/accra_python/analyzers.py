"""Profilers as accra-language analyzers: memray (memory) and scalene
(CPU/GPU load).

A profiler is never installed into the project's environment: it goes, once per
Python version and platform, into a directory of its own outside the project,
and a run reaches it through a `sitecustomize` that appends that directory to
`sys.path` after the environment's site-packages, so the project's own packages
always win and its package list stays as it was."""

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from abc import abstractmethod
from pathlib import Path
from typing import override

from accra_language import (
    AccraAnalysisError,
    AccraError,
    AccraInstallError,
    AccraNoSamplesError,
    AnalysisResults,
    AnalysisRun,
    Analyzer,
    AnalyzerSpec,
    Config,
    EnvironmentManager,
    LoadProfile,
    MemoryProfile,
    Metrics,
)

from . import utils

_SIZE_RE = r"(\d+(?:\.\d+)?)\s*([KMGT]?B)"
_MB_PER_UNIT = {
    "B": 1 / (1024 * 1024),
    "KB": 1 / 1024,
    "MB": 1.0,
    "GB": 1024.0,
    "TB": 1024.0 * 1024.0,
}


def _megabytes(stats: str, label: str) -> float | None:
    """The size after `label` in `memray stats` output, in MB."""
    match = re.search(label + r":\s*" + _SIZE_RE, stats, re.IGNORECASE)
    if not match:
        return None
    return round(float(match.group(1)) * _MB_PER_UNIT[match.group(2).upper()], 4)


def _target(entry_point) -> list[str]:
    """The entry point's argv without its interpreter, for `python -m <tool>
    run`: `[script]`, `["-m", module]`, or a console script's path."""
    command = list(entry_point.command)
    if len(command) > 1 and Path(command[0]).name.startswith("python"):
        return command[1:]
    return command


# put on PYTHONPATH of a run: appends the tools directory (the environment
# variable below) to sys.path once site-packages is on it, then lets the
# project's own sitecustomize, if it has one, run as it would have
_LOADER = """\
import os
import sys

_tools = os.environ.get("ACCRA_PYTHON_TOOLS")
if _tools and _tools not in sys.path:
    sys.path.append(_tools)

_here = os.path.dirname(os.path.abspath(__file__))
_own = sys.modules.pop("sitecustomize", None)
_path = sys.path[:]
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
try:
    import sitecustomize  # noqa: F401
except ModuleNotFoundError as _exc:
    if _exc.name != "sitecustomize":
        raise
    if _own is not None:
        sys.modules["sitecustomize"] = _own
finally:
    sys.path[:] = _path
"""


def _cache_root() -> Path:
    """Where the profilers live: the user's cache directory, else a temporary
    one."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    for root in (Path(base), Path(tempfile.gettempdir())):
        try:
            directory = root / "accra-python" / "tools"
            directory.mkdir(parents=True, exist_ok=True)
            if os.access(directory, os.W_OK):
                return directory
        except OSError:
            continue
    return Path(tempfile.mkdtemp())


def _interpreter_key(python: str, config: Config, timeout) -> str | None:
    """`cp311-linux-x86_64`: what a compiled profiler is built for."""
    probed = utils.run(
        [
            python,
            "-c",
            (
                "import sys, sysconfig;"
                "print('cp%d%d-%s' % (*sys.version_info[:2], sysconfig.get_platform()))"
            ),
        ],
        config,
        timeout,
    )
    key = (probed.stdout or "").strip()
    return (
        re.sub(r"[^A-Za-z0-9_.-]", "_", key) or None if probed.returncode == 0 else None
    )


class _Tools:
    """A profiler's installation: its packages directory and the loader."""

    def __init__(self, packages: Path, loader: Path):
        self.packages = packages
        self.loader = loader

    def environment(self, env: dict[str, str]) -> dict[str, str]:
        """`env` with the loader first on PYTHONPATH and the packages named."""
        env = dict(env)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.loader), *([existing] if existing else [])]
        )
        env["ACCRA_PYTHON_TOOLS"] = str(self.packages)
        return env


def _install_tools(
    name: str, packages: tuple[str, ...], env_manager: EnvironmentManager
) -> _Tools | AccraInstallError:
    """The profiler `packages` for the environment's Python, installed once
    outside the project (`uv pip install --target`, whose resolution ignores
    what the project has installed)."""
    config: Config = env_manager.spec.config
    python = str(env_manager.environment_path() / "bin" / "python")
    timeout = utils.call_timeout(env_manager)
    try:
        key = _interpreter_key(python, config, timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return AccraInstallError(
            message=f"could not run the environment's Python: {exc}"
        )
    if key is None:
        return AccraInstallError(message="could not read the environment's Python")
    root = _cache_root() / key / name
    packages_dir = root / "packages"
    loader_dir = root / "loader"
    done = root / ".installed"
    wanted = " ".join(packages)
    if done.is_file() and done.read_text(encoding="utf-8") == wanted:
        return _Tools(packages_dir, loader_dir)

    # built beside the final place and moved into it, so that concurrent runs
    # never see a half-installed directory
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=root.parent))
    try:
        installed = utils.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                python,
                "--target",
                str(staging / "packages"),
                *packages,
            ],
            config,
            timeout,
            extra_env=utils.uv_timeout_env(config),
        )
        if installed.returncode != 0:
            return utils.failure_error(
                AccraInstallError, f"could not install {name}", installed
            )
        (staging / "loader").mkdir()
        (staging / "loader" / "sitecustomize.py").write_text(_LOADER, encoding="utf-8")
        (staging / ".installed").write_text(wanted, encoding="utf-8")
        # another run may have installed it meanwhile: keep that one
        if not (done.is_file() and done.read_text(encoding="utf-8") == wanted):
            shutil.rmtree(root, ignore_errors=True)
            try:
                staging.rename(root)
            except OSError:
                return AccraInstallError(message=f"could not install {name}")
    except (subprocess.SubprocessError, OSError) as exc:
        return AccraInstallError(message=f"could not install {name}: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _Tools(packages_dir, loader_dir)


def _report_path(entry_point, output_dir: Path | None, suffix: str) -> Path:
    """`<output_dir>/<the entry point's file name><suffix>`; a new temporary
    directory without `output_dir`."""
    directory = Path(output_dir) if output_dir else Path(tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(entry_point.path).name if entry_point.path else "profile"
    return directory / f"{stem}{suffix}"


class _ProfilerAnalyzer(Analyzer):
    """Runs a profiler over each run it is given, from its own installation."""

    _PACKAGES: tuple[str, ...] = ()

    def __init__(self, spec: AnalyzerSpec):
        super().__init__(spec)
        # the install error of an interpreter key that failed, not retried
        self._failed: dict[str, AccraInstallError] = {}

    def _unsupported(self) -> AccraInstallError | None:
        return None

    def _tools(self, env_manager: EnvironmentManager) -> _Tools | AccraInstallError:
        unsupported = self._unsupported()
        if unsupported is not None:
            return unsupported
        python = str(env_manager.environment_path() / "bin" / "python")
        if python in self._failed:
            return self._failed[python]
        tools = _install_tools(self.spec.name, self._PACKAGES, env_manager)
        if isinstance(tools, AccraInstallError):
            self._failed[python] = tools
        return tools

    @override
    def analyze_project(
        self, env_manager: EnvironmentManager, runs=()
    ) -> AnalysisResults:
        if not runs:
            return {}
        tools = self._tools(env_manager)
        results: AnalysisResults = {}
        for run in runs:
            if isinstance(tools, AccraError):
                results[run.entry_point] = AccraAnalysisError(message=tools.message)
            else:
                results[run.entry_point] = self._measure(run, env_manager, tools)
        return results

    @abstractmethod
    def _measure(self, run: AnalysisRun, env_manager: EnvironmentManager, tools):
        """The profiles of one run, or its error."""
        ...

    def _run(self, command, run: AnalysisRun, env_manager, tools: _Tools):
        config: Config = self.spec.config
        env = run.env
        if env is None:
            base_env = dict(config.env) if config.env is not None else dict(os.environ)
            env = env_manager.run_environment(base_env)
        return utils.run(
            command,
            config,
            run.timeout if run.timeout is not None else utils.call_timeout(env_manager),
            cwd=run.entry_point.cwd or config.cwd,
            env=tools.environment(dict(env)),
        )

    def _python(self, env_manager: EnvironmentManager) -> str:
        return str(env_manager.environment_path() / "bin" / "python")


class Memray(_ProfilerAnalyzer):
    """Peak and total allocated memory of one run, from `memray stats`."""

    def __init__(self, config: Config):
        super().__init__(
            AnalyzerSpec(name="memray", config=config, metrics={Metrics.MEMORY})
        )

    # memray's report templates need jinja2 and markupsafe
    _PACKAGES = ("jinja2", "markupsafe", "memray")

    @override
    def _measure(self, run, env_manager, tools):
        entry_point, args = run.entry_point, run.args
        python = self._python(env_manager)
        output = _report_path(entry_point, run.output_dir, ".bin")
        output.unlink(missing_ok=True)
        started = time.monotonic()
        ran = self._run(
            [
                python,
                "-m",
                "memray",
                "run",
                "--output",
                str(output),
                *_target(entry_point),
                *args,
            ],
            run,
            env_manager,
            tools,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if ran.returncode != 0:
            return utils.failure_error(
                AccraAnalysisError, f"memray run of {entry_point.name} failed", ran
            )

        stats = self._run(
            [python, "-m", "memray", "stats", str(output)],
            run,
            env_manager,
            tools,
        )
        if stats.returncode != 0:
            return utils.failure_error(AccraAnalysisError, "memray stats failed", stats)

        peak = _megabytes(stats.stdout or "", "Peak memory usage")
        total = _megabytes(stats.stdout or "", "Total memory allocated")
        if peak is None or total is None:
            return AccraAnalysisError(message="unreadable memray stats output")
        return {
            MemoryProfile(
                duration_ms=duration_ms,
                peak_memory_mb=peak,
                total_memory_mb=total,
                report=str(output),
            )
        }


class Scalene(_ProfilerAnalyzer):
    """CPU (Python, native and system time) and peak GPU use of one run, from
    scalene's JSON report. Linux and macOS only."""

    def __init__(self, config: Config):
        super().__init__(
            AnalyzerSpec(name="scalene", config=config, metrics={Metrics.LOAD})
        )

    _PACKAGES = ("scalene", "jinja2", "markupsafe")

    @override
    def _unsupported(self):
        if platform.system() not in {"Linux", "Darwin"}:
            return AccraInstallError(message="scalene runs on Linux and macOS only")
        return None

    @override
    def _measure(self, run, env_manager, tools):
        entry_point, args = run.entry_point, run.args
        output = _report_path(entry_point, run.output_dir, ".scalene.json")
        output.unlink(missing_ok=True)
        started = time.monotonic()
        ran = self._run(
            [
                self._python(env_manager),
                "-m",
                "scalene",
                "run",
                "--outfile",
                str(output),
                *_target(entry_point),
                *args,
            ],
            run,
            env_manager,
            tools,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        # a failing script still leaves a report worth reading; without one
        # the run is the error
        if ran.returncode != 0 and not output.exists():
            return utils.failure_error(
                AccraAnalysisError, f"scalene run of {entry_point.name} failed", ran
            )
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return AccraAnalysisError(
                message=f"unreadable scalene report: {exc}",
                **utils.failure_fields(ran),
            )

        lines = [
            line
            for entry in (report.get("files") or {}).values()
            for line in entry.get("lines", [])
            if isinstance(line, dict)
        ]
        if not lines:
            # scalene writes an empty report when the run ended before it
            # could collect samples
            return AccraNoSamplesError(
                message="the run completed before scalene could collect samples",
                **utils.failure_fields(ran),
            )
        cpu = sum(
            float(line.get(key, 0.0) or 0.0)
            for line in lines
            for key in ("n_cpu_percent_python", "n_cpu_percent_c", "n_sys_percent")
        )
        gpu = max(float(line.get("n_gpu_percent", 0.0) or 0.0) for line in lines)
        return {
            LoadProfile(
                duration_ms=duration_ms,
                cpu_percent=round(cpu, 2),
                gpu_percent=round(gpu, 2),
                exit_code=ran.returncode or None,
                report=str(output),
            )
        }
