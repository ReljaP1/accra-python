"""Usage scripts in Python -- the profile, the check and the reading."""

import accra_language
import pytest
from accra_language import ApiSymbol, Config

from accra_python import Python3
from accra_python.scripts import _import_aliases

SYMBOLS = [
    ApiSymbol(module="toolbox/math_ops.py", name="add_numbers"),
    ApiSymbol(module="toolbox/text_ops.py", name="stylize_text"),
    ApiSymbol(module="toolbox/utils.py", name="log_message", returns_value=False),
    ApiSymbol(module="main.py", name="main", is_driver=True),
    ApiSymbol(module="toolbox/fmt.py", name="Formatter.render", kind="method"),
]


def _python3(tmp_path):
    return Python3(Config(cwd=tmp_path))


def test_profile(tmp_path):
    profile = _python3(tmp_path).script_profile()
    assert (profile.language, profile.file_suffix, profile.code_fence) == (
        "Python",
        ".py",
        "python",
    )
    assert profile.conventions == (
        "Do not use pytest or any external testing library.",
    )
    assert profile.run_command.format(file="script.py") == "python script.py"
    assert profile.cli_frameworks == ("argparse", "click", "typer")
    assert profile.project_files == ("setup.py", "pyproject.toml")


def test_check_script(tmp_path):
    python3 = _python3(tmp_path)
    good = tmp_path / "good.py"
    good.write_text("print('hi')\n")
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n")
    assert python3.check_script(good) is None
    error = python3.check_script(bad)
    assert (
        isinstance(error, accra_language.AccraError) and "SyntaxError" in error.message
    )
    assert isinstance(
        python3.check_script(tmp_path / "missing.py"), accra_language.AccraError
    )


def _read(tmp_path, source, symbols=None, inferred=()):
    return _python3(tmp_path).read_script(source, symbols or SYMBOLS, inferred)


def test_subprocess_only(tmp_path):
    reading = _read(
        tmp_path,
        "import subprocess, sys\nsubprocess.run([sys.executable, 'main.py'])\n",
    )
    assert reading.launched_subprocess and not reading.calls_project_api


def test_driver_only(tmp_path):
    reading = _read(tmp_path, "from main import main\nmain()\n")
    assert reading.calls_only_entry_point and reading.driver_symbols_called == ("main",)


def test_demonstrative(tmp_path):
    reading = _read(
        tmp_path,
        "from toolbox.math_ops import add_numbers\n"
        "from toolbox.text_ops import stylize_text\n"
        "total = add_numbers(2, 3)\n"
        "print(total, stylize_text('a b'))\n",
    )
    assert reading.calls_project_api
    assert reading.builds_own_arguments and reading.reports_returned_value
    assert reading.demonstrated_symbols == ("add_numbers", "stylize_text")
    assert reading.distinct_api_symbols == 2


def test_side_effect_symbol_is_not_counted(tmp_path):
    reading = _read(
        tmp_path, "from toolbox.utils import log_message\nlog_message('hi')\n"
    )
    assert reading.calls_project_api and reading.distinct_api_symbols == 0


def test_inferred_input_passthrough_is_not_built(tmp_path):
    copied = _read(
        tmp_path,
        "from toolbox.text_ops import stylize_text\nprint(stylize_text('World'))\n",
        inferred={"World"},
    )
    built = _read(
        tmp_path,
        "from toolbox.text_ops import stylize_text\nprint(stylize_text('a b c'))\n",
        inferred={"World"},
    )
    assert not copied.builds_own_arguments and built.builds_own_arguments


@pytest.mark.parametrize(
    "script",
    [
        "from toolbox.math_ops import add_numbers\nprint(add_numbers(1, 2))\n",
        "from toolbox.math_ops import add_numbers as add\nprint(add(1, 2))\n",
        "from toolbox import math_ops\nprint(math_ops.add_numbers(1, 2))\n",
        "import toolbox.math_ops\nprint(toolbox.math_ops.add_numbers(1, 2))\n",
        "import toolbox.math_ops as m\nprint(m.add_numbers(1, 2))\n",
    ],
)
def test_import_forms(tmp_path, script):
    assert _read(tmp_path, script).demonstrated_symbols == ("add_numbers",)


def test_src_layout_and_aliases(tmp_path):
    symbols = [ApiSymbol(module="src/toolbox/math_ops.py", name="add_numbers")]
    reading = _read(
        tmp_path,
        "from toolbox.math_ops import add_numbers\nprint(add_numbers(2, 3))\n",
        symbols,
    )
    assert reading.calls_project_api and not reading.launched_subprocess
    assert _import_aliases("src/pkg/mod.py") == ["src.pkg.mod", "pkg.mod"]
    assert _import_aliases("src.py") == ["src"]


def test_method_by_unambiguous_leaf(tmp_path):
    reading = _read(
        tmp_path,
        "from toolbox.fmt import Formatter\nf = Formatter()\nprint(f.render('x'))\n",
    )
    assert "Formatter.render" in reading.demonstrated_symbols


def test_parse_error_is_soft(tmp_path):
    reading = _read(tmp_path, "def (:\n")
    assert reading.parse_error and not reading.calls_project_api
