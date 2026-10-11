import subprocess
from typing import override

from accra_language import (
    AccraBuildError,
    AccraError,
    AccraInstallError,
    AccraResult,
    Config,
    Dependency,
    DockerfileInstruction,
    EnvironmentManager,
    EnvironmentManagerSpec,
)

from .adhoc import Adhoc
from .pyproject_toml import Pyproject_toml
from .requirements_txt import Requirements_txt
from .setup_cfg import Setup_cfg
from .setup_py import Setup_py


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

    def _install_uv(self) -> AccraError | None:
        curl_result = subprocess.run(
            ["curl", "-LsSf", "https://astral.sh/uv/install.sh"],
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
        )

        if curl_result.returncode != 0:
            return AccraInstallError(
                message=f"could not install environment manager: {self.spec.name}-{self.spec.version}"
            )

        shell_result = subprocess.run(
            ["sh"],
            stdin=curl_result.stdout,
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
        )

        if shell_result.returncode != 0:
            return AccraInstallError(
                message=f"could not install environment manager: {self.spec.name}-{self.spec.version}"
            )

        return None

    def _uv_pyproject_setup(self) -> AccraResult:
        uv_sync_result = subprocess.run(
            ["uv", "sync"],
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
        )

        if uv_sync_result.returncode != 0:
            return AccraResult(
                errors=[AccraBuildError(message="uv sync failed")], success=False
            )

        return AccraResult(dockerfile=[DockerfileInstruction("RUN uv sync")])

    def _uv_non_pyproject_setup(self) -> AccraResult:
        uv_venv_result = subprocess.run(
            ["uv", "venv"],
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
        )

        if uv_venv_result.returncode != 0:
            return AccraResult(
                errors=[AccraBuildError(message="uv venv failed")], success=False
            )

        return AccraResult(dockerfile=[DockerfileInstruction("RUN uv venv")])

    @override
    def setup(self) -> AccraResult:
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
            **self.spec.config.model_dump(),
        )

        if pyenv_result.returncode != 0:
            return AccraResult(
                errors=[
                    AccraInstallError(
                        message=f"pyenv could not install python version: {language_version}"
                    )
                ]
            )

        result.dockerfile.append(
            DockerfileInstruction(f"FROM python:{language_version}"),
        )

        # install uv (if not already installed)
        uv_version_result = subprocess.run(
            ["uv", "--version"],
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
        )

        if uv_version_result.returncode != 0:
            uv_install_error: AccraError | None = self._install_uv()
            if uv_install_error:
                return AccraResult(errors=[uv_install_error], success=False)

        result.dockerfile.append(
            DockerfileInstruction("RUN curl -LsSf https://astral.sh/uv/install.sh | sh")
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
            **self.spec.config.model_dump(),
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
            dockerfile=[
                DockerfileInstruction(
                    f"RUN uv add {dependency.name + dependency.version}"
                )
            ]
        )

    def _install_non_pyproject_dependency(self, dependency: Dependency) -> AccraResult:
        install_result = subprocess.run(
            ["uv", "pip", "install", dependency.name + dependency.version],
            check=False,
            capture_output=True,
            text=True,
            **self.spec.config.model_dump(),
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
            dockerfile=[
                DockerfileInstruction(
                    f"RUN uv pip install {dependency.name + dependency.version}"
                )
            ]
        )

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        result: AccraResult = (
            self._install_pyproject_dependency(dependency)
            if self.is_pyproject_present
            else self._install_non_pyproject_dependency(dependency)
        )

        return result
