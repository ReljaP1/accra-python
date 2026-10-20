"""memray and scalene as accra-language analyzers, on a real run."""

import subprocess
from pathlib import Path

import accra_language
import pytest
from accra_language import Config

from accra_python import Python3

WORKLOAD = """
import sys, time

blocks = [bytearray(1024 * 1024) for _ in range(int(sys.argv[1]))]
end = time.time() + 3
total = 0
while time.time() < end:
    total += sum(range(10_000))
print(len(blocks), total > 0)
"""


def _python3_with_venv(project: Path) -> Python3:
    subprocess.run(["uv", "venv", "--seed", "-q", str(project / ".venv")], check=True)
    return Python3(Config(cwd=project))


@pytest.mark.integration
def test_memray_and_scalene_measure_a_run(tmp_path):
    (tmp_path / "work.py").write_text(WORKLOAD)
    python3 = _python3_with_venv(tmp_path)
    python3.detect()
    [entry] = [e for e in python3.entry_points() if e.name == "work.py"] or [None]
    if entry is None:  # no __main__ guard: build the entry point by hand
        entry = accra_language.EntryPoint(
            name="work.py",
            kind="script",
            command=(str(tmp_path / ".venv/bin/python"), str(tmp_path / "work.py")),
        )

    results = python3.analyze(entry, ["64"])

    assert list(results) == ["memray", "scalene"]
    [memory] = results["memray"]
    assert isinstance(memory, accra_language.MemoryProfile), results["memray"]
    assert memory.peak_memory_mb >= 60
    assert memory.total_memory_mb >= memory.peak_memory_mb

    load = results["scalene"]
    assert not isinstance(load, accra_language.AccraError), load.message
    [load] = load
    assert load.cpu_percent > 0
    assert load.duration_ms >= 3000


@pytest.mark.integration
def test_a_failing_run_is_an_error(tmp_path):
    (tmp_path / "boom.py").write_text("raise SystemExit(3)\n")
    python3 = _python3_with_venv(tmp_path)
    python3.detect()
    entry = accra_language.EntryPoint(
        name="boom.py",
        kind="script",
        command=(str(tmp_path / ".venv/bin/python"), str(tmp_path / "boom.py")),
    )

    results = python3.analyze(entry, [], metrics={accra_language.Metrics.MEMORY})

    assert list(results) == ["memray"]
    assert isinstance(results["memray"], accra_language.AccraError)
    assert "memray run of boom.py failed" in results["memray"].message


@pytest.mark.integration
def test_run_options_and_reports(tmp_path):
    (tmp_path / "work.py").write_text(WORKLOAD)
    python3 = _python3_with_venv(tmp_path)
    python3.detect()
    entry = python3.entry_point_for(tmp_path / "work.py")
    assert entry.kind == "script" and entry.command[-1] == str(tmp_path / "work.py")
    reports = tmp_path / "reports"
    env = python3.env_manager.run_environment(dict(__import__("os").environ))
    env["WORK_MARKER"] = "1"

    results = python3.analyze(
        entry,
        ["8"],
        {accra_language.Metrics.MEMORY},
        env=env,
        timeout=120,
        output_dir=reports,
    )

    [memory] = results["memray"]
    assert memory.report == str(reports / "work.py.bin")
    assert Path(memory.report).is_file()
    assert memory.exit_code is None


@pytest.mark.integration
def test_scalene_measures_a_failing_run_and_reports_no_samples(tmp_path):
    (tmp_path / "slow_fail.py").write_text(
        "import time\nend = time.time() + 3\nwhile time.time() < end:\n"
        "    sum(range(10_000))\nraise SystemExit(4)\n"
    )
    (tmp_path / "quick.py").write_text("print(1)\n")
    python3 = _python3_with_venv(tmp_path)
    python3.detect()
    load = accra_language.Metrics.LOAD

    failing = python3.analyze(
        python3.entry_point_for(tmp_path / "slow_fail.py"), [], {load}
    )
    [profile] = failing["scalene"]
    assert profile.exit_code == 4 and profile.cpu_percent > 0
    assert profile.report.endswith("slow_fail.py.scalene.json")

    quick = python3.analyze(python3.entry_point_for(tmp_path / "quick.py"), [], {load})
    assert isinstance(quick["scalene"], accra_language.AccraNoSamplesError)


def test_entry_point_for_runs_package_files_as_modules(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "run.py").write_text("print(1)\n")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "script.py").write_text("print(1)\n")
    python3 = Python3(Config(cwd=tmp_path))
    python3.detect()
    python = str(tmp_path / ".venv" / "bin" / "python")

    module = python3.entry_point_for(tmp_path / "pkg" / "run.py")
    assert (module.kind, module.command) == ("module", (python, "-m", "pkg.run"))
    script = python3.entry_point_for(Path("tools/script.py"))
    assert (script.kind, script.command) == (
        "script",
        (python, str(tmp_path / "tools" / "script.py")),
    )
