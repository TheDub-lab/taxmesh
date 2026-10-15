"""Controls must detect source-to-target drift after a clean run."""

from __future__ import annotations

import sqlite3

from src.controls.tieouts import run_tieouts
from src.integrations.flow import run_flow
from src.sources import erp, tax_mart
from src.sources.seed import run_seed


def _clean_run(workspace, engine):
    run_seed(cfg=workspace.cfg, root=workspace.root)
    run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine)


def test_clean_run_ties_out_everywhere(workspace, engine):
    _clean_run(workspace, engine)
    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "PASS"
    assert all(c["status"] == "PASS" for c in tie["controls"])
    assert all(r["status"] == "PASS" for r in tie["byJurisdiction"])
    assert tie["exceptions"] == {
        "missingInEngine": [], "extraInEngine": [],
        "dlqEvents": [], "pendingEvents": [],
    }
    assert tie["outputPath"].endswith(".json")


def test_missing_gl_posting_is_detected(workspace, engine):
    _clean_run(workspace, engine)
    journal = erp.load_journal(workspace.paths)
    journal = journal[:-1]  # tamper: drop the last GL entry
    erp.save_journal(workspace.paths, journal)

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "FAIL"
    by_name = {c["name"]: c for c in tie["controls"]}
    assert by_name["count_billing_vs_gl"]["status"] == "FAIL"
    assert by_name["count_billing_vs_gl"]["delta"] == 1  # billing minus GL
    assert by_name["money_subtotal_billing_vs_gl"]["status"] == "FAIL"
    assert by_name["count_billing_vs_engine"]["status"] == "PASS"  # engine untouched


def test_mart_row_drift_is_detected(workspace, engine):
    _clean_run(workspace, engine)
    con = sqlite3.connect(tax_mart.db_path(workspace.paths))
    con.execute("DELETE FROM fct_invoices WHERE invoice_id = "
                "(SELECT MAX(invoice_id) FROM fct_invoices)")
    con.commit()
    con.close()

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "FAIL"
    by_name = {c["name"]: c for c in tie["controls"]}
    assert by_name["count_billing_vs_mart"]["status"] == "FAIL"
    assert by_name["count_billing_vs_mart"]["delta"] == 1  # billing minus mart
    assert by_name["count_billing_vs_engine"]["status"] == "PASS"
