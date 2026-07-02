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
    # Registration is required so freezegun can patch the module's datetime binding.
    sys.modules[filename] = mod
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


class FakeState:
    """Recording stand-in for pyscript's ``state`` namespace.

    ``get``/``getattr`` mirror the source's read pattern (source does
    ``state.getattr(...) or {}`` on the attr result, so a missing prime -> None
    is handled by the caller). ``setattr`` records dotted-key writes and never
    raises (source wraps its setattr block in try/except; a raising fake would
    silently leave ``attrs_written`` empty).
    """

    def __init__(self, get_map, attr_map):
        self.get_map = get_map
        self.attr_map = attr_map
        self.setattr_calls = []
        self.attrs_written = {}

    def get(self, entity):
        return self.get_map.get(entity)

    def getattr(self, entity):
        return self.attr_map.get(entity)

    def setattr(self, dotted_key, value):
        self.setattr_calls.append((dotted_key, value))
        self.attrs_written[dotted_key] = value


class FakeLog:
    """Recording stand-in for pyscript's ``log`` namespace.

    Each level appends ``(level, msg)`` to ``self.records``; unknown levels are
    handled by the ``__getattr__`` fallback returning a recording callable.
    """

    def __init__(self):
        self.records = []

    def warning(self, msg):
        self.records.append(("warning", msg))

    def debug(self, msg):
        self.records.append(("debug", msg))

    def info(self, msg):
        self.records.append(("info", msg))

    def error(self, msg):
        self.records.append(("error", msg))

    def __getattr__(self, level):
        def _record(msg):
            self.records.append((level, msg))
        return _record


class FakeInputText:
    """Recording stand-in for pyscript's ``input_text`` namespace.

    Source writes bare attribute assignments (``input_text.tesla_charging_schedule
    = summary``); ``__setattr__`` captures ``name -> value`` into ``.writes``.
    The internal store is set via ``object.__setattr__`` to avoid recursion.
    """

    def __init__(self):
        object.__setattr__(self, "_writes", {})

    def __setattr__(self, name, value):
        self._writes[name] = value

    @property
    def writes(self):
        return self._writes


class FakeInputNumber:
    """Recording stand-in for pyscript's ``input_number`` namespace.

    Source writes bare attribute assignments (``input_number.tesla_charging_status
    = status_code``); ``__setattr__`` captures ``name -> value`` into ``.writes``.
    The internal store is set via ``object.__setattr__`` to avoid recursion.
    """

    def __init__(self):
        object.__setattr__(self, "_writes", {})

    def __setattr__(self, name, value):
        self._writes[name] = value

    @property
    def writes(self):
        return self._writes


class _World:
    """Namespace exposing the injected fakes for assertions."""

    def __init__(self, state, log, input_text, input_number):
        self.state = state
        self.log = log
        self.input_text = input_text
        self.input_number = input_number


@pytest.fixture
def world(monkeypatch):
    """Factory injecting recording fakes onto a session-loaded module.

    ``make(mod, get=..., attrs=...)`` monkeypatches ``state``/``log``/
    ``input_text``/``input_number`` on ``mod`` and returns a ``_World`` exposing
    them. monkeypatch auto-reverts each test, restoring the ``_Noop`` stubs.
    """

    def make(mod, *, get=None, attrs=None):
        fake_state = FakeState(get or {}, attrs or {})
        fake_log = FakeLog()
        fake_input_text = FakeInputText()
        fake_input_number = FakeInputNumber()
        monkeypatch.setattr(mod, "state", fake_state)
        monkeypatch.setattr(mod, "log", fake_log)
        monkeypatch.setattr(mod, "input_text", fake_input_text)
        monkeypatch.setattr(mod, "input_number", fake_input_number)
        return _World(fake_state, fake_log, fake_input_text, fake_input_number)

    return make


@pytest.fixture(scope="session")
def tesla():
    return load_pyscript("TeslaSmartCharging.py")


@pytest.fixture(scope="session")
def spot():
    return load_pyscript("UpdateSpotPriceSensors.py")


@pytest.fixture(scope="session")
def savings():
    return load_pyscript("SolarSavings.py")
