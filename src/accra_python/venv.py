import subprocess
from typing import override

from accra_language import (
    AccraBuildError,
    AccraError,
    AccraInstallError,
    AccraResult,
    Config,
    Dependency,
    Dockerfile,
    DockerfileInstruction,
    EnvironmentManager,
    EnvironmentManagerSpec,
)

from .adhoc import Adhoc
from .pyproject_toml import Pyproject_toml
from .requirements_txt import Requirements_txt
from .setup_cfg import Setup_cfg
from .setup_py import Setup_py
from .utils import (
    _DOCKER_CREATE_USER,
    _DOCKER_SWITCH_USER,
    docker_setup_workdir,
    pyenv_project_setup,
)


class Venv(EnvironmentManager):
    def __init__(self, config: Config):
        spec = EnvironmentManagerSpec(
            name="venv",
            version="",
            supported_manifests={
                Adhoc(config),
                Pyproject_toml(config),
                Requirements_txt(config),
                Setup_cfg(config),
                Setup_py(config),
            },
            config=config,
        )
        super().__init__(spec)

    @override
    def pre_dependencies_installation(self) -> AccraResult:
        result = AccraResult()

        language_version: str | AccraError = self.select_language_version()
        if isinstance(language_version, AccraError):
            return AccraResult(errors=[language_version], success=False)

        # install python
        pyenv_error: AccraError | None = pyenv_project_setup(
            language_version, self.spec.config
        )
        if isinstance(pyenv_error, AccraError):
            return AccraResult(errors=[pyenv_error], success=False)

        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction(f"FROM python:{language_version}")
        )

        result.dockerfile.pre_dependencies_installation.extend(
            docker_setup_workdir(self.spec.config.repo) + _DOCKER_CREATE_USER
        )

        # setup venv
        venv_result = subprocess.run(
            ["python", "-m", "venv", "venv"],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if venv_result.returncode != 0:
            result.extend(
                AccraResult(
                    errors=[AccraBuildError(message="failed to setup venv")],
                    success=False,
                )
            )
            return result

        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction("RUN python -m venv venv")
        )

        return result

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        install_result = subprocess.run(
            [
                "venv/bin/python",
                "-m",
                "pip",
                "install",
                dependency.name + dependency.version,
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if install_result.returncode != 0:
            return AccraResult(
                errors=[
                    AccraInstallError(
                        message=f"pip could not install dependency: {dependency.name + dependency.version}"
                    )
                ]
            )

        return AccraResult(
            dockerfile=Dockerfile(
                dependencies_installation=[
                    DockerfileInstruction(
                        f"RUN venv/bin/python -m pip install {dependency.name + dependency.version}"
                    )
                ]
            )
        )

    @override
    def post_dependencies_installation(self) -> AccraResult:
        return AccraResult(
            dockerfile=Dockerfile(post_dependencies_installation=_DOCKER_SWITCH_USER)
        )
