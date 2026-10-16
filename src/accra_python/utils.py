import subprocess
from functools import cache

from accra_language import AccraError, AccraInstallError, Config, DockerfileInstruction
from packaging.specifiers import SpecifierSet
from packaging.version import Version


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


def pyenv_project_setup(python_version: str, config: Config) -> AccraError | None:
    pyenv_result = subprocess.run(
        ["pyenv", "install", "--skip-existing", python_version],
        check=False,
        capture_output=True,
        text=True,
        cwd=config.cwd,
        env=config.env,
        shell=config.shell,
        timeout=config.timeout,
    )

    if pyenv_result.returncode != 0:
        return AccraInstallError(
            message=f"pyenv could not install python version: {python_version}"
        )

    pyenv_result = subprocess.run(
        ["pyenv", "local", python_version],
        check=False,
        capture_output=True,
        text=True,
        cwd=config.cwd,
        env=config.env,
        shell=config.shell,
        timeout=config.timeout,
    )

    if pyenv_result.returncode != 0:
        return AccraInstallError(
            message=f"pyenv could not set python version: {python_version}"
        )


@cache
def docker_setup_workdir(repo: str) -> list[DockerfileInstruction]:
    return [
        DockerfileInstruction(f"RUN git clone {repo} /project"),
        DockerfileInstruction("WORKDIR /project"),
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
    DockerfileInstruction("RUN chown -R appuser:appuser /project"),
    DockerfileInstruction("USER appuser"),
]
