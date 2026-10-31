import subprocess
from typing import override

from accra_language import (
    AccraBuildError,
    AccraError,
    AccraFailedDependencyInstallError,
    AccraResult,
    Config,
    Dependency,
    Dockerfile,
    DockerfileInstruction,
    EnvironmentManager,
    EnvironmentManagerSpec,
)

from . import utils
from .adhoc import Adhoc
from .pyproject_toml import Pyproject_toml
from .python_environment import PythonEnvironment
from .requirements_txt import Requirements_txt
from .setup_cfg import Setup_cfg
from .setup_py import Setup_py
from .utils import (
    DOCKER_NATIVE_PACKAGES,
    docker_activate_environment,
    docker_install,
    docker_python_toolchain,
    pyenv_project_setup,
)

# the interpreter of the venv uv creates in the project (relative to its root)
_VENV_PYTHON = ".venv/bin/python"


class Uv(PythonEnvironment, EnvironmentManager):
    _ENV_DIR = ".venv"

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
            version_order=utils.version_order,
        )
        super().__init__(spec)

    def _run(self, command: list[str]):
        return utils.run(
            command,
            self.spec.config,
            utils.call_timeout(self),
            extra_env=utils.uv_timeout_env(self.spec.config),
        )

    def _venv_setup(self) -> AccraResult:
        # NOTE seeded with pip: callers list and inspect the environment with it.
        # --clear replaces a venv left by an earlier build or by version
        # detection (`uv run` syncs a pyproject project into .venv)
        uv_venv_result = self._run(
            [
                "uv",
                "venv",
                "--python",
                self.selected_language_version,
                "--seed",
                "--clear",
            ]
        )

        if uv_venv_result.returncode != 0:
            return AccraResult(
                errors=[
                    utils.failure_error(
                        AccraBuildError, "uv venv failed", uv_venv_result
                    )
                ],
                success=False,
            )

        repair_error = self.ensure_pkg_resources()
        if repair_error is not None:
            return AccraResult(errors=[repair_error], success=False)

        workdir = getattr(self.spec.config, "image_workdir", "/project")
        return AccraResult(
            dockerfile=Dockerfile(
                pre_dependencies_installation=[
                    DockerfileInstruction(
                        f"RUN uv venv --seed --python {self.selected_language_version}"
                    ),
                    *docker_activate_environment(".venv", workdir),
                ]
            )
        )

    @override
    def pre_dependencies_installation(self) -> AccraResult:
        result = AccraResult()

        language_version: str | AccraError = self.select_language_version()
        if isinstance(language_version, AccraError):
            return AccraResult(errors=[language_version], success=False)

        # a minor version would make `pyenv install` compile CPython
        language_version = utils.installed_patch(
            language_version, self.spec.config, utils.call_timeout(self)
        )
        self.selected_language_version = language_version

        # install python
        pyenv_error: AccraError | None = pyenv_project_setup(
            language_version, self.spec.config, utils.call_timeout(self)
        )
        if isinstance(pyenv_error, AccraError):
            return AccraResult(errors=[pyenv_error], success=False)

        # the toolchain first: the caller's base image has no Python or uv
        result.dockerfile.pre_dependencies_installation.extend(
            [*docker_python_toolchain(language_version), DOCKER_NATIVE_PACKAGES]
        )

        # setup uv
        # NOTE the venv only, for every project: its dependencies come from the
        # dependency step and the project itself from `post_dependencies_installation()`
        setup_result: AccraResult = self._venv_setup()

        result.extend(setup_result)
        return result

    @override
    def _get_dependencies(self) -> set[Dependency] | None:
        """The manifests' dependencies, without the project itself and the
        packages whose environment markers are false for the selected version
        (`utils.project_dependencies`)."""
        dependencies: set[Dependency] | None = super()._get_dependencies()
        if dependencies is None:
            return None

        return utils.project_dependencies(
            dependencies, self.spec.config.cwd, self.selected_language_version
        )

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        # NOTE `uv pip install`, not `uv add`, for pyproject projects too: uv add
        # rewrites the analyzed project's pyproject.toml and uv.lock and refuses
        # a dependency named like the project
        # NOTE what already imports from the project is not installed (it may
        # be a local module the PyPI package of that name would shadow)
        if self.already_importable(dependency):
            return AccraResult()

        spec: str = dependency.name + dependency.version
        # --python targets the project's venv even with an unrelated VIRTUAL_ENV
        try:
            install_result = self._run(
                ["uv", "pip", "install", "--python", _VENV_PYTHON, spec]
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # a timed-out or unstartable install is a failed dependency, not an
            # exception out of the plugin
            return AccraResult(
                errors=[
                    utils.exception_error(
                        AccraFailedDependencyInstallError,
                        f"{self.spec.name} could not install dependency: {spec}",
                        exc,
                        dependency=dependency,
                        environment_manager=self,
                    )
                ]
            )

        if install_result.returncode != 0:
            return AccraResult(
                errors=[
                    utils.failure_error(
                        AccraFailedDependencyInstallError,
                        f"{self.spec.name} could not install dependency: {spec}",
                        install_result,
                        dependency=dependency,
                        environment_manager=self,
                    )
                ]
            )

        # the Dockerfile pins the installed version; a package that
        # installed but does not import is a failed dependency
        errors = []
        import_error = self.import_check_error(dependency)
        if import_error is not None:
            errors.append(import_error)
        return AccraResult(
            errors=errors,
            dockerfile=Dockerfile(
                dependencies_installation=[
                    docker_install("uv pip install", self.pinned_spec(dependency))
                ]
            ),
        )
