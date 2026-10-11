import shutil
from pathlib import Path

from accra_language import AccraError, Config, Dependency

from accra_python import Requirements_txt


def test_recursive_requirements():
    correct_dependencies: set[Dependency] = {
        Dependency(name="requests", version=""),
        Dependency(name="packaging", version=""),
        Dependency(name="pytest", version=""),
    }

    project_dir = Path(__file__).resolve().parent / Path(
        "fixtures/recursive_requirements"
    )
    tmp_dir = Path("/tmp/recursive_requirements")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.copytree(project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir)
        requirements_txt = Requirements_txt(config)

        deps: set[Dependency] | AccraError = requirements_txt.extract_dependencies()
        assert not isinstance(deps, AccraError)

        assert deps == correct_dependencies

    finally:
        shutil.rmtree(tmp_dir)
