"""Go-live readiness checklist.

Turns the actual run evidence — AP controls, the parallel run, e-invoicing parse
coverage, and the dead-letter queue — into a machine-checkable gate list. The
CLI exits non-zero when the platform is not ready, so this doubles as a
release gate.
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_paths, load_config
from ..integrations.engine_client import EngineClient
from ..util import atomic_write_json, new_run_id, now_iso
from .ap_controls import run_ap_controls
from .parallel_run import run_parallel_run


def evaluate_go_live(cfg: dict, ap_result: dict, parallel_result: dict) -> dict:
    """Pure evaluation: build the gate list from two already-computed reports."""
    policy = cfg.get("go_live", {})
    max_blocked = int(policy.get("max_blocked_invoices", 0))
    max_unexpected = int(policy.get("max_unexpected_parallel_variances", 0))

    blocked = ap_result.get("counts", {}).get("blocked", 0)
    unexpected = parallel_result.get("counts", {}).get("unexpected", 0)
    coverage = next((c for c in ap_result["controls"]
                     if c["name"] == "einvoice_parse_coverage"), None)
    coverage_ok = bool(coverage and coverage["status"] == "PASS"
                       and not ap_result.get("exceptions", {}).get("parseErrors"))
    dlq_ok = not ap_result.get("exceptions", {}).get("dlqEvents")

    gates = [
        {
            "id": "ap_controls_pass",
            "description": "all AP controls and jurisdiction ties PASS",
            "status": "PASS" if ap_result.get("status") == "PASS" else "FAIL",
            "evidence": ap_result.get("outputPath"),
        },
        {
            "id": "parallel_run_no_unexpected_variances",
            "description": f"unexpected parallel-run variances <= {max_unexpected}",
            "status": "PASS" if unexpected <= max_unexpected else "FAIL",
            "observed": unexpected,
            "evidence": parallel_result.get("outputPath"),
        },
        {
            "id": "blocked_invoices_within_threshold",
            "description": f"blocked (unmatched) invoices <= {max_blocked}",
            "status": "PASS" if blocked <= max_blocked else "FAIL",
            "observed": blocked,
            "evidence": ap_result.get("outputPath"),
        },
    ]
    if policy.get("require_einvoice_coverage", True):
        gates.append({
            "id": "einvoice_parse_coverage",
            "description": "every inbound e-invoice parsed with no errors",
            "status": "PASS" if coverage_ok else "FAIL",
            "evidence": ap_result.get("outputPath"),
        })
    if policy.get("require_no_dlq", True):
        gates.append({
            "id": "no_dead_letters",
            "description": "no vendor-invoice events in the dead-letter queue",
            "status": "PASS" if dlq_ok else "FAIL",
            "evidence": ap_result.get("outputPath"),
        })

    status = "READY" if all(g["status"] == "PASS" for g in gates) else "NOT_READY"
    return {
        "status": status,
        "gates": gates,
        "blockedInvoices": ap_result.get("exceptions", {}).get("blockedInvoices", []),
        "unexpectedVariances": parallel_result.get("unexpectedVariances", []),
    }


def run_go_live(
    cfg: dict | None = None,
    root=None,
    engine: EngineClient | None = None,
    inject: list[str] | None = None,
    write: bool = True,
) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    ap_result = run_ap_controls(cfg=cfg, root=root, engine=engine, write=True)
    parallel_result = run_parallel_run(cfg=cfg, root=root, inject=inject, write=True)

    result = evaluate_go_live(cfg, ap_result, parallel_result)
    result["runId"] = new_run_id("go_live")
    result["generatedAt"] = now_iso()
    result["apControlsRunId"] = ap_result["runId"]
    result["parallelRunId"] = parallel_result["runId"]

    if write:
        paths.go_live.mkdir(parents=True, exist_ok=True)
        out: Path = paths.go_live / f"{result['runId']}.json"
        atomic_write_json(out, result)
        result["outputPath"] = str(out)
    return result
