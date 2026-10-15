"""Atomic JSON file store with a process-local lock (single-process services)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..util import atomic_write_json


class JsonStore:
    def __init__(self, path: Path, default: Any):
        self._path = Path(path)
        self._default = default
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Any:
        if not self._path.exists():
            return json.loads(json.dumps(self._default))
        with open(self._path, encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data: Any) -> None:
        atomic_write_json(self._path, data)

    def update(self, fn):
        """Read-modify-write under lock. Returns fn's result."""
        with self._lock:
            data = self.load()
            result = fn(data)
            self.save(data)
            return result
