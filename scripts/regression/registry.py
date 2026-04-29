from __future__ import annotations

from collections.abc import Callable, Sequence
import importlib
from pathlib import Path
import sys

ScenarioFn = Callable[[str, Path], None]
Scenario = tuple[str, ScenarioFn]
ScenarioSource = Sequence[Scenario] | Callable[[], Sequence[Scenario]]


def load_scenarios(source: ScenarioSource | None = None) -> list[Scenario]:
    raw_source = source if source is not None else _legacy_scenarios_source()
    scenarios = list(raw_source() if callable(raw_source) else raw_source)
    _validate_scenarios(scenarios)
    return scenarios


def _legacy_scenarios_source() -> Callable[[], Sequence[Scenario]]:
    main_module = sys.modules.get("__main__")
    main_file = str(getattr(main_module, "__file__", "") or "")
    if main_file.endswith("regression_interrupt.py") and hasattr(main_module, "scenarios"):
        return getattr(main_module, "scenarios")

    module = importlib.import_module("regression_interrupt")
    return getattr(module, "scenarios")


def _validate_scenarios(scenarios: Sequence[Scenario]) -> None:
    labels: set[str] = set()
    functions: set[ScenarioFn] = set()
    duplicate_labels: list[str] = []
    duplicate_functions: list[str] = []

    for entry in scenarios:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError(f"invalid scenario entry: {entry!r}")
        label, fn = entry
        if not isinstance(label, str) or not label:
            raise ValueError(f"invalid scenario label: {label!r}")
        if not callable(fn):
            raise ValueError(f"scenario {label!r} is not callable: {fn!r}")
        if label in labels:
            duplicate_labels.append(label)
        labels.add(label)
        if fn in functions:
            duplicate_functions.append(getattr(fn, "__name__", repr(fn)))
        functions.add(fn)

    if duplicate_labels:
        raise ValueError(f"duplicate scenario labels: {', '.join(duplicate_labels)}")
    if duplicate_functions:
        raise ValueError(f"duplicate scenario functions: {', '.join(duplicate_functions)}")
