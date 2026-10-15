"""Integration bus: retry policy, idempotency, dead-letter queue, run manifests.

This is the in-repo stand-in for an integration platform / middleware layer.
Handlers must be idempotent — the bus guarantees at-least-once delivery.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Paths
from ..util import atomic_write_json, new_run_id, now_iso, short_hash


@dataclass
class AttemptResult:
    ok: bool
    attempts: int
    result: Any = None
    error: str | None = None
    dlq: bool = False


class IdempotencyStore:
    """JSON-backed record of processed keys -> cached results."""

    def __init__(self, path: Path):
        self._path = Path(path)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        with open(self._path, encoding="utf-8") as fh:
            return json.load(fh)

    def get(self, key: str) -> Any | None:
        rec = self._load().get(key)
        return rec["result"] if rec else None

    def put(self, key: str, result: Any) -> None:
        data = self._load()
        data[key] = {"recordedAt": now_iso(), "result": result}
        atomic_write_json(self._path, data)


def deliver(
    event: dict,
    handler: Callable[[], Any],
    *,
    dlq_path: Path,
    max_attempts: int = 3,
    backoff_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> AttemptResult:
    """Attempt handler with retries; append to the DLQ file on exhaustion."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return AttemptResult(ok=True, attempts=attempt, result=handler())
        except Exception as err:  # noqa: BLE001 - bus must never let an error escape
            last_error = err
            if attempt < max_attempts and backoff_s > 0:
                sleep(backoff_s * attempt)

    record = {
        "eventId": event.get("eventId"),
        "event": event,
        "attempts": max_attempts,
        "error": f"{type(last_error).__name__}: {last_error}",
        "failedAt": now_iso(),
    }
    dlq_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dlq_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return AttemptResult(ok=False, attempts=max_attempts, error=record["error"], dlq=True)


def read_dlq(dlq_path: Path) -> list[dict]:
    if not dlq_path.exists():
        return []
    with open(dlq_path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class RunManifest:
    """One JSON file per job run: counts, hashes, errors, status.

    This is the raw material of the SOX evidence trail (Phase 4 packages it).
    """

    def __init__(self, job: str, paths: Paths, notes: dict | None = None):
        stamp = now_iso().replace(":", "").replace("-", "")
        self.run_id = f"{job}-{stamp}-{short_hash(str(time.time_ns()), 6)}"
        self.job = job
        self.paths = paths
        self.started_at = now_iso()
        self.finished_at: str | None = None
        self.status = "RUNNING"
        self.counts: dict[str, int] = {}
        self.attempts_total = 0
        self.errors: list[dict] = []
        self.hashes: dict[str, str] = {}
        self.notes = notes or {}
        self._write()

    def count(self, status: str, n: int = 1) -> None:
        self.counts[status] = self.counts.get(status, 0) + n

    def add_error(self, ref: str, error: str) -> None:
        self.errors.append({"ref": ref, "error": error, "at": now_iso()})

    def set_hash(self, label: str, value) -> None:
        self.hashes[label] = short_hash(value)

    def finish(self, status: str) -> Path:
        self.status = status
        self.finished_at = now_iso()
        return self._write()

    @property
    def manifest_path(self) -> Path:
        return self.paths.runs / f"{self.run_id}.json"

    def _write(self) -> Path:
        self.paths.runs.mkdir(parents=True, exist_ok=True)
        payload = {
            "runId": self.run_id,
            "job": self.job,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "status": self.status,
            "counts": self.counts,
            "attemptsTotal": self.attempts_total,
            "errors": self.errors,
            "hashes": self.hashes,
            "notes": self.notes,
        }
        atomic_write_json(self.manifest_path, payload)
        return self.manifest_path
