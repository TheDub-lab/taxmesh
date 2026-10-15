"""Minimal monitoring: roll up run manifests and the DLQ into one health record.

The evidence trail is the set of run manifests under outputs/runs plus the dead
letter queues under outputs/dlq. This module reads them back and answers the
only two operational questions that matter here: did every job finish cleanly,
and is anything sitting in a dead letter queue?
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths, get_paths, load_config
from ..integrations.bus import read_dlq
from ..util import atomic_write_json, new_run_id, now_iso

HEALTHY_STATUSES = {"COMPLETED"}


def _load_manifests(paths: Paths) -> list[dict]:
    manifests = []
    for path in sorted(paths.runs.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                manifests.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def _dlq_summary(paths: Paths) -> dict:
    files = []
    total = 0
    for path in sorted(paths.dlq.glob("*.jsonl")):
        records = read_dlq(path)
        if records:
            files.append({"file": path.name, "events": len(records)})
        total += len(records)
    return {"locations": files, "totalEvents": total}


def build_health(cfg: dict | None = None, root=None) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    manifests = _load_manifests(paths)
    dlq = _dlq_summary(paths)

    by_status: dict[str, int] = {}
    degraded_runs: list[dict] = []
    for manifest in manifests:
        status = manifest.get("status", "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
        if status not in HEALTHY_STATUSES:
            degraded_runs.append({
                "runId": manifest.get("runId"),
                "job": manifest.get("job"),
                "status": status,
                "errors": len(manifest.get("errors", [])),
            })

    healthy = not degraded_runs and dlq["totalEvents"] == 0
    return {
        "runId": new_run_id("health"),
        "generatedAt": now_iso(),
        "status": "HEALTHY" if healthy else "DEGRADED",
        "runs": {
            "total": len(manifests),
            "byStatus": by_status,
            "degraded": degraded_runs,
            "latest": manifests[-1]["runId"] if manifests else None,
        },
        "dlq": dlq,
    }


def run_monitor(cfg: dict | None = None, root=None, write: bool = True) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    result = build_health(cfg, root)
    if write:
        paths.monitoring.mkdir(parents=True, exist_ok=True)
        out: Path = paths.monitoring / "health.json"
        atomic_write_json(out, result)
        result["outputPath"] = str(out)
    return result
