import shutil
from pathlib import Path

from accra_language import AccraResult, Config, Dockerfile

from accra_python import (
    _DOCKER_CREATE_USER,
    _DOCKER_SWITCH_USER,
    Python3,
    docker_setup_workdir,
)


def test_uv():
    correct = AccraResult(
        dockerfile=Dockerfile(
            pre_dependencies_installation=[
                "FROM python:3.14",
                *docker_setup_workdir("https://fake_repo"),
                *_DOCKER_CREATE_USER,
                "RUN curl -LsSf https://astral.sh/uv/install.sh | sh",
                "RUN uv sync",
            ],
            dependencies_installation=[
                "RUN uv add httpx",
                "RUN uv add pydantic",
                "RUN uv add rich",
            ],
            post_dependencies_installation=[*_DOCKER_SWITCH_USER],
        )
    )

    py3_sample_project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/py3-sample-project"
    )
    tmp_dir = Path("/tmp/py3-sample-project")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.copytree(py3_sample_project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir, repo="https://fake_repo")
        python3 = Python3(config)

        assert python3.detect()

        result: AccraResult = python3.build()
        assert result.success

        assert result == correct

    finally:
        shutil.rmtree(tmp_dir)
