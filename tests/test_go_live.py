"""Go-live readiness gate: evidence -> READY / NOT_READY."""

from __future__ import annotations

from src.controls.go_live import evaluate_go_live, run_go_live
from src.integrations.ap_flow import run_ap_flow
from src.sources.seed_ap import run_seed_ap


def _clean(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)


def test_clean_run_is_ready(ap_workspace, ap_engine):
    _clean(ap_workspace, ap_engine)

    result = run_go_live(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "READY"
    assert all(g["status"] == "PASS" for g in result["gates"])
    assert result["blockedInvoices"] == []


def test_blocked_invoices_force_not_ready(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root,
                inject=["price_variance", "no_po", "duplicate_invoice"])
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)

    result = run_go_live(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "NOT_READY"

    by_id = {g["id"]: g for g in result["gates"]}
    assert by_id["blocked_invoices_within_threshold"]["status"] == "FAIL"
    assert by_id["blocked_invoices_within_threshold"]["observed"] == 3
    # integrity is intact even though the migration is gated
    assert by_id["ap_controls_pass"]["status"] == "PASS"
    assert by_id["parallel_run_no_unexpected_variances"]["status"] == "PASS"
    assert len(result["blockedInvoices"]) == 3


def test_unexpected_parallel_variance_blocks_go_live(ap_workspace, ap_engine):
    _clean(ap_workspace, ap_engine)

    result = run_go_live(cfg=ap_workspace.cfg, root=ap_workspace.root,
                         engine=ap_engine, inject=["legacy_drift"])
    assert result["status"] == "NOT_READY"

    by_id = {g["id"]: g for g in result["gates"]}
    assert by_id["parallel_run_no_unexpected_variances"]["status"] == "FAIL"
    assert by_id["parallel_run_no_unexpected_variances"]["observed"] == 1
    assert len(result["unexpectedVariances"]) == 1


def test_evaluate_go_live_is_a_pure_gate_list(ap_workspace):
    cfg = ap_workspace.cfg
    ap_pass = {
        "status": "PASS",
        "counts": {"blocked": 0},
        "exceptions": {"dlqEvents": [], "parseErrors": []},
        "controls": [{"name": "einvoice_parse_coverage", "status": "PASS"}],
    }
    parallel_pass = {"counts": {"unexpected": 0}, "unexpectedVariances": []}

    ready = evaluate_go_live(cfg, ap_pass, parallel_pass)
    assert ready["status"] == "READY"
    assert {g["id"] for g in ready["gates"]} == {
        "ap_controls_pass", "parallel_run_no_unexpected_variances",
        "blocked_invoices_within_threshold", "einvoice_parse_coverage",
        "no_dead_letters",
    }

    gated = evaluate_go_live(
        cfg,
        {**ap_pass, "counts": {"blocked": 1}},
        parallel_pass,
    )
    assert gated["status"] == "NOT_READY"


def test_dlq_blocks_go_live_gate(ap_workspace):
    cfg = ap_workspace.cfg
    ap_pass = {
        "status": "PASS",
        "counts": {"blocked": 0},
        "exceptions": {"dlqEvents": ["EVT-AP-00009"], "parseErrors": []},
        "controls": [{"name": "einvoice_parse_coverage", "status": "PASS"}],
    }
    result = evaluate_go_live(cfg, ap_pass, {"counts": {"unexpected": 0}})
    assert result["status"] == "NOT_READY"
    assert {g["id"]: g["status"] for g in result["gates"]}["no_dead_letters"] == "FAIL"
