from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from .registry import Scenario, ScenarioSource, load_scenarios


def main(argv: list[str] | None = None, scenario_source: ScenarioSource | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--list", action="store_true", help="list matching scenario labels without running them")
    parser.add_argument("--match", action="append", default=[], metavar="TEXT", help="run only scenarios whose label or function name contains TEXT")
    parser.add_argument("--fail-fast", action="store_true", help="stop after the first failing scenario")
    args = parser.parse_args(argv)

    scenarios = _filter_scenarios(load_scenarios(scenario_source), args.match)
    if args.list:
        try:
            for label, _fn in scenarios:
                print(label)
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except OSError:
                pass
        return 0
    if not scenarios:
        print("No scenarios matched.", file=sys.stderr)
        return 1
    return run_scenarios(scenarios, keep_tmp=args.keep_tmp, fail_fast=args.fail_fast)


def _filter_scenarios(scenarios: list[Scenario], patterns: list[str]) -> list[Scenario]:
    if not patterns:
        return scenarios
    return [
        (label, fn)
        for label, fn in scenarios
        if any(pattern in label or pattern in getattr(fn, "__name__", "") for pattern in patterns)
    ]


def run_scenarios(scenarios: list[Scenario], *, keep_tmp: bool = False, fail_fast: bool = False) -> int:
    base = Path(tempfile.mkdtemp(prefix="bridge-regression-"))
    try:
        passes = 0
        fails = 0
        for label, fn in scenarios:
            sub = base / label
            sub.mkdir(parents=True, exist_ok=True)
            try:
                fn(label, sub)
                passes += 1
            except AssertionError as exc:
                print(f"  FAIL  {label}: {exc}", file=sys.stderr)
                fails += 1
                if fail_fast:
                    break
            except Exception as exc:
                print(f"  ERROR {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
                fails += 1
                if fail_fast:
                    break
        print(f"--- {passes} passed, {fails} failed ---")
        return 0 if fails == 0 else 1
    finally:
        if not keep_tmp:
            shutil.rmtree(base, ignore_errors=True)
