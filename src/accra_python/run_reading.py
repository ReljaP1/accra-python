"""Reading a Python run's failure output: Python 3's `ModuleNotFoundError`
(`No module named 'x'`), `FileNotFoundError` (`No such file or directory:
'x'`) and argparse's usage error (`the following arguments are required:
...`). Each name is kept as often and in the order the output gives it.
"""

from __future__ import annotations

import re

from accra_language import RunDiagnosis

from . import utils

_MISSING_MODULE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_MISSING_FILE = re.compile(r"No such file or directory: '([^']+)'")
_MISSING_ARGUMENTS = re.compile(r"the following arguments are required: ([^\n]+)")


def diagnose_run(output: str) -> RunDiagnosis:
    text = output or ""
    return RunDiagnosis(
        missing_dependencies=tuple(_MISSING_MODULE.findall(text)),
        missing_files=tuple(_MISSING_FILE.findall(text)),
        missing_arguments=tuple(_MISSING_ARGUMENTS.findall(text)),
    )


def standard_modules() -> frozenset[str]:
    """The standard library's top-level module names, of any Python 3 version
    the plugin builds (`utils.standard_modules`)."""
    return utils.standard_modules()
