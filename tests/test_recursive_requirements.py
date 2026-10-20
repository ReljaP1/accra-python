import shutil
from pathlib import Path

from accra_language import AccraError, Config, Dependency

from accra_python import Requirements_txt


def test_recursive_requirements(tmp_path):
    correct_dependencies: set[Dependency] = {
        Dependency(name="requests", version=">=2.31"),
        Dependency(name="packaging", version=">=23.0"),
        Dependency(name="pytest", version=">=8.0"),
    }

    project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/recursive_requirements"
    )
    tmp_dir = tmp_path / "recursive_requirements"

    shutil.copytree(project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir)
        requirements_txt = Requirements_txt(config)

        deps: set[Dependency] | AccraError = requirements_txt.extract_dependencies()
        assert not isinstance(deps, AccraError)

        assert deps == correct_dependencies

    finally:
        shutil.rmtree(tmp_dir)
