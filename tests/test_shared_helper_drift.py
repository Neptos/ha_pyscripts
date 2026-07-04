"""Static drift guard for the canonical shared price helpers.

The deployment model for this repo is "copy each ``.py`` file into Home
Assistant's ``config/pyscript/`` directory" (see CLAUDE.md). There is no shared
module import path at runtime, so ``_parse_dt`` and ``_normalize_price_data`` are
physically duplicated across the scripts that need them. To keep the copies from
silently drifting apart, this test parses each source file's AST, extracts the
exact source text of the shared functions, and asserts the copies are
byte-identical. If you must change one copy, change them all in lockstep.

``ast.parse`` does not execute code, so no fakes/fixtures are needed.
"""

import ast
from pathlib import Path

import pytest

# Files that carry the canonical copies of the shared helpers.
SOURCE_FILES = [
    "TeslaSmartCharging.py",
    "UpdateSpotPriceSensors.py",
    "HotWaterOptimizer.py",
]

SHARED_FUNCTIONS = ["_parse_dt", "_normalize_price_data"]

# _get_statistic is duplicated only across the two recorder-statistics scripts.
STATISTIC_PAIR = ["UpdateSpotPriceSensors.py", "SolarSavings.py"]


def _extract_function_source(fname, func_name):
    path = Path(__file__).parent.parent / fname
    source = path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not extract {func_name} from {fname}"
            return segment
    pytest.fail(f"{func_name} not found in {fname}")


@pytest.mark.parametrize("func_name", SHARED_FUNCTIONS)
def test_shared_helpers_are_byte_identical(func_name):
    sources = {
        fname: _extract_function_source(fname, func_name) for fname in SOURCE_FILES
    }
    reference_file = SOURCE_FILES[0]
    reference = sources[reference_file]
    for fname, segment in sources.items():
        assert segment == reference, (
            f"{func_name} in {fname} has drifted from {reference_file}. "
            f"The shared helpers must be byte-identical across "
            f"{SOURCE_FILES} (deployment precludes a shared module)."
        )


def test_get_statistic_is_byte_identical():
    sources = {
        fname: _extract_function_source(fname, "_get_statistic") for fname in STATISTIC_PAIR
    }
    reference_file = STATISTIC_PAIR[0]
    reference = sources[reference_file]
    for fname, segment in sources.items():
        assert segment == reference, (
            f"_get_statistic in {fname} has drifted from {reference_file}. "
            f"The copies must be byte-identical across "
            f"{STATISTIC_PAIR} (deployment precludes a shared module)."
        )
