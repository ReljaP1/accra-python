from functools import cache

from accra_language import DockerfileInstruction
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_DOCKER_SETUP_WORKDIR: list[DockerfileInstruction] = [
    DockerfileInstruction("WORKDIR /app")
]

_DOCKER_CREATE_USER: list[DockerfileInstruction] = [
    DockerfileInstruction(
        'RUN adduser \
                --disabled-password \
                --gecos "" \
                --home "/home/appuser" \
                --shell "/bin/bash" \
                --uid "10001" \
                appuser'
    )
]

_DOCKER_SWITCH_USER: list[DockerfileInstruction] = [
    DockerfileInstruction("RUN chown -R appuser:appuser /app"),
    DockerfileInstruction("USER appuser"),
]


@cache
def get_python_minor_versions(requires_python: str = ">=0") -> set[str]:
    # NOTE pyenv cannot install minor version < 5 on Debian
    MIN_MINOR_VERSION = 5
    MAX_MINOR_VERSION = 14

    all_versions: set[str] = {
        f"3.{i}" for i in range(MIN_MINOR_VERSION, MAX_MINOR_VERSION + 1)
    }

    parsed_versions: set[str] = {
        v for v in all_versions if SpecifierSet(requires_python).contains(Version(v))
    }

    return parsed_versions
