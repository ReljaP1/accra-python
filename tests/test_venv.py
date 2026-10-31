import shutil
from pathlib import Path

import pytest
from accra_language import AccraResult, Config, Dependency, Dockerfile

from accra_python import (
    DOCKER_NATIVE_PACKAGES,
    Python3,
    Venv,
    docker_activate_environment,
    docker_python_toolchain,
)


@pytest.mark.integration
def test_venv(tmp_path):
    py3_sample_project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/py3-sample-project"
    )
    tmp_dir = tmp_path / "py3-sample-project"

    shutil.copytree(py3_sample_project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir, repo="https://fake_repo")
        venv = Venv(config)
        python3 = Python3(config=config, env_manager=venv)

        assert python3.detect()

        result: AccraResult = python3.build()
        assert result.success

        version = venv.selected_language_version
        assert version.startswith("3.14")
        pinned = {
            name: venv.installed_version(Dependency(name=name, version=""))
            for name in ("httpx", "pydantic", "rich")
        }
        assert all(pinned.values())
        # the packages the direct lines do not pin follow in one `--no-deps` line
        transitive = venv.transitive_pins()
        assert len(transitive) == 1
        assert str(transitive[0]).startswith(
            "RUN venv/bin/python -m pip install --no-deps "
        )
        assert not any(f" {name}==" in str(transitive[0]) for name in pinned)
        assert result == AccraResult(
            dockerfile=Dockerfile(
                pre_dependencies_installation=[
                    *docker_python_toolchain(version),
                    DOCKER_NATIVE_PACKAGES,
                    (
                        f"RUN $(uv python find {version} 2>/dev/null || command -v python)"
                        " -m venv venv"
                    ),
                    *docker_activate_environment("venv"),
                ],
                dependencies_installation=[
                    # pinned to the installed version
                    f"RUN venv/bin/python -m pip install httpx=={pinned['httpx']}",
                    f"RUN venv/bin/python -m pip install pydantic=={pinned['pydantic']}",
                    f"RUN venv/bin/python -m pip install rich=={pinned['rich']}",
                    *transitive,
                ],
                # then the project itself
                post_dependencies_installation=[
                    "RUN python -m pip install --no-cache-dir ."
                ],
            )
        )

    finally:
        shutil.rmtree(tmp_dir)
