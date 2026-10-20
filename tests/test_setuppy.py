import shutil
from pathlib import Path

from accra_language import AccraError, Config, Dependency

from accra_python import Setup_py


def test_setuppy(tmp_path):
    # the versions are kept
    correct_dependencies: set[Dependency] = {
        Dependency(name="requests", version=">=2.28"),
        Dependency(name="numpy", version=">=1.24"),
        Dependency(name="click", version=">=8.1"),
    }

    project_dir = Path(__file__).resolve().parent / Path("fixtures/setup_py-project")
    tmp_dir = tmp_path / "setup_py-project"

    shutil.copytree(project_dir, tmp_dir)

    try:
        config = Config(cwd=tmp_dir)
        setuppy = Setup_py(config)

        assert setuppy.detect()

        deps: set[Dependency] | AccraError = setuppy.extract_dependencies()
        assert not isinstance(deps, AccraError)

        assert deps == correct_dependencies

    finally:
        shutil.rmtree(tmp_dir)
