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
from .utils import _DOCKER_CREATE_USER, _DOCKER_SETUP_WORKDIR, _DOCKER_SWITCH_USER


class Uv(EnvironmentManager):
    def __init__(self, config: Config):
        spec = EnvironmentManagerSpec(
            name="uv",
            version="0.11",
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

        self.is_pyproject_present: bool = bool(
            [
                manifest
                for manifest in self.present_manifests
                if isinstance(manifest, Pyproject_toml)
            ]
        )

    def _uv_pyproject_setup(self) -> AccraResult:
        uv_sync_result = subprocess.run(
            ["uv", "sync"],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if uv_sync_result.returncode != 0:
            return AccraResult(
                errors=[AccraBuildError(message="uv sync failed")], success=False
            )

        return AccraResult(
            dockerfile=Dockerfile(
                pre_dependencies_installation=[DockerfileInstruction("RUN uv sync")]
            )
        )

    def _uv_non_pyproject_setup(self) -> AccraResult:
        uv_venv_result = subprocess.run(
            ["uv", "venv", "--python", self.selected_language_version, "--seed"],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if uv_venv_result.returncode != 0:
            return AccraResult(
                errors=[AccraBuildError(message="uv venv failed")], success=False
            )

        return AccraResult(
            dockerfile=Dockerfile(
                pre_dependencies_installation=[DockerfileInstruction("RUN uv venv")]
            )
        )

    @override
    def pre_dependencies_installation(self) -> AccraResult:
        result = AccraResult()

        language_version: str | AccraError = self.select_language_version()
        if isinstance(language_version, AccraError):
            return AccraResult(errors=[language_version], success=False)

        # install python
        pyenv_result = subprocess.run(
            ["pyenv", "install", "--skip-existing", language_version],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.spec.config.cwd,
            env=self.spec.config.env,
            shell=self.spec.config.shell,
            timeout=self.spec.config.timeout,
        )

        if pyenv_result.returncode != 0:
            return AccraResult(
                errors=[
                    AccraInstallError(
                        message=f"pyenv could not install python version: {language_version}"
                    )
                ]
            )

        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction(f"FROM python:{language_version}")
        )

        result.dockerfile.pre_dependencies_installation.extend(
            _DOCKER_SETUP_WORKDIR + _DOCKER_CREATE_USER
        )
        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction("RUN curl -LsSf https://astral.sh/uv/install.sh | sh")
        )
        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction(f"RUN git clone {self.spec.config.repo}")
        )

        # setup uv
        setup_result: AccraResult = (
            self._uv_pyproject_setup()
            if self.is_pyproject_present
            else self._uv_non_pyproject_setup()
        )

        result.extend(setup_result)
        return result

    def _install_pyproject_dependency(self, dependency: Dependency) -> AccraResult:
        install_result = subprocess.run(
            ["uv", "add", dependency.name + dependency.version],
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
                        message=f"{self.spec.name} could not install dependency: {dependency.name + dependency.version}"
                    )
                ]
            )

        return AccraResult(
            dockerfile=Dockerfile(
                dependencies_installation=[
                    DockerfileInstruction(
                        f"RUN uv add {dependency.name + dependency.version}"
                    )
                ]
            )
        )

    def _install_non_pyproject_dependency(self, dependency: Dependency) -> AccraResult:
        install_result = subprocess.run(
            ["uv", "pip", "install", dependency.name + dependency.version],
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
                        message=f"{self.spec.name} could not install dependency: {dependency.name + dependency.version}"
                    )
                ]
            )

        return AccraResult(
            dockerfile=Dockerfile(
                dependencies_installation=[
                    DockerfileInstruction(
                        f"RUN uv pip install {dependency.name + dependency.version}"
                    )
                ]
            )
        )

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        result: AccraResult = (
            self._install_pyproject_dependency(dependency)
            if self.is_pyproject_present
            else self._install_non_pyproject_dependency(dependency)
        )

        return result

    @override
    def post_dependencies_installation(self) -> AccraResult:
        return AccraResult(
            dockerfile=Dockerfile(post_dependencies_installation=_DOCKER_SWITCH_USER)
        )
