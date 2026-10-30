"""memray and scalene as accra-language analyzers, on a real run."""

import subprocess
from pathlib import Path

import accra_language
import pytest
from accra_language import AnalysisRun, Config

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

    results = python3.analyze_project(
        {accra_language.Metrics.MEMORY, accra_language.Metrics.LOAD},
        [AnalysisRun(entry_point=entry, args=("64",))],
    )

    assert list(results) == ["memray", "scalene"]
    [memory] = results["memray"][entry]
    assert isinstance(memory, accra_language.MemoryProfile), results["memray"]
    assert memory.peak_memory_mb >= 60
    assert memory.total_memory_mb >= memory.peak_memory_mb

    load = results["scalene"][entry]
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

    results = python3.analyze_project(
        {accra_language.Metrics.MEMORY}, [AnalysisRun(entry_point=entry)]
    )

    assert list(results) == ["memray"]
    assert isinstance(results["memray"][entry], accra_language.AccraError)
    assert "memray run of boom.py failed" in results["memray"][entry].message


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

    results = python3.analyze_project(
        {accra_language.Metrics.MEMORY},
        [
            AnalysisRun(
                entry_point=entry,
                args=("8",),
                env=env,
                timeout=120,
                output_dir=reports,
            )
        ],
    )

    [memory] = results["memray"][entry]
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

    slow = python3.entry_point_for(tmp_path / "slow_fail.py")
    failing = python3.analyze_project({load}, [AnalysisRun(entry_point=slow)])
    [profile] = failing["scalene"][slow]
    assert profile.exit_code == 4 and profile.cpu_percent > 0
    assert profile.report.endswith("slow_fail.py.scalene.json")

    entry = python3.entry_point_for(tmp_path / "quick.py")
    quick = python3.analyze_project({load}, [AnalysisRun(entry_point=entry)])
    assert isinstance(quick["scalene"][entry], accra_language.AccraNoSamplesError)


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


@pytest.mark.integration
def test_profilers_stay_out_of_the_project_environment(tmp_path):
    # the project pins its own jinja2 and has a sitecustomize of its own; the
    # run must see both untouched, and the package list must not change
    (tmp_path / "check.py").write_text(
        "import importlib.metadata as m, sys\n"
        "import jinja2\n"
        "open(sys.argv[1], 'w').write(\n"
        "    m.version('jinja2') + ' ' + jinja2.__file__ + ' '\n"
        "    + str(getattr(sys, 'accra_project_sitecustomize', False)))\n"
    )
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\nsys.accra_project_sitecustomize = True\n"
    )
    python3 = _python3_with_venv(tmp_path)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--python",
            str(tmp_path / ".venv/bin/python"),
            "jinja2==3.1.2",
            "markupsafe==2.1.3",
        ],
        check=True,
    )
    python3.detect()
    before = python3.env_manager.installed_dependencies()
    entry = python3.entry_point_for(tmp_path / "check.py")
    out = tmp_path / "seen.txt"
    run = AnalysisRun(
        entry_point=entry,
        args=(str(out),),
        env=python3.env_manager.run_environment(dict(__import__("os").environ)),
    )

    results = python3.analyze_project(
        {accra_language.Metrics.MEMORY, accra_language.Metrics.LOAD}, [run]
    )

    assert isinstance(results["memray"][entry], set), results["memray"][entry]
    version, location, sitecustomize = out.read_text().split()
    assert version == "3.1.2"
    assert location.startswith(str(tmp_path / ".venv"))
    assert sitecustomize == "True"
    assert python3.env_manager.installed_dependencies() == before
    assert not {"memray", "scalene"} & {
        d.name.lower() for d in python3.env_manager.installed_dependencies()
    }


def test_a_profiler_that_cannot_be_installed_fails_each_run(tmp_path, monkeypatch):
    from accra_python import analyzers

    monkeypatch.setattr(
        analyzers,
        "_install_tools",
        lambda name, packages, env_manager: accra_language.AccraInstallError(
            message="no network"
        ),
    )
    python3 = Python3(Config(cwd=tmp_path))
    python3.detect()
    first = accra_language.EntryPoint(name="a.py", kind="script", command=("python",))
    second = accra_language.EntryPoint(name="b.py", kind="script", command=("python",))
    runs = [AnalysisRun(entry_point=first), AnalysisRun(entry_point=second)]

    results = python3.analyze_project({accra_language.Metrics.MEMORY}, runs)

    assert set(results["memray"]) == {first, second}
    for error in results["memray"].values():
        assert isinstance(error, accra_language.AccraAnalysisError)
        assert error.message == "no network"
    # a measure of runs gives nothing when there are none
    assert python3.analyze_project({accra_language.Metrics.MEMORY}) == {"memray": {}}


def test_all_analyzers_are_registered_on_the_language(tmp_path):
    python3 = Python3(Config(cwd=tmp_path))

    names = sorted(a.spec.name for a in python3.spec.analyzers)

    assert names == ["memray", "python-network", "python-parallelism", "scalene"]
