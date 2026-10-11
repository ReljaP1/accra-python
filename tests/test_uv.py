import shutil
from pathlib import Path

from accra_language import AccraResult, Config

from accra_python import Python3


def test_uv():
    correct = AccraResult(
        dockerfile=[
            "FROM python:3.14",
            "RUN curl -LsSf https://astral.sh/uv/install.sh | sh",
            "RUN uv sync",
            "RUN uv add httpx",
            "RUN uv add pydantic",
            "RUN uv add rich",
        ]
    )

    uv_sample_project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/uv-sample-project"
    )
    tmp_dir = Path("/tmp/uv-sample-project")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.copytree(uv_sample_project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir)
        python3 = Python3(config)

        assert python3.detect()

        result: AccraResult = python3.build()
        assert result.success

        assert result == correct

    finally:
        shutil.rmtree(tmp_dir)
