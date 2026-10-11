import shutil
from pathlib import Path

from accra_language import AccraError, Config, Dependency

from accra_python import Setup_py


def test_setuppy():
    correct_dependencies: set[Dependency] = {
        Dependency(name="requests", version=""),
        Dependency(name="numpy", version=""),
        Dependency(name="click", version=""),
    }

    project_dir = Path(__file__).resolve().parent / Path("fixtures/setup_py-project")
    tmp_dir = Path("/tmp/setup_py-project")

    shutil.rmtree(tmp_dir, ignore_errors=True)
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
