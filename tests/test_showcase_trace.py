"""
Tests for accra_python/showcase_trace.py.

These run the shim as a real subprocess against throwaway fixture repos, because
the properties under test are runtime properties -- what a profile hook does and
does not observe -- and a mocked version would prove nothing.

Covered:
  - a repository function that runs IS recorded
  - a call inside a guard that returns early is NOT recorded
  - code launched via subprocess.run([sys.executable, ...]) is NOT recorded
    (structural subprocess exclusion)
  - a function called from a spawned thread IS recorded (threading.setprofile)
  - the target's exit code is preserved, so Tier 1 is unaffected by tracing
  - a crashing target still yields a trace file
  - load_trace degrades to [] on a missing or malformed file
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(_ROOT, "src", "accra_python", "showcase_trace.py")

_spec = importlib.util.spec_from_file_location("shim_under_test", SHIM)
shim = importlib.util.module_from_spec(_spec)
sys.modules["shim_under_test"] = shim
_spec.loader.exec_module(shim)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
        FAILURES.append(label)


def run_shim(repo_root: str, script: str, workdir: str):
    """Run the shim over `script`; return (exit_code, trace entries, payload)."""
    out = os.path.join(workdir, "trace.json")
    env = dict(os.environ)
    env["ACCRA_REPO_ROOT"] = repo_root
    env["ACCRA_TRACE_OUT"] = out
    proc = subprocess.run(
        [sys.executable, SHIM, script],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        cwd=workdir,
    )
    payload = {}
    if os.path.exists(out):
        with open(out) as handle:
            payload = json.load(handle)
    return proc, shim.load_trace(out), payload


def write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)
    return path


def make_repo(base: str) -> str:
    """A tiny library-shaped repo under `base/repo`."""
    repo = os.path.join(base, "repo")
    write(os.path.join(repo, "toolbox", "__init__.py"), "")
    write(
        os.path.join(repo, "toolbox", "math_ops.py"),
        "def add_numbers(a, b):\n"
        '    """Adds."""\n'
        "    return a + b\n"
        "\n"
        "def never_called(x):\n"
        "    return x\n",
    )
    write(
        os.path.join(repo, "driver.py"),
        "from toolbox.math_ops import add_numbers\n"
        "print('driver says', add_numbers(1, 1))\n",
    )
    return repo


def test_executed_call_is_recorded() -> None:
    print("\n[1] a repository function that runs is recorded")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(
            os.path.join(base, "show.py"),
            "import sys\n"
            f"sys.path.insert(0, {repo!r})\n"
            "from toolbox.math_ops import add_numbers\n"
            "print(add_numbers(2, 3))\n",
        )
        proc, entries, payload = run_shim(repo, script, base)
        names = {name for _, name in entries}
        check("exit code 0", proc.returncode == 0, detail=proc.stderr[-300:])
        check("add_numbers recorded", "add_numbers" in names, detail=str(entries))
        check("never_called NOT recorded", "never_called" not in names)
        check(
            "module path is repo-relative",
            any(m == "toolbox/math_ops.py" for m, _ in entries),
            detail=str(entries),
        )
        check("status completed", payload.get("status") == "completed")


def test_guarded_call_is_not_recorded() -> None:
    print("\n[2] THE over-counting fix: a guarded call that never runs is absent")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(
            os.path.join(base, "show.py"),
            "import sys, os\n"
            f"sys.path.insert(0, {repo!r})\n"
            "from toolbox.math_ops import add_numbers\n"
            "def run():\n"
            "    if not os.path.exists('definitely-not-here'):\n"
            "        print('[ERROR] missing input; exiting gracefully')\n"
            "        return\n"
            "    print(add_numbers(1, 2))\n"
            "run()\n",
        )
        proc, entries, _ = run_shim(repo, script, base)
        names = {name for _, name in entries}
        check("exit code 0 — the script 'succeeded'", proc.returncode == 0)
        check(
            "add_numbers NOT recorded despite being statically present",
            "add_numbers" not in names,
            detail=str(entries),
        )


def test_subprocess_launch_is_invisible() -> None:
    print("\n[3] structural subprocess exclusion — no pattern-matching required")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(
            os.path.join(base, "show.py"),
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, {os.path.join(repo, 'driver.py')!r}],\n"
            f"               cwd={repo!r}, capture_output=True)\n"
            "print('launched')\n",
        )
        proc, entries, _ = run_shim(repo, script, base)
        names = {name for _, name in entries}
        check("exit code 0", proc.returncode == 0, detail=proc.stderr[-300:])
        check(
            "add_numbers ran in the child but is NOT in our trace",
            "add_numbers" not in names,
            detail=str(entries),
        )


def test_thread_is_traced() -> None:
    print("\n[4] threading.setprofile — a call from a spawned thread is recorded")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(
            os.path.join(base, "show.py"),
            "import sys, threading\n"
            f"sys.path.insert(0, {repo!r})\n"
            "from toolbox.math_ops import add_numbers\n"
            "results = []\n"
            "t = threading.Thread(target=lambda: results.append(add_numbers(4, 5)))\n"
            "t.start(); t.join()\n"
            "print(results)\n",
        )
        proc, entries, _ = run_shim(repo, script, base)
        names = {name for _, name in entries}
        check("exit code 0", proc.returncode == 0, detail=proc.stderr[-300:])
        check(
            "add_numbers recorded from the thread",
            "add_numbers" in names,
            detail=str(entries),
        )


def test_exit_code_preserved() -> None:
    print("\n[5] the target's exit code survives tracing (Tier 1 unaffected)")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(os.path.join(base, "show.py"), "import sys\nsys.exit(3)\n")
        proc, _, payload = run_shim(repo, script, base)
        check(
            "nonzero exit propagated", proc.returncode == 3, detail=str(proc.returncode)
        )
        check("status records system_exit", payload.get("status") == "system_exit")

        ok = write(os.path.join(base, "ok.py"), "print('fine')\n")
        proc2, _, _ = run_shim(repo, ok, base)
        check("zero exit propagated", proc2.returncode == 0)


def test_crash_still_writes_trace() -> None:
    print("\n[6] a crashing target still yields a trace")
    with tempfile.TemporaryDirectory() as base:
        repo = make_repo(base)
        script = write(
            os.path.join(base, "show.py"),
            "import sys\n"
            f"sys.path.insert(0, {repo!r})\n"
            "from toolbox.math_ops import add_numbers\n"
            "print(add_numbers(1, 1))\n"
            "raise RuntimeError('boom')\n",
        )
        proc, entries, payload = run_shim(repo, script, base)
        names = {name for _, name in entries}
        check("nonzero exit", proc.returncode == 1)
        check("status records exception", payload.get("status") == "exception")
        check(
            "the call made before the crash is still recorded", "add_numbers" in names
        )


def test_load_trace_degrades() -> None:
    print("\n[7] load_trace degrades to [] rather than raising")
    check("missing file -> []", shim.load_trace("/nonexistent/trace.json") == [])
    with tempfile.TemporaryDirectory() as base:
        bad = write(os.path.join(base, "bad.json"), "{not json")
        check("malformed file -> []", shim.load_trace(bad) == [])
        empty = write(os.path.join(base, "empty.json"), "{}")
        check("no entries key -> []", shim.load_trace(empty) == [])


def test_missing_env_is_shim_error() -> None:
    print("\n[8] a shim misconfiguration is distinguishable from a script failure")
    with tempfile.TemporaryDirectory() as base:
        script = write(os.path.join(base, "show.py"), "print('hi')\n")
        proc = subprocess.run(
            [sys.executable, SHIM, script],
            check=False,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in ("ACCRA_REPO_ROOT", "ACCRA_TRACE_OUT")
            },
            capture_output=True,
            text=True,
            cwd=base,
        )
        check(
            "exits 97, not 0 or 1",
            proc.returncode == shim.SHIM_INTERNAL_ERROR,
            detail=str(proc.returncode),
        )


def main() -> int:
    print("=== showcase_trace tests ===")
    test_executed_call_is_recorded()
    test_guarded_call_is_not_recorded()
    test_subprocess_launch_is_invisible()
    test_thread_is_traced()
    test_exit_code_preserved()
    test_crash_still_writes_trace()
    test_load_trace_degrades()
    test_missing_env_is_shim_error()

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} check(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


def test_showcase_trace():
    assert main() == 0, FAILURES


if __name__ == "__main__":
    sys.exit(main())
