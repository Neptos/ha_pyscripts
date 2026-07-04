"""Repo-wide lint: no lambdas capturing enclosing-function locals (pyscript bug).

pyscript's AST interpreter fails to resolve a lambda's closure over an
enclosing function's LOCAL variable at call time, raising NameError — even
though the identical code works under CPython (which is what this test suite
runs on, so a runtime test cannot catch it). Regression that motivated this
guard (HA logs, 2026-07-04): ``lambda s, b, sp: _calculate_effective_price(s,
b, sp, solar_ctx=solar_ctx)`` in TeslaSmartCharging raised
"name 'solar_ctx' is not defined" on every call, emptying the slot pool and
killing schedule calculation.

Rule enforced: a lambda body may only reference its own parameters, names it
binds itself (comprehension targets, walrus), module-level names, and
builtins. To pass invariants into a callback, call the target function
directly or bind values via lambda default arguments
(``lambda s, _ctx=ctx: f(s, _ctx)``) — defaults are evaluated at definition
time in the enclosing scope, which pyscript handles correctly.

``ast.parse`` does not execute code, so no fakes/fixtures are needed.
"""

import ast
import builtins
from pathlib import Path

import pytest

SOURCE_FILES = [
    "TeslaSmartCharging.py",
    "UpdateSpotPriceSensors.py",
    "SolarSavings.py",
    "HotWaterOptimizer.py",
    "SolarForecast.py",
]


def _module_level_names(tree):
    """Names bound at module scope (defs, classes, imports, assignments)."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        else:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
    return names


def _lambda_free_names(lam):
    """Names the lambda body loads that it does not bind itself."""
    bound = {a.arg for a in lam.args.args + lam.args.posonlyargs + lam.args.kwonlyargs}
    if lam.args.vararg:
        bound.add(lam.args.vararg.arg)
    if lam.args.kwarg:
        bound.add(lam.args.kwarg.arg)
    for node in ast.walk(lam.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.comprehension):
            for tgt in ast.walk(node.target):
                if isinstance(tgt, ast.Name):
                    bound.add(tgt.id)
    return {
        node.id
        for node in ast.walk(lam.body)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id not in bound
    }


@pytest.mark.parametrize("fname", SOURCE_FILES)
def test_no_closure_capturing_lambdas(fname):
    path = Path(__file__).parent.parent / fname
    tree = ast.parse(path.read_text())
    allowed = _module_level_names(tree) | set(dir(builtins))
    offending = [
        (node.lineno, sorted(_lambda_free_names(node) - allowed))
        for node in ast.walk(tree)
        if isinstance(node, ast.Lambda) and (_lambda_free_names(node) - allowed)
    ]
    assert offending == [], (
        f"{fname} contains lambda(s) capturing enclosing-function locals at "
        f"(line, names): {offending}. pyscript raises NameError on such "
        f"closures at call time; call the function directly or bind the value "
        f"via a lambda default argument instead."
    )
