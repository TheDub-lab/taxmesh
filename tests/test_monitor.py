"""Monitoring roll-up: manifests + DLQ -> HEALTHY / DEGRADED."""

from __future__ import annotations

from src.integrations.bus import RunManifest, deliver
from src.monitoring.health import build_health, run_monitor


def test_clean_manifests_are_healthy(ap_workspace):
    RunManifest("ap_flow", ap_workspace.paths).finish("COMPLETED")
    RunManifest("tieouts", ap_workspace.paths).finish("COMPLETED")

    result = build_health(cfg=ap_workspace.cfg, root=ap_workspace.root)
    assert result["status"] == "HEALTHY"
    assert result["runs"]["total"] == 2
    assert result["runs"]["byStatus"] == {"COMPLETED": 2}
    assert result["dlq"]["totalEvents"] == 0


def test_failed_manifest_degrades_health(ap_workspace):
    RunManifest("ap_flow", ap_workspace.paths).finish("COMPLETED")
    RunManifest("tieouts", ap_workspace.paths).finish("FAILED")

    result = build_health(cfg=ap_workspace.cfg, root=ap_workspace.root)
    assert result["status"] == "DEGRADED"
    degraded = result["runs"]["degraded"]
    assert [d["job"] for d in degraded] == ["tieouts"]
    assert degraded[0]["status"] == "FAILED"


def test_completed_with_exceptions_is_degraded(ap_workspace):
    RunManifest("ap_flow", ap_workspace.paths).finish("COMPLETED_WITH_EXCEPTIONS")
    result = build_health(cfg=ap_workspace.cfg, root=ap_workspace.root)
    assert result["status"] == "DEGRADED"


def test_dead_letters_degrades_health_even_when_runs_pass(ap_workspace):
    RunManifest("ap_flow", ap_workspace.paths).finish("COMPLETED")

    def boom():
        raise RuntimeError("engine 422")

    deliver({"eventId": "EVT-AP-00001"}, boom,
            dlq_path=ap_workspace.paths.dlq / "ap_events.jsonl", max_attempts=1)

    result = build_health(cfg=ap_workspace.cfg, root=ap_workspace.root)
    assert result["status"] == "DEGRADED"
    assert result["dlq"]["totalEvents"] == 1
    assert result["runs"]["degraded"] == []


def test_run_monitor_writes_the_health_record(ap_workspace):
    RunManifest("ap_flow", ap_workspace.paths).finish("COMPLETED")
    result = run_monitor(cfg=ap_workspace.cfg, root=ap_workspace.root)

    assert result["status"] == "HEALTHY"
    assert (ap_workspace.paths.monitoring / "health.json").exists()
    assert result["outputPath"].endswith("health.json")
