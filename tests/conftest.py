"""Pytest scaffolding to load HA pyscript files under plain CPython.

The scripts target Home Assistant's pyscript runtime, which injects magic
globals (decorators like ``@service``, namespaces like ``state``/``log``) and
provides the ``homeassistant.*`` recorder packages. Neither exists under plain
CPython, so we stub them: ``_Noop`` covers the injected globals, and a
``sys.modules`` fake covers the ``homeassistant.*`` imports. This lets the
source files ``exec`` cleanly so their pure functions can be unit-tested.
"""

import datetime
import pathlib
import sys
import types
import typing

import pytest


class _Noop:
    """Stub for pyscript runtime globals (decorators, state, log). See load_pyscript docstring for the mock-boundary contract."""

    def __call__(self, *a, **k):
        # Bare-form decorator: @service, @time_trigger with a single callable
        # arg and no kwargs -> return the decorated function unchanged.
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        # Arg-form decorator: @time_trigger("cron..."), @state_trigger(f"...")
        # -> return a passthrough decorator.
        return lambda fn: fn

    def __getattr__(self, name):
        # Swallow attribute access like log.warning, state.getattr, etc.
        return _Noop()


# Fake homeassistant.* packages so the recorder imports resolve.
# Add a dotted path here if a future recorder script imports a new
# homeassistant.* symbol.
_HA_FAKES = [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.recorder",
    "homeassistant.components.recorder.statistics",
    "homeassistant.components.recorder.history",
]

for _path in _HA_FAKES:
    _fake = types.ModuleType(_path)
    _fake.get_instance = _Noop()
    _fake.statistics_during_period = _Noop()
    _fake.get_significant_states = _Noop()
    sys.modules[_path] = _fake


def load_pyscript(filename):
    """Load a HA pyscript source file as a module under plain CPython.

    Reads the source relative to the repo root, seeds a fresh module namespace
    with the pyscript magic globals (as ``_Noop`` stubs) plus ``Literal`` and
    ``dt`` (used unimported in def-time annotations by the recorder scripts),
    then execs the source.

    Mock boundary is import-only: ``_Noop`` silently swallows attribute access,
    so do NOT rely on its return values in assertions. The loader never
    modifies the source file.
    """
    src = (pathlib.Path(__file__).parent.parent / filename).read_text()
    mod = types.ModuleType(filename)
    for name in (
        "time_trigger", "service", "state_trigger", "log", "state", "task",
        "hass", "pyscript", "input_number", "input_text", "input_boolean",
        "input_select", "sensor", "switch", "number", "binary_sensor",
    ):
        mod.__dict__[name] = _Noop()
    mod.__dict__["Literal"] = typing.Literal
    mod.__dict__["dt"] = datetime
    exec(compile(src, filename, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="session")
def tesla():
    return load_pyscript("TeslaSmartCharging.py")


@pytest.fixture(scope="session")
def spot():
    return load_pyscript("UpdateSpotPriceSensors.py")


@pytest.fixture(scope="session")
def savings():
    return load_pyscript("SolarSavings.py")
