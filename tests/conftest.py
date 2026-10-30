"""Tests marked `integration` need the real pyenv/uv toolchain and the network
(PyPI); they are skipped, not failed, when either is missing."""

import functools
import shutil
import socket

import pytest

TOOLS = ("pyenv", "uv")


@functools.cache
def _pypi_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
    except OSError:
        return False
    return True


def pytest_collection_modifyitems(config, items):
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    reason = f"needs {', '.join(missing)} on PATH" if missing else ""
    for item in items:
        if item.get_closest_marker("integration") is None:
            continue
        if missing:
            item.add_marker(pytest.mark.skip(reason=reason))
        elif not _pypi_reachable():
            item.add_marker(pytest.mark.skip(reason="needs network access to PyPI"))
