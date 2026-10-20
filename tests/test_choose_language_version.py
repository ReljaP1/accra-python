import pytest
from accra_language import Config

from accra_python import Uv, Venv


@pytest.mark.parametrize("env_manager", [Uv, Venv])
def test_choose_language_version_follows_accras_policy(tmp_path, env_manager):
    # 3.11, 3.10, 3.9, 3.8, 3.7, 3.12, 3.6 in that order, else the highest
    manager = env_manager(Config(cwd=tmp_path))

    assert manager.choose_language_version({"3.5", "3.9", "3.10", "3.11"}) == "3.11"
    assert manager.choose_language_version({"3.5", "3.9", "3.10"}) == "3.10"
    assert manager.choose_language_version({"3.13", "3.6", "3.12"}) == "3.12"
    assert manager.choose_language_version({"3.13", "3.14", "3.5"}) == "3.14"
    assert manager.choose_language_version({"3.11.2", "3.11.10"}) == "3.11.10"
