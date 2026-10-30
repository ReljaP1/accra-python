#!/usr/bin/env python
"""
Dynamic confirmation shim for usage scripts.

Runs a generated showcase script under `sys.setprofile` and records which
repository functions actually executed, then writes them as JSON.

Two consequences:
  * code launched with `subprocess.run([sys.executable, ...])` runs in another
    interpreter and never enters this trace, so it cannot count as a
    demonstration;
  * dead code and untaken branches simply do not appear.

Constraints this file obeys deliberately:
  * standard library only -- it executes inside the *analyzed project's* venv,
    where `accra_code` is not installed and adding a dependency could conflict
    with the project's own pins;
  * no f-strings and no variable annotations -- it must import on every
    Python 3 version the plugin builds environments for (3.5 through 3.14);
  * `sys.setprofile` (call/return only) rather than `sys.settrace` (per line),
    which is far cheaper on call-heavy code;
  * `threading.setprofile` as well, because the profile hook is per-thread.

Usage (the caller sets both environment variables):

    ACCRA_REPO_ROOT=/abs/path/to/repo \
    ACCRA_TRACE_OUT=/abs/path/trace.json \
    python showcase_trace.py /abs/path/to/accra_generated_test.py [args...]

Exit code is the target script's own, so the caller's Tier-1 judgement is
unaffected by running under the shim. If the shim itself cannot start, it
returns 97 so a shim failure is never mistaken for a script failure.
"""

# It runs inside the analysed project's interpreter (Python 3.5 and later), so
# no f-strings, and catching everything is how it never changes the outcome.
# ruff: noqa: BLE001, SIM115, UP031

import json
import os
import runpy
import sys
import threading

SHIM_INTERNAL_ERROR = 97


def _normalize(path):
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _relative(path, root):
    """Repo-relative POSIX path, or the absolute path when outside the root."""
    try:
        rel = os.path.relpath(path, root)
    except Exception:
        return path
    if rel.startswith(os.pardir):
        return path
    return rel.replace(os.sep, "/")


def _make_hook(repo_root, seen):
    def hook(frame, event, arg):
        if event != "call":
            return
        code = frame.f_code
        filename = code.co_filename
        # Cheap prefix test first; realpath on every call would dominate cost.
        if filename.startswith(repo_root):
            seen.add((filename, code.co_name))
        return

    return hook


def _write(out_path, repo_root, seen, status, detail):
    entries = []
    for filename, funcname in sorted(seen):
        entries.append(
            {
                "module": _relative(_normalize(filename), repo_root),
                "function": funcname,
            }
        )
    payload = {
        "schema": "accra.showcase.trace/1",
        "status": status,
        "detail": detail,
        "repo_root": repo_root,
        "entries": entries,
    }
    try:
        handle = open(out_path, "w")
        try:
            json.dump(payload, handle, indent=2, sort_keys=True)
        finally:
            handle.close()
    except Exception:
        # Never let trace-writing failure change the script's outcome.
        sys.stderr.write("[accra-trace] could not write %s\n" % out_path)


def main(argv):
    repo_root = os.environ.get("ACCRA_REPO_ROOT") or ""
    out_path = os.environ.get("ACCRA_TRACE_OUT") or ""

    if not repo_root or not out_path or len(argv) < 2:
        sys.stderr.write(
            "[accra-trace] need ACCRA_REPO_ROOT, ACCRA_TRACE_OUT and a target script\n"
        )
        return SHIM_INTERNAL_ERROR

    repo_root = _normalize(repo_root)
    target = argv[1]
    seen = set()
    hook = _make_hook(repo_root, seen)

    # The target must see itself as __main__ with its own argv.
    sys.argv = [target] + list(argv[2:])

    status = "completed"
    detail = ""
    exit_code = 0

    sys.setprofile(hook)
    threading.setprofile(hook)
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            exit_code = 0
        elif isinstance(code, int):
            exit_code = code
        else:
            sys.stderr.write("%s\n" % (code,))
            exit_code = 1
        status = "system_exit"
        detail = str(code)
    except BaseException:
        import traceback

        traceback.print_exc()
        status = "exception"
        detail = repr(sys.exc_info()[1])
        exit_code = 1
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        _write(out_path, repo_root, seen, status, detail)

    return exit_code


def load_trace(path):
    """Read a trace file into [(module, function), ...] for the verifier.

    Tolerates a missing or malformed file by returning an empty list: absence of
    dynamic evidence must degrade to static-only, never raise.
    """
    try:
        handle = open(path)
        try:
            payload = json.load(handle)
        finally:
            handle.close()
    except Exception:
        return []
    entries = payload.get("entries") or []
    result = []
    for entry in entries:
        module = entry.get("module")
        function = entry.get("function")
        if module and function:
            result.append((module, function))
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv))
