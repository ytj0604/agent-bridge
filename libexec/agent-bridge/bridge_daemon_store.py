#!/usr/bin/env python3
from pathlib import Path
from typing import Any, Callable

from bridge_util import locked_json, read_json


AGGREGATE_STORE_DEFAULT = {"version": 1, "aggregates": {}}


class QueueStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def update(self, mutator: Callable[[list[dict]], Any]) -> Any:
        with locked_json(self.path, []) as queue:
            result = mutator(queue)
            return result

    def read(self) -> list[dict]:
        return self.update(lambda queue: list(queue))


class AggregateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _default() -> dict:
        return {"version": 1, "aggregates": {}}

    @classmethod
    def _normalize(cls, data: object) -> dict:
        if not isinstance(data, dict):
            return cls._default()
        normalized = dict(data)
        normalized.setdefault("version", 1)
        if not isinstance(normalized.get("aggregates"), dict):
            normalized["aggregates"] = {}
        return normalized

    def update(self, mutator: Callable[[dict], Any]) -> Any:
        with locked_json(self.path, self._default()) as data:
            if not isinstance(data.get("aggregates"), dict):
                data["aggregates"] = {}
            data.setdefault("version", 1)
            result = mutator(data)
            return result

    def read(self) -> dict:
        return self._normalize(read_json(self.path, self._default()))

    def read_aggregates(self) -> dict:
        aggregates = self.read().get("aggregates")
        return aggregates if isinstance(aggregates, dict) else {}

    def get(self, aggregate_id: str) -> dict:
        aggregate = self.read_aggregates().get(aggregate_id)
        return dict(aggregate) if isinstance(aggregate, dict) else {}
