import shutil
from pathlib import Path

import pytest
from accra_language import AccraResult, Config, Dependency, Dockerfile

from accra_python import (
    DOCKER_NATIVE_PACKAGES,
    Python3,
    docker_activate_environment,
    docker_python_toolchain,
)


@pytest.mark.integration
def test_adhoc_package_resolution(tmp_path):
    project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/adhoc-module-project"
    )
    tmp_dir = tmp_path / "adhoc-module-project"

    shutil.copytree(project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir, repo="https://fake_repo")
        python3 = Python3(config)

        assert python3.detect()

        result: AccraResult = python3.build()
        assert result.success

        # no manifest limits the version, so the preference order picks 3.11 (its
        # installed patch when there is one)
        version = python3.env_manager.selected_language_version
        assert version == "3.11" or version.startswith("3.11.")
        # the Dockerfile pins the installed version
        pyyaml = python3.env_manager.installed_version(
            Dependency(name="PyYAML", version="")
        )
        assert pyyaml
        assert result == AccraResult(
            dockerfile=Dockerfile(
                pre_dependencies_installation=[
                    *docker_python_toolchain(version),
                    DOCKER_NATIVE_PACKAGES,
                    f"RUN uv venv --seed --python {version}",
                    *docker_activate_environment(".venv"),
                ],
                dependencies_installation=[f"RUN uv pip install PyYAML=={pyyaml}"],
            )
        )

    finally:
        shutil.rmtree(tmp_dir)


def test_usage_patterns_add_the_packages_they_need(tmp_path):
    from accra_python.adhoc import Adhoc, usage_packages

    assert usage_packages("from pydantic import EmailStr") == {"pydantic[email]"}
    assert usage_packages("x = Form(...)\nctx = CryptContext()") == {
        "python-multipart",
        "passlib",
    }
    assert usage_packages("print 'py2 code still scanned', EmailStr") == {
        "pydantic[email]"
    }
    (tmp_path / "app.py").write_text("from pydantic import BaseModel, EmailStr\n")
    names = {d.name for d in Adhoc(Config(cwd=tmp_path)).extract_dependencies()}
    assert {"pydantic", "pydantic[email]"} <= names
