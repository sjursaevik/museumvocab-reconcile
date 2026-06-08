"""A tiny resumable on-disk cache.

Authority lookups are the slow, network-bound part of the pipeline and must be
resumable: if ``lookup`` is interrupted, re-running it should pick up where it
left off rather than re-querying everything. Keys are typically the source term
ID (for whole-term results) or an authority concept ID (for fetched concepts).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def has(self, key: str) -> bool:
        return key in self._data

    def set(self, key: str, value: Any, *, flush: bool = True) -> None:
        self._data[key] = value
        if flush:
            self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
