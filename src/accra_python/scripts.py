"""Usage scripts in Python: how one is written, checked and read.

ACCRa writes a usage script for the project (with a language model), runs it
through `entry_point_for` and asks what it demonstrates. The Python part of that
lives here: the script profile for the prompt, a syntax check, and a static
reading of the script against the project's public symbols -- which of them it
imports and calls in its own process, whether it builds its own arguments and
reports what the calls return, and whether it launches a subprocess instead.
The classification of a reading (outcome class, passing) is ACCRa's.

Stdlib only, so it runs anywhere the repository can be parsed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from accra_language import (
    AccraError,
    AccraRunError,
    ApiSymbol,
    ScriptProfile,
    ScriptReading,
)

# The profile ACCRa's prompts use for Python.
PYTHON_SCRIPT_PROFILE = ScriptProfile(
    language="Python",
    file_suffix=".py",
    code_fence="python",
    conventions=("Do not use pytest or any external testing library.",),
    run_command="python {file}",
    cli_frameworks=("argparse", "click", "typer"),
    project_files=("setup.py", "pyproject.toml"),
    # how ACCRa's guessers recognise Python commands in documentation
    command_pattern=r"python(?:3)?",
    usage_patterns=(
        r"pip(?:3)?\s+install",
        r"conda\s+install",
        r"python(?:3)?\s+-m\s+",
        r"python(?:3)?\s+\w[\w./]*\.py",
        r"requirements(?:[-_]\w+)?\.txt",
        r"```\s*python",
        r"\$\s*python(?:3)?",
        r"\$\s*pip(?:3)?",
    ),
    package_registry="PyPI",
)


def check_script(path: Path) -> AccraError | None:
    """None when the file at `path` compiles as Python (no code runs)."""
    try:
        source = Path(path).read_text(encoding="utf-8")
        compile(source, str(path), "exec")
    except OSError as exc:
        return AccraRunError(message=f"could not read {path}: {exc}")
    except (
        SyntaxError,
        ValueError,
        UnicodeDecodeError,
        RecursionError,
        MemoryError,
    ) as exc:
        return AccraRunError(message=f"{type(exc).__name__}: {exc}")
    return None


def read_script(
    source: str,
    symbols: Iterable[ApiSymbol],
    inferred_inputs: Iterable[str] = (),
) -> ScriptReading:
    """What `source` does with the project's public `symbols`."""
    return _ScriptReader(symbols, {str(v) for v in inferred_inputs}).read(source)


# In-process API use is the whole point; these hand work to another interpreter
# or shell, so a symbol reached this way is not demonstrated.
_SUBPROCESS_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system",
        "os.execv",
        "os.execvp",
        "os.spawnv",
        "runpy.run_path",
        "runpy.run_module",
    }
)

# Expression kinds that count as "the script built this argument itself" (builds_own_arguments).
_CONSTRUCTED_NODES = (
    ast.Constant,
    ast.JoinedStr,
    ast.BinOp,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
)


def module_path_to_dotted(module_path: str) -> str:
    """`toolbox/text_ops.py` -> `toolbox.text_ops`; `pkg/__init__.py` -> `pkg`."""
    path = module_path.replace("\\", "/")
    path = path.removesuffix(".py")
    path = path.removesuffix("/__init__")
    return path.strip("/").replace("/", ".")


# Directory levels that package a project without being part of its import path.
# `src/scores/x.py` is imported as `scores.x`; `src` never appears in the name.
_PACKAGING_ROOTS = ("src", "lib", "python", "source")


def _import_aliases(module_path: str) -> list[str]:
    """Every dotted name a script might import this module by, most literal first.

    A repository-relative path is not an import path. Under the `src/` layout the
    two differ by exactly one leading segment, and taking the path at face value
    silently credits a script with nothing.
    """
    dotted = module_path_to_dotted(module_path)
    if not dotted:
        return []
    aliases = [dotted]
    parts = dotted.split(".")
    # Strip packaging prefixes one level at a time: `src.pkg.mod` -> `pkg.mod`,
    # and `python.src.pkg.mod` -> `src.pkg.mod` -> `pkg.mod`.
    while len(parts) > 1 and parts[0].lower() in _PACKAGING_ROOTS:
        parts = parts[1:]
        aliases.append(".".join(parts))
    return aliases


def _dotted_name(node: ast.AST) -> str | None:
    """Render `a.b.c` from Attribute/Name chains; None for anything else."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


class _ImportResolver(ast.NodeVisitor):
    """Map local names in the script back to repository modules and symbols.

    Handles `from pkg.mod import sym`, `from pkg.mod import sym as alias`,
    `from pkg import mod`, `import pkg.mod`, and `import pkg.mod as alias`.
    """

    def __init__(self, dotted_to_module: dict[str, str]) -> None:
        self.dotted_to_module = dotted_to_module
        # local name -> (module rel path, symbol name)
        self.symbol_bindings: dict[str, tuple[str, str]] = {}
        # local name -> module rel path  (module or package bound as a name)
        self.module_bindings: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            # `from pkg.mod import sym`
            module = self.dotted_to_module.get(base)
            if module is not None:
                self.symbol_bindings[local] = (module, alias.name)
            # `from pkg import mod` — the imported name is itself a module
            submodule = self.dotted_to_module.get(f"{base}.{alias.name}".strip("."))
            if submodule is not None:
                self.module_bindings[local] = submodule
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = self.dotted_to_module.get(alias.name)
            if module is None:
                continue
            if alias.asname:
                self.module_bindings[alias.asname] = module
            else:
                # `import pkg.mod` binds `pkg`, but calls read `pkg.mod.sym`;
                # register the full dotted path so _dotted_name matches.
                self.module_bindings[alias.name] = module
        self.generic_visit(node)


def _script_constructed_names(tree: ast.Module) -> set[str]:
    """Names assigned from an expression the script itself built (builds_own_arguments)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, _CONSTRUCTED_NODES):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, _CONSTRUCTED_NODES)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def _arg_is_constructed(
    arg: ast.AST,
    constructed_names: set[str],
    inferred_inputs: set[str],
) -> bool:
    """True when this argument was built by the script, not copied in."""
    try:
        source = ast.unparse(arg)
    except (ValueError, RecursionError, MemoryError, TypeError):
        source = ""
    # An argument copied verbatim from an inferred CLI input is not constructed.
    if source and source.strip("'\"") in inferred_inputs:
        return False
    if isinstance(arg, _CONSTRUCTED_NODES):
        return True
    return bool(isinstance(arg, ast.Name) and arg.id in constructed_names)


class _ScriptReader:
    """Static reading of one script against the project's public symbols."""

    def __init__(self, symbols, inferred_inputs: set[str] | None = None) -> None:
        self.symbols = list(symbols)
        self.inferred_inputs = {str(v) for v in (inferred_inputs or set())}

        # A module is registered under every dotted name a script could plausibly
        # import it by. The literal path is not enough: under the `src/` layout,
        # `src/scores/sample_data.py` yields the key `src.scores.sample_data`,
        # while the installed package is imported as `scores.sample_data`.
        self.dotted_to_module: dict[str, str] = {}
        for symbol in self.symbols:
            for dotted in _import_aliases(symbol.module):
                self.dotted_to_module.setdefault(dotted, symbol.module)

        # (module, leaf symbol name) -> Symbol
        self.by_module_leaf: dict[tuple[str, str], ApiSymbol] = {}
        for symbol in self.symbols:
            leaf = symbol.name.split(".")[-1]
            self.by_module_leaf.setdefault((symbol.module, leaf), symbol)

    # -- resolution ------------------------------------------------------

    def _resolve_call(self, func: ast.AST, resolver: _ImportResolver):
        """Return the repository Symbol this call targets, or None."""
        if isinstance(func, ast.Name):
            binding = resolver.symbol_bindings.get(func.id)
            if binding is not None:
                return self.by_module_leaf.get(binding)
            return None

        if isinstance(func, ast.Attribute):
            dotted = _dotted_name(func)
            if dotted is None:
                return None
            head, _, leaf = dotted.rpartition(".")
            # `mod.sym` / `alias.sym` / `pkg.mod.sym`
            module = resolver.module_bindings.get(head)
            if module is None:
                module = self.dotted_to_module.get(head)
            if module is not None:
                return self.by_module_leaf.get((module, leaf))
            # `obj.method()` where obj came from a repo class: attribute the
            # method by leaf name when it is unambiguous across the surface.
            candidates = [
                symbol
                for (mod, name), symbol in self.by_module_leaf.items()
                if name == leaf and symbol.kind == "method"
            ]
            if len(candidates) == 1:
                return candidates[0]
            return None

        return None

    # -- analysis --------------------------------------------------------

    def read(self, source: str) -> ScriptReading:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            return ScriptReading(parse_error=f"{type(exc).__name__}: {exc}")

        resolver = _ImportResolver(self.dotted_to_module)
        resolver.visit(tree)
        constructed_names = _script_constructed_names(tree)

        demonstrated: dict[str, ApiSymbol] = {}
        drivers: set[str] = set()
        launched_subprocess = False
        builds_own_arguments = False
        # Names bound to the result of a repository call, for reports_returned_value.
        result_names: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            dotted = _dotted_name(node.func)
            if dotted in _SUBPROCESS_CALLS:
                launched_subprocess = True
                continue

            symbol = self._resolve_call(node.func, resolver)
            if symbol is None:
                continue

            if symbol.is_driver:
                drivers.add(symbol.name)
                continue

            demonstrated[symbol.name] = symbol

            if not builds_own_arguments:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if _arg_is_constructed(
                        arg, constructed_names, self.inferred_inputs
                    ):
                        builds_own_arguments = True
                        break

        # reports_returned_value needs to know which locals hold a repository call's result.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and self._resolve_call(node.value.func, resolver) is not None
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        result_names.add(target.id)

        return ScriptReading(
            calls_project_api=bool(demonstrated),
            calls_only_entry_point=bool(drivers) and not demonstrated,
            builds_own_arguments=builds_own_arguments,
            reports_returned_value=self._has_dependent_output(
                tree, resolver, result_names
            ),
            # A pure side-effect symbol cannot furnish an observable result, so
            # it must not inflate the count.
            distinct_api_symbols=sum(
                1 for symbol in demonstrated.values() if symbol.returns_value
            ),
            demonstrated_symbols=tuple(sorted(demonstrated)),
            driver_symbols_called=tuple(sorted(drivers)),
            launched_subprocess=launched_subprocess,
        )

    def _has_dependent_output(
        self,
        tree: ast.Module,
        resolver: _ImportResolver,
        result_names: set[str],
    ) -> bool:
        """Is any printed/asserted value derived from a repository call?"""
        for node in ast.walk(tree):
            observed: list[ast.AST] = []
            if isinstance(node, ast.Call) and _dotted_name(node.func) in {
                "print",
                "pprint.pprint",
            }:
                observed = list(node.args)
            elif isinstance(node, ast.Assert):
                observed = [node.test]
            if not observed:
                continue
            for expression in observed:
                for inner in ast.walk(expression):
                    if (
                        isinstance(inner, ast.Call)
                        and self._resolve_call(inner.func, resolver) is not None
                    ):
                        return True
                    if isinstance(inner, ast.Name) and inner.id in result_names:
                        return True
        return False
