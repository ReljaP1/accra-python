import shutil
from pathlib import Path

import pytest
from accra_language import AccraResult, Config, Dependency, Dockerfile

from accra_python import (
    DOCKER_INSTALL_UV,
    DOCKER_NATIVE_PACKAGES,
    Python3,
    docker_activate_environment,
    docker_python_toolchain,
)


@pytest.mark.integration
def test_uv(tmp_path):
    py3_sample_project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/py3-sample-project"
    )
    tmp_dir = tmp_path / "py3-sample-project"

    shutil.copytree(py3_sample_project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir, repo="https://fake_repo")
        python3 = Python3(config)

        assert python3.detect()

        result: AccraResult = python3.build()
        assert result.success

        # requires-python >= 3.14; an installed 3.14 patch is used when there is one
        version = python3.env_manager.selected_language_version
        assert version.startswith("3.14")
        pinned = {
            name: python3.env_manager.installed_version(
                Dependency(name=name, version="")
            )
            for name in ("httpx", "pydantic", "rich")
        }
        assert all(pinned.values())
        assert result == AccraResult(
            dockerfile=Dockerfile(
                base_image=f"python:{version}",
                toolchain_installation=docker_python_toolchain(version),
                pre_dependencies_installation=[
                    DOCKER_INSTALL_UV,
                    DOCKER_NATIVE_PACKAGES,
                    f"RUN uv venv --seed --python {version}",
                    *docker_activate_environment(".venv"),
                ],
                # the project is not installed by the build;
                # each dependency is pinned to its installed version
                dependencies_installation=[
                    f"RUN uv pip install httpx=={pinned['httpx']}",
                    f"RUN uv pip install pydantic=={pinned['pydantic']}",
                    f"RUN uv pip install rich=={pinned['rich']}",
                ],
            )
        )

    finally:
        shutil.rmtree(tmp_dir)
