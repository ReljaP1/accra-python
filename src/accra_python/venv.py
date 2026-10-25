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


class Venv(PythonEnvironment, EnvironmentManager):
    _ENV_DIR = "venv"

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
            version_order=utils.version_order,
        )
        super().__init__(spec)

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

        # setup venv
        # PYENV_VERSION selects the interpreter without `pyenv local`, which
        # would write .python-version into the analyzed project
        venv_result = utils.run(
            ["python", "-m", "venv", "venv"],
            self.spec.config,
            utils.call_timeout(self),
            extra_env={"PYENV_VERSION": language_version},
        )

        if venv_result.returncode != 0:
            result.extend(
                AccraResult(
                    errors=[
                        utils.failure_error(
                            AccraBuildError, "failed to setup venv", venv_result
                        )
                    ],
                    success=False,
                )
            )
            return result

        repair_error = self.ensure_pkg_resources()
        if repair_error is not None:
            result.extend(AccraResult(errors=[repair_error], success=False))
            return result

        result.dockerfile.pre_dependencies_installation.append(
            DockerfileInstruction(
                f"RUN $(uv python find {language_version} 2>/dev/null || command -v python)"
                " -m venv venv"
            )
        )
        result.dockerfile.pre_dependencies_installation.extend(
            docker_activate_environment(
                "venv", getattr(self.spec.config, "image_workdir", "/project")
            )
        )

        return result

    @override
    def _get_dependencies(self) -> set[Dependency] | None:
        """The manifests' dependencies, without the project itself
        (`utils.project_dependencies`)."""
        dependencies: set[Dependency] | None = super()._get_dependencies()
        if dependencies is None:
            return None

        return utils.project_dependencies(dependencies, self.spec.config.cwd)

    @override
    def _install_dependency(self, dependency: Dependency) -> AccraResult:
        # NOTE what already imports from the project is not installed (it may
        # be a local module the PyPI package of that name would shadow)
        if self.already_importable(dependency):
            return AccraResult()

        install_result = utils.run(
            [
                "venv/bin/python",
                "-m",
                "pip",
                "install",
                "--retries",
                utils.PIP_RETRIES,
                dependency.name + dependency.version,
            ],
            self.spec.config,
            utils.call_timeout(self),
        )

        if install_result.returncode != 0:
            return AccraResult(
                errors=[
                    utils.failure_error(
                        AccraFailedDependencyInstallError,
                        "pip could not install dependency: "
                        f"{dependency.name + dependency.version}",
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
                    docker_install(
                        "venv/bin/python -m pip install",
                        self.pinned_spec(dependency),
                    )
                ]
            ),
        )

    @override
    def post_dependencies_installation(self) -> AccraResult:
        # the user and `chown` are the composing caller's
        return AccraResult()
