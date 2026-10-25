import pytest
from accra_language import Config

from accra_python import Uv, Venv
from accra_python.utils import version_order


@pytest.mark.parametrize("env_manager", [Uv, Venv])
def test_the_managers_rank_versions_by_accras_policy(tmp_path, env_manager):
    manager = env_manager(Config(cwd=tmp_path))
    order = manager.spec.version_order

    assert order is version_order
    assert order({"3.5", "3.9", "3.10", "3.11"})[0] == "3.11"
    assert order({"3.5", "3.9", "3.10"})[0] == "3.10"
    assert order({"3.13", "3.6", "3.12"})[0] == "3.12"
    assert order({"3.13", "3.14", "3.5"})[0] == "3.14"
    assert order({"3.11.2", "3.11.10"})[0] == "3.11.10"


def test_version_order_is_the_whole_ordered_list():
    # 3.11, 3.10, 3.9, 3.8, 3.7, 3.12, 3.6, then the rest, highest first
    versions = {f"3.{minor}" for minor in range(5, 15)}
    assert version_order(versions) == [
        "3.11",
        "3.10",
        "3.9",
        "3.8",
        "3.7",
        "3.12",
        "3.6",
        "3.14",
        "3.13",
        "3.5",
    ]
    assert version_order({"3.11.2", "3.11.10", "3.9"}) == ["3.11.10", "3.11.2", "3.9"]
    assert version_order(set()) == []


@pytest.mark.parametrize("env_manager", [Uv, Venv])
def test_ranked_language_versions_of_a_project(tmp_path, env_manager):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='x', python_requires='>=3.8')\n"
    )
    manager = env_manager(Config(cwd=tmp_path))

    ranked = manager.ranked_language_versions()
    assert ranked[:5] == ["3.11", "3.10", "3.9", "3.8", "3.12"]
    assert ranked[-2:] == ["3.14", "3.13"]
    assert manager.select_language_version() == "3.11"
