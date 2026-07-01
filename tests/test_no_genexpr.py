"""Repo-wide lint: no generator expressions (pyscript forbids them).

pyscript does not support generator expressions (e.g. ``sum(x for x in y)``);
list comprehensions must be used instead (``sum([x for x in y])``). This test
parses each source file's AST and asserts there are zero ``GeneratorExp`` nodes.

``ast.parse`` does not execute code, so no fakes/fixtures are needed.
"""

import ast
from pathlib import Path

import pytest

SOURCE_FILES = [
    "TeslaSmartCharging.py",
    "UpdateSpotPriceSensors.py",
    "SolarSavings.py",
    "HotWaterOptimizer.py",
    "SolarForecast.py",
]


@pytest.mark.parametrize("fname", SOURCE_FILES)
def test_no_generator_expressions(fname):
    path = Path(__file__).parent.parent / fname
    tree = ast.parse(path.read_text())
    offending = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.GeneratorExp)]
    assert offending == [], (
        f"{fname} contains forbidden generator expression(s) at line(s): "
        f"{offending}. Use list comprehensions instead (pyscript forbids genexprs)."
    )
