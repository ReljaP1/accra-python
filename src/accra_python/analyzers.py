"""Profilers as accra-language analyzers: memray (memory) and scalene
(CPU/GPU load)."""

import json
import os
import platform
import re
import tempfile
import time
from pathlib import Path
from typing import override

from accra_language import (
    AccraAnalysisError,
    AccraInstallError,
    AccraNoSamplesError,
    Analyzer,
    AnalyzerSpec,
    Config,
    Dependency,
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


def _install(
    env_manager: EnvironmentManager, *packages: str
) -> AccraInstallError | None:
    """Installs `packages` in order; the first failure's error, else None."""
    for package in packages:
        result = env_manager._install_dependency(Dependency(name=package, version=""))
        if result.errors:
            error = result.errors[0]
            return AccraInstallError(message=error.message)
    return None


def _report_path(entry_point, output_dir: Path | None, suffix: str) -> Path:
    """`<output_dir>/<the entry point's file name><suffix>`; a new temporary
    directory without `output_dir`."""
    directory = Path(output_dir) if output_dir else Path(tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(entry_point.path).name if entry_point.path else "profile"
    return directory / f"{stem}{suffix}"


class _ProfilerAnalyzer(Analyzer):
    def _run(
        self,
        command,
        entry_point,
        env_manager: EnvironmentManager,
        env=None,
        timeout=None,
    ):
        config: Config = self.spec.config
        if env is None:
            base_env = dict(config.env) if config.env is not None else dict(os.environ)
            env = env_manager.run_environment(base_env)
        return utils.run(
            command,
            config,
            timeout if timeout is not None else utils.call_timeout(env_manager),
            cwd=entry_point.cwd or config.cwd,
            env=dict(env),
        )

    def _python(self, env_manager: EnvironmentManager) -> str:
        return str(env_manager.environment_path() / "bin" / "python")


class Memray(_ProfilerAnalyzer):
    """Peak and total allocated memory of one run, from `memray stats`."""

    def __init__(self, config: Config):
        super().__init__(
            AnalyzerSpec(name="memray", config=config, metrics={Metrics.MEMORY})
        )

    @override
    def install(self, env_manager: EnvironmentManager) -> AccraInstallError | None:
        # memray's report templates need jinja2 and markupsafe, installed first
        return _install(env_manager, "jinja2", "markupsafe", "memray")

    @override
    def analyze(
        self, entry_point, args, env_manager, *, env=None, timeout=None, output_dir=None
    ):
        python = self._python(env_manager)
        output = _report_path(entry_point, output_dir, ".bin")
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
            entry_point,
            env_manager,
            env,
            timeout,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if ran.returncode != 0:
            return utils.failure_error(
                AccraAnalysisError, f"memray run of {entry_point.name} failed", ran
            )

        stats = self._run(
            [python, "-m", "memray", "stats", str(output)],
            entry_point,
            env_manager,
            env,
            timeout,
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

    @override
    def install(self, env_manager: EnvironmentManager) -> AccraInstallError | None:
        if platform.system() not in {"Linux", "Darwin"}:
            return AccraInstallError(message="scalene runs on Linux and macOS only")
        return _install(env_manager, "scalene", "jinja2", "markupsafe")

    @override
    def analyze(
        self, entry_point, args, env_manager, *, env=None, timeout=None, output_dir=None
    ):
        output = _report_path(entry_point, output_dir, ".scalene.json")
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
            entry_point,
            env_manager,
            env,
            timeout,
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
